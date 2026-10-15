"""FunASR 中文转写。模型只加载一次，常驻在 MCP 进程里。

注意：MCP 走 stdio，FunASR/modelscope 会往 stdout print，必须重定向到 stderr，
否则会把 JSON-RPC 流打坏。
"""

from __future__ import annotations

import contextlib
import functools
import gc
import hashlib
import json
import logging
import os
import shutil
import wave
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_MODEL_KWARGS = {
    "paraformer-zh": dict(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc"),
    "SenseVoiceSmall": dict(
        model="iic/SenseVoiceSmall", vad_model="fsmn-vad", vad_kwargs={"max_single_segment_time": 30000}
    ),
}

_models: dict[tuple[str, str], object] = {}
_lock = threading.Lock()


def release_models() -> bool:
    """Drop idle model caches; serialized with inference so active work is safe."""
    with _lock:
        if not _models:
            return False
        _models.clear()
        gc.collect()
        if sys.platform.startswith("linux"):
            # glibc can retain freed ONNX arenas unless explicitly trimmed.
            import ctypes

            try:
                trim = ctypes.CDLL(None).malloc_trim
                trim.argtypes = [ctypes.c_size_t]
                trim.restype = ctypes.c_int
                trim(0)
            except (OSError, AttributeError):
                pass
    return True


def _release_after_job(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        finally:
            if os.getenv("DOUYIN_RELEASE_ASR_AFTER_JOB", "0").lower() in {"1", "true", "yes", "on"}:
                release_models()
    return wrapped


class TranscribeError(RuntimeError):
    pass


def resolve_device(device: str) -> str:
    if device != "auto":
        return device
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda:0"
    except ImportError:
        pass
    return "cpu"


def extract_audio(video: Path, wav: Path) -> Path:
    """转成 16kHz 单声道 wav，FunASR 的标准输入。"""
    ffmpeg = os.getenv("DOUYIN_FFMPEG") or shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            pass
    if not ffmpeg:
        raise TranscribeError("找不到 ffmpeg，请先安装并加入 PATH")
    cmd = [ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not wav.exists():
        raise TranscribeError(f"ffmpeg 抽音频失败：{proc.stderr.strip()[-300:]}")
    return wav


def _load(model_name: str, device: str):
    key = (model_name, device)
    with _lock:
        if key not in _models:
            if model_name not in _MODEL_KWARGS:
                raise TranscribeError(f"不支持的模型 {model_name}，可选 {list(_MODEL_KWARGS)}")
            try:
                from funasr import AutoModel
            except ImportError as e:
                raise TranscribeError("未安装 funasr（pip install funasr modelscope torch torchaudio）") from e
            log.info("加载 FunASR 模型 %s @ %s（首次会下载模型）", model_name, device)
            with contextlib.redirect_stdout(sys.stderr):
                _models[key] = AutoModel(
                    **_MODEL_KWARGS[model_name], device=device, disable_update=True, disable_pbar=True
                )
        return _models[key]


# ---- 轻量后端：sherpa-onnx + SenseVoice int8（纯 CPU，约 230MB，不需要 torch） ----
# 小内存云服务器用这个。模型目录里要有 model.int8.onnx 和 tokens.txt。
SHERPA_MODEL = "sherpa-sensevoice"
CHUNK_S = 25.0  # SenseVoice 适合 30 s 以内的片段
SEARCH_S = 5.0  # 在每个切点前后这么多秒里找最安静的位置下刀，避免切断词


def _sherpa_recognizer():
    key = (SHERPA_MODEL, "cpu")
    with _lock:
        if key not in _models:
            try:
                import sherpa_onnx
            except ImportError as e:
                raise TranscribeError("未安装 sherpa-onnx（pip install sherpa-onnx numpy）") from e
            d = Path(os.getenv("DOUYIN_SHERPA_MODEL_DIR", "~/dy2text-public/models/sensevoice")).expanduser()
            model, tokens = d / "model.int8.onnx", d / "tokens.txt"
            if not (model.exists() and tokens.exists()):
                raise TranscribeError(f"找不到 SenseVoice 模型文件：{d}")
            log.info("加载 sherpa-onnx SenseVoice：%s", d)
            _models[key] = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model),
                tokens=str(tokens),
                num_threads=int(os.getenv("DOUYIN_ASR_THREADS", "2")),
                language="zh",
                use_itn=True,
            )
        return _models[key]


def split_points(samples, sr: int = 16000, chunk_s: float = CHUNK_S, search_s: float = SEARCH_S) -> list[int]:
    """返回切点（样本下标）。每个切点取目标位置 ±search_s 内 20 ms 帧能量最低处。"""
    import numpy as np

    n = len(samples)
    frame = int(sr * 0.02)
    cuts, start = [0], 0
    while n - start > int(sr * (chunk_s + search_s)):
        lo = start + int(sr * (chunk_s - search_s))
        hi = min(start + int(sr * (chunk_s + search_s)), n - frame)
        seg = samples[lo:hi]
        k = len(seg) // frame
        energy = (seg[: k * frame].reshape(k, frame) ** 2).mean(axis=1)
        start = lo + int(np.argmin(energy)) * frame
        cuts.append(start)
    cuts.append(n)
    return cuts


def _pcm_chunks(wav: Path, start_frame: int = 0):
    """Read at most 30.02 seconds; never allocate a whole long recording."""
    import numpy as np

    with wave.open(str(wav)) as f:
        sr = f.getframerate()
        if f.getnchannels() != 1 or f.getsampwidth() != 2 or sr != 16000:
            raise TranscribeError("转写需要 16 kHz 单声道 PCM16 WAV，请先抽取音频")
        total = f.getnframes()
        window = int(sr * (CHUNK_S + SEARCH_S + 0.02))
        offset = start_frame
        while offset < total:
            f.setpos(offset)
            samples = np.frombuffer(f.readframes(min(window, total-offset)), dtype="<i2").astype(np.float32) / 32768.0
            cut = split_points(samples, sr)[1]
            yield offset / sr, (offset + cut) / sr, samples[:cut]
            offset += cut


def _transcribe_sherpa(wav: Path) -> str:
    return "".join(s["text"] for s in transcribe_segments(wav, SHERPA_MODEL)).strip()


def _atomic_json(path: Path, data) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


@_release_after_job
def transcribe_segments(wav: Path, model_name: str = SHERPA_MODEL, device: str = "auto",
                        checkpoint_dir: Path | None = None, force: bool = False) -> list[dict]:
    """Timestamped chunks; sherpa resumes only against the same audio and model."""
    with wave.open(str(wav)) as f:
        sr, frames = f.getframerate(), f.getnframes()
    if model_name != SHERPA_MODEL:
        return [{"start": 0.0, "end": frames/sr, "text": transcribe(wav, model_name, device)}]
    rows = []
    checkpoint = None
    if checkpoint_dir:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with wav.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        identity = {"schema": 1, "audio_sha256": digest.hexdigest(), "model": model_name, "device": device,
                    "sample_rate": sr, "frames": frames, "chunk_s": CHUNK_S, "search_s": SEARCH_S}
        identity_path = checkpoint_dir / "checkpoint.json"
        checkpoint = checkpoint_dir / "segments.jsonl"
        try:
            if not force and json.loads(identity_path.read_text()) == identity:
                end = 0.0
                for line in checkpoint.read_text().splitlines():
                    try:
                        row = json.loads(line)
                        assert isinstance(row["text"], str) and row["start"] == end
                        assert end < row["end"] <= frames/sr and row["end"] - end <= CHUNK_S + SEARCH_S + 1e-6
                    except (ValueError, KeyError, TypeError, AssertionError):
                        break  # An interrupted final write is discarded; keep the valid prefix.
                    rows.append(row)
                    end = row["end"]
        except (OSError, ValueError):
            pass
        checkpoint.write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in rows), encoding="utf-8")
        _atomic_json(identity_path, identity)
    rec = _sherpa_recognizer()
    with _lock:
        for start, end, samples in _pcm_chunks(wav, round(rows[-1]["end"]*sr) if rows else 0):
            st = rec.create_stream()
            st.accept_waveform(sr, samples)
            rec.decode_stream(st)
            row = {"start": start, "end": end, "text": st.result.text.strip()}
            rows.append(row)
            if checkpoint:
                with checkpoint.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False)+"\n")
                _atomic_json(checkpoint_dir / "progress.json", {"processed_s": end, "duration_s": frames/sr, "segments": len(rows)})
    return rows


@_release_after_job
def transcribe(wav: Path, model_name: str = "paraformer-zh", device: str = "auto") -> str:
    if model_name == SHERPA_MODEL:
        return _transcribe_sherpa(wav)
    device = resolve_device(device)
    model = _load(model_name, device)
    # 同一个模型不并发推理，GPU 显存也扛不住
    with _lock, contextlib.redirect_stdout(sys.stderr):
        if model_name == "SenseVoiceSmall":
            from funasr.utils.postprocess_utils import rich_transcription_postprocess

            res = model.generate(
                input=str(wav), language="zh", use_itn=True, batch_size_s=60, merge_vad=True, merge_length_s=15
            )
            text = "".join(rich_transcription_postprocess(r["text"]) for r in res)
        else:
            res = model.generate(input=str(wav), batch_size_s=300)
            text = "".join(r.get("text", "") for r in res)
    return text.strip()
