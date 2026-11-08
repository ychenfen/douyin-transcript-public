import json
import urllib.error
import urllib.request

import pytest

from douyin_workflow import cli, pipeline
from douyin_workflow.config import Settings
from douyin_workflow.exporters import (
    format_item,
    split_subtitle_segments,
    to_json,
    to_markdown,
    to_srt,
    to_txt,
    to_vtt,
)
from douyin_workflow.server import App, make_server

SAMPLE_ITEM = {
    "aweme_id": "7372484719365098803",
    "title": "开源大模型本地部署指南",
    "author": "科技小王",
    "duration_s": 12.0,
    "share_url": "https://v.douyin.com/iRNBho6u/",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb924",
    "backend": "iesdouyin",
    "created_at": "2026-11-08 12:00:00",
    "transcript": "大家好，今天我们来聊聊如何在本地部署开源大模型。第一步是准备好环境。第二步是下载量化权重并启动服务。",
}


def test_to_markdown_renders_yaml_frontmatter_and_body():
    md = to_markdown(SAMPLE_ITEM)
    assert md.startswith("---\n")
    assert 'title: "开源大模型本地部署指南"' in md
    assert 'author: "科技小王"' in md
    assert 'aweme_id: "7372484719365098803"' in md
    assert "duration_s: 12.0" in md
    assert 'source_url: "https://v.douyin.com/iRNBho6u/"' in md
    assert 'sha256: "e3b0c44298fc1c149afbf4c8996fb924"' in md
    assert "# 开源大模型本地部署指南" in md
    assert "## 逐字稿" in md
    assert "第一步是准备好环境" in md


def test_to_srt_and_to_vtt_generate_valid_subtitle_cues():
    srt = to_srt(SAMPLE_ITEM)
    assert "1\n00:00:00,000 --> " in srt
    assert "大家好，今天我们来聊聊如何在本地部署开源大模型" in srt
    assert "2\n" in srt
    assert "00:00:12,000\n第二步是下载量化权重并启动服务" in srt

    vtt = to_vtt(SAMPLE_ITEM)
    assert vtt.startswith("WEBVTT\n\n1\n00:00:00.000 --> ")
    assert "00:00:12.000\n第二步是下载量化权重并启动服务" in vtt


def test_split_subtitle_segments_supports_explicit_segments():
    cues = split_subtitle_segments(
        "ignored",
        segments=[
            {"start": 0.5, "end": 2.25, "text": "第一句。"},
            {"start": 2.25, "end": 5.0, "text": "第二句！"},
        ],
    )
    assert cues == [(0.5, 2.25, "第一句。"), (2.25, 5.0, "第二句！")]


def test_format_item_dispatcher_and_invalid_format():
    assert format_item(SAMPLE_ITEM, "txt") == to_txt(SAMPLE_ITEM)
    assert json.loads(format_item(SAMPLE_ITEM, "json"))["aweme_id"] == SAMPLE_ITEM["aweme_id"]
    assert format_item(SAMPLE_ITEM, "md") == to_markdown(SAMPLE_ITEM)
    assert format_item(SAMPLE_ITEM, "srt") == to_srt(SAMPLE_ITEM)
    assert format_item(SAMPLE_ITEM, "vtt") == to_vtt(SAMPLE_ITEM)
    with pytest.raises(ValueError, match="不支持的导出格式"):
        format_item(SAMPLE_ITEM, "xml")


def test_cli_export_and_format_flag(tmp_path, monkeypatch, capsys):
    settings = Settings(data_dir=tmp_path / "data")
    item_dir = settings.data_dir / SAMPLE_ITEM["aweme_id"]
    item_dir.mkdir(parents=True)
    (item_dir / "meta.json").write_text(json.dumps(SAMPLE_ITEM, ensure_ascii=False), encoding="utf-8")
    (item_dir / "transcript.txt").write_text(SAMPLE_ITEM["transcript"], encoding="utf-8")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)

    assert cli.main(["export", SAMPLE_ITEM["aweme_id"], "--format", "srt"]) == 0
    srt_out = capsys.readouterr().out
    assert "00:00:00,000 -->" in srt_out

    out_file = tmp_path / "out.md"
    assert cli.main(["export", SAMPLE_ITEM["aweme_id"], "--format", "md", "-o", str(out_file)]) == 0
    assert out_file.read_text(encoding="utf-8").startswith("---\n")

    assert cli.main(["export", "0000000000000000000", "--format", "md"]) == 3


def test_server_transcript_export_routes(tmp_path):
    import threading

    settings = Settings(data_dir=tmp_path / "data")
    item_id = SAMPLE_ITEM["aweme_id"]
    item_dir = settings.data_dir / item_id
    item_dir.mkdir(parents=True)
    (item_dir / "meta.json").write_text(json.dumps(SAMPLE_ITEM, ensure_ascii=False), encoding="utf-8")
    (item_dir / "transcript.txt").write_text(SAMPLE_ITEM["transcript"], encoding="utf-8")

    app = App("secret", settings=settings, require_auth=False)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        with urllib.request.urlopen(f"{base}/items/{item_id}/transcript.md") as resp:
            body = resp.read().decode("utf-8")
            assert resp.headers["Content-Type"].startswith("text/markdown")
            assert 'aweme_id: "7372484719365098803"' in body

        with urllib.request.urlopen(f"{base}/items/{item_id}/transcript.srt") as resp:
            body = resp.read().decode("utf-8")
            assert resp.headers["Content-Type"].startswith("application/x-subrip")
            assert "00:00:00,000 -->" in body

        with urllib.request.urlopen(f"{base}/items/{item_id}/transcript.vtt") as resp:
            body = resp.read().decode("utf-8")
            assert resp.headers["Content-Type"].startswith("text/vtt")
            assert body.startswith("WEBVTT")

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base}/items/9999999999999999999/transcript.md")
        assert exc_info.value.code == 404

        # WeChat items require auth even when require_auth=False
        wx_id = "wx-0123456789abcdef"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base}/items/{wx_id}/transcript.md")
        assert exc_info.value.code == 401
    finally:
        srv.shutdown()
