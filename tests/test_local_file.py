import json
import wave

import pytest

from douyin_workflow import cli, pipeline
from douyin_workflow.config import Settings
from douyin_workflow.transcribe import TranscribeError


def _write_wav(w, seconds=2):
    with wave.open(str(w), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\0\0" * 16000 * seconds)
    return w


@pytest.fixture
def settings(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "extract_audio", lambda v, w: _write_wav(w))

    def fake_transcribe(w, m, d):
        calls.append(w)
        return "视频号逐字稿。"

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    s = Settings(data_dir=tmp_path / "data", backends=["iesdouyin"], min_interval=0)
    s.calls = calls
    return s


def test_file_to_text_writes_files_and_caches(tmp_path, settings):
    video = tmp_path / "视频号.mp4"
    video.write_bytes(b"fake video")
    res = pipeline.file_to_text(video, settings=settings)
    item = settings.data_dir / res["aweme_id"]
    assert res["aweme_id"].startswith("file-") and res["cached"] is False
    assert res["title"] == "视频号" and res["duration_s"] == 2.0 and res["backend"] == "local_file"
    assert (item / "transcript.txt").read_text(encoding="utf-8") == "视频号逐字稿。"
    assert json.loads((item / "meta.json").read_text(encoding="utf-8"))["source_file"] == "视频号.mp4"
    assert video.exists()  # 原文件不动

    again = pipeline.file_to_text(video, settings=settings)
    assert again["cached"] is True and len(settings.calls) == 1
    pipeline.file_to_text(video, force=True, settings=settings)
    assert len(settings.calls) == 2


def test_file_to_text_custom_title_and_list_local(tmp_path, settings):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")
    pipeline.file_to_text(video, title="考研冲刺", settings=settings)
    [m] = pipeline.list_local(settings=settings)
    assert m["title"] == "考研冲刺" and m["aweme_id"].startswith("file-")


def test_same_content_shares_cache(tmp_path, settings):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_bytes(b"same")
    b.write_bytes(b"same")
    assert pipeline.file_to_text(a, settings=settings)["aweme_id"] == pipeline.file_to_text(b, settings=settings)["aweme_id"]
    assert len(settings.calls) == 1


def test_missing_file(tmp_path, settings):
    with pytest.raises(TranscribeError, match="找不到文件"):
        pipeline.file_to_text(tmp_path / "nope.mp4", settings=settings)
    assert pipeline.run_file_safe(str(tmp_path / "nope.mp4"), settings=settings)["error"] == "transcribe_failed"


def test_cli_file(tmp_path, settings, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v")
    assert cli.main(["file", str(video), "--title", "标题"]) == 0
    out = capsys.readouterr().out
    assert "# 标题" in out and "视频号逐字稿。" in out
    assert cli.main(["file", str(tmp_path / "missing.mp4")]) == 3
