"""主流程：分享口令 → 作品 ID → 下载（降级）→ 抽音频 → 转写 → 落盘。

本地视频（视频号、录屏等先下载到本机的文件）走 file_to_text：跳过下载，
按文件 SHA-256 前 16 位建目录 <data_dir>/file-<hash>/，同一文件再次请求读缓存。

每条视频一个目录 <data_dir>/<aweme_id>/：
  meta.json       标题、作者、时长、用了哪个后端、哪些后端失败了
  transcript.txt  逐字稿
  audio.wav       16k 单声道音频（留着方便换模型重转）
  video.mp4       默认转写完删掉，DOUYIN_KEEP_VIDEO=1 保留
同一个作品再次请求时直接读缓存，不再访问抖音。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import wave
from pathlib import Path

import requests

from .config import Settings, get_settings
from .bilibili import BilibiliError, BilibiliSource, is_bilibili_share_text, resolve_bilibili
from .downloaders import AllBackendsFailed, DownloadChain, UnsupportedContent, build_backends
from .media import cleanup_retained_videos
from .share import ShareParseError, resolve
from .transcribe import TranscribeError, extract_audio, transcribe, transcribe_segments, _atomic_json
from .transcripts import exports, write_exports
from .wechat import WechatResolveError, YuanbaoWechatResolver, extract_wechat_url, is_wechat_share_text, wechat_item_id

log = logging.getLogger(__name__)

_chain: DownloadChain | None = None


def _get_chain(settings: Settings) -> DownloadChain:
    # 进程内共用一条链，限速才对跨请求生效
    global _chain
    if _chain is None:
        _chain = DownloadChain(build_backends(settings), settings.min_interval)
    return _chain


def load_cached(item_dir: Path) -> dict | None:
    meta, txt = item_dir / "meta.json", item_dir / "transcript.txt"
    if not (meta.exists() and txt.exists()):
        return None
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["transcript"] = txt.read_text(encoding="utf-8")
    data["cached"] = True
    segments_path = item_dir / "segments.json"
    if data.get("source_platform") == "bilibili" and segments_path.exists():
        data["segments"] = json.loads(segments_path.read_text(encoding="utf-8"))
        data["exports"] = exports(data, data["segments"])
    return data


def douyin_to_text(share_text: str, force: bool = False, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    url, aweme_id = resolve(share_text, timeout=settings.timeout)
    item_dir = settings.data_dir / aweme_id

    if not force and (cached := load_cached(item_dir)):
        log.info("命中缓存 %s", aweme_id)
        return cached

    t0 = time.monotonic()
    info, fallback_errors = _get_chain(settings).download(url, aweme_id, item_dir)
    t_dl = time.monotonic() - t0

    wav = extract_audio(info.video_path, item_dir / "audio.wav")
    text = transcribe(wav, settings.asr_model, settings.asr_device)
    t_asr = time.monotonic() - t0 - t_dl

    if not settings.keep_video and info.video_path and info.video_path.exists():
        info.video_path.unlink()
        info.video_path = None

    meta = {
        **info.to_dict(),
        "share_url": url,
        "asr_model": settings.asr_model,
        "fallback_errors": fallback_errors,
        "timing_s": {"download": round(t_dl, 1), "transcribe": round(t_asr, 1)},
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dir": str(item_dir),
    }
    (item_dir / "transcript.txt").write_text(text, encoding="utf-8")
    (item_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    cleanup_retained_videos(settings)
    return {**meta, "transcript": text, "cached": False}


def resolve_source(share_text: str, settings: Settings | None = None) -> tuple[str, str, str]:
    """Return canonical URL, stable item ID, and source platform."""
    settings = settings or get_settings()
    if is_bilibili_share_text(share_text):
        url, item_id = resolve_bilibili(share_text, timeout=settings.timeout)
        return url, item_id, "bilibili"
    if is_wechat_share_text(share_text):
        url = extract_wechat_url(share_text)
        return url, wechat_item_id(url), "wechat"
    url, aweme_id = resolve(share_text, timeout=settings.timeout)
    return url, aweme_id, "douyin"


def wechat_to_text(share_text: str, force: bool = False, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    url = extract_wechat_url(share_text)
    item_id = wechat_item_id(url)
    item_dir = settings.data_dir / item_id
    if not force and (cached := load_cached(item_dir)):
        log.info("命中缓存 %s", item_id)
        return cached

    t0 = time.monotonic()
    info = YuanbaoWechatResolver(settings.wechat_yuanbao_cookie, settings.timeout).download(url, item_id, item_dir)
    t_download = time.monotonic() - t0
    wav = extract_audio(info.video_path, item_dir / "audio.wav")
    text = transcribe(wav, settings.asr_model, settings.asr_device)
    t_asr = time.monotonic() - t0 - t_download
    if not settings.keep_video and info.video_path and info.video_path.exists():
        info.video_path.unlink()
        info.video_path = None
    meta = {
        **info.to_dict(),
        "share_url": url,
        "source_platform": "wechat",
        "asr_model": settings.asr_model,
        "timing_s": {"download": round(t_download, 1), "transcribe": round(t_asr, 1)},
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dir": str(item_dir),
    }
    (item_dir / "transcript.txt").write_text(text, encoding="utf-8")
    (item_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    cleanup_retained_videos(settings)
    return {**meta, "transcript": text, "cached": False}


def bilibili_to_text(share_text: str, force: bool = False, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    url, item_id = resolve_bilibili(share_text, timeout=settings.timeout)
    directory = settings.data_dir / item_id
    if not force and (cached := load_cached(directory)):
        return cached
    directory.mkdir(parents=True, exist_ok=True)
    # Do not serve old completed metadata with new, partly written exports.
    (directory / "meta.json").unlink(missing_ok=True)
    (directory / "progress.json").unlink(missing_ok=True)
    started = time.monotonic()
    source = BilibiliSource(settings.timeout)
    try:
        meta = source.metadata(url)
        marker_path = directory / "audio-source.json"
        wav = directory / "audio.wav"
        try:
            marker = json.loads(marker_path.read_text())
        except (OSError, ValueError):
            marker = {}
        reusable = (not force and wav.exists() and marker.get("cid") == meta["cid"]
                    and marker.get("wav_sha256") == _sha256(wav))
        if not reusable:
            audio = source.download_audio(meta, directory / "source.m4a")
            temporary_wav = extract_audio(audio, directory / "audio.tmp.wav")
            duration = _wav_seconds(temporary_wav)
            if duration is None or abs(duration - meta["duration_s"]) > 3:
                raise BilibiliError("音频实际时长与所选分 P 不一致，未将不完整结果写入缓存")
            temporary_wav.replace(wav)
            _atomic_json(marker_path, {"cid": meta["cid"], "wav_sha256": _sha256(wav)})
            audio.unlink(missing_ok=True)
    finally:
        source.session.close()
    download_s = time.monotonic() - started
    segments = transcribe_segments(wav, settings.asr_model, settings.asr_device,
                                   checkpoint_dir=directory, force=force)
    actual_duration = _wav_seconds(wav)
    if not segments or abs(segments[-1]["end"] - actual_duration) > 0.2:
        raise TranscribeError("音频未完整转写，已保存分段进度，可重试续跑")
    meta.update({"aweme_id": item_id, "video_path": None, "source_url": url,
                 "asr_model": settings.asr_model, "audio_duration_s": actual_duration,
                 "timing_s": {"download": round(download_s, 1), "transcribe": round(time.monotonic()-started-download_s, 1)},
                 "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "dir": str(directory),
                 "transcription_note": "语音识别初稿，未逐字校对，未提取画面文字"})
    text = "\n".join(s["text"] for s in segments).strip()
    content = write_exports(directory, meta, segments)
    (directory / "transcript.txt").write_text(text, encoding="utf-8")
    _atomic_json(directory / "meta.json", meta)  # Publish completed cache last.
    return {**meta, "transcript": text, "segments": segments, "exports": content, "cached": False}


def link_to_text(share_text: str, force: bool = False, settings: Settings | None = None) -> dict:
    if is_bilibili_share_text(share_text):
        return bilibili_to_text(share_text, force=force, settings=settings)
    if is_wechat_share_text(share_text):
        return wechat_to_text(share_text, force=force, settings=settings)
    return douyin_to_text(share_text, force=force, settings=settings)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _wav_seconds(wav: Path) -> float | None:
    try:
        with wave.open(str(wav)) as f:
            return round(f.getnframes() / f.getframerate(), 1)
    except (wave.Error, OSError, ZeroDivisionError):
        return None


def file_to_text(
    path: str | Path, title: str | None = None, force: bool = False, settings: Settings | None = None
) -> dict:
    """本地视频或音频文件 → 逐字稿。原文件只读不动，不复制进素材库。"""
    settings = settings or get_settings()
    src = Path(path).expanduser()
    if not src.is_file():
        raise TranscribeError(f"找不到文件：{src}")
    try:
        digest = _sha256(src)
    except OSError as e:
        raise TranscribeError(f"读不了文件：{e}") from e
    item_id = f"file-{digest[:16]}"
    item_dir = settings.data_dir / item_id

    if not force and (cached := load_cached(item_dir)):
        log.info("命中缓存 %s", item_id)
        # 同一内容换了标题或文件名：只更新元数据，不重新转写
        updates = {"source_file": src.name, **({"title": title} if title else {})}
        if any(cached.get(k) != v for k, v in updates.items()):
            meta_path = item_dir / "meta.json"
            meta = {**json.loads(meta_path.read_text(encoding="utf-8")), **updates}
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            cached.update(updates)
        return cached

    item_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    wav = extract_audio(src, item_dir / "audio.wav")
    text = transcribe(wav, settings.asr_model, settings.asr_device)

    meta = {
        "aweme_id": item_id,
        "title": title or src.stem,
        "author": None,
        "duration_s": _wav_seconds(wav),
        "backend": "local_file",
        "source_file": src.name,
        "sha256": digest,
        "asr_model": settings.asr_model,
        "timing_s": {"transcribe": round(time.monotonic() - t0, 1)},
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dir": str(item_dir),
    }
    (item_dir / "transcript.txt").write_text(text, encoding="utf-8")
    (item_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**meta, "transcript": text, "cached": False}


def run_file_safe(
    path: str, title: str | None = None, force: bool = False, settings: Settings | None = None
) -> dict:
    """file_to_text 的不抛异常版本，给 MCP 用。"""
    try:
        return file_to_text(path, title=title, force=force, settings=settings)
    except TranscribeError as e:
        return {"error": "transcribe_failed", "message": str(e)}
    except OSError as e:
        return {"error": "file_unreadable", "message": f"读不了文件：{e}"}


def run_safe(share_text: str, force: bool = False, settings: Settings | None = None) -> dict:
    """douyin_to_text 的不抛异常版本：已知错误转成 {"error", "message"}，给 MCP / HTTP 用。"""
    try:
        return link_to_text(share_text, force=force, settings=settings)
    except ShareParseError as e:
        return {"error": "bad_share_text", "message": str(e)}
    except UnsupportedContent as e:
        return {"error": "unsupported_content", "message": str(e)}
    except AllBackendsFailed as e:
        return {
            "error": "download_failed",
            "message": "所有下载后端都失败了，可能是 cookie 过期或被风控，先跑 healthcheck 排查",
            "backends": e.errors,
        }
    except TranscribeError as e:
        return {"error": "transcribe_failed", "message": str(e)}
    except WechatResolveError as e:
        return {"error": "wechat_unavailable", "message": str(e)}
    except BilibiliError as e:
        return {"error": "bilibili_unavailable", "message": str(e)}
    except requests.RequestException as e:
        return {"error": "network", "message": f"网络出错：{e}"}


def list_local(limit: int = 20, settings: Settings | None = None) -> list[dict]:
    """最近处理过的作品（不含逐字稿正文），给 Claude 翻素材库用。"""
    settings = settings or get_settings()
    if not settings.data_dir.exists():
        return []
    metas = sorted(settings.data_dir.glob("*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in metas[:limit]:
        m = json.loads(p.read_text(encoding="utf-8"))
        out.append({k: m.get(k) for k in ("aweme_id", "title", "author", "duration_s", "created_at", "dir")})
    return out
