import pytest

pytest.importorskip("mcp")

from douyin_workflow import mcp_server  # noqa: E402
from douyin_workflow.downloaders import AllBackendsFailed  # noqa: E402


def test_bad_share_text_returns_error():
    assert mcp_server.douyin_to_text("随便一段话")["error"] == "bad_share_text"


def test_download_failure_is_reported_not_raised(monkeypatch):
    def boom(*a, **k):
        raise AllBackendsFailed({"iesdouyin": "x", "ytdlp": "cookie 过期"})

    monkeypatch.setattr(mcp_server.pipeline, "douyin_to_text", boom)
    res = mcp_server.douyin_to_text("https://www.douyin.com/video/7372484719365098803")
    assert res["error"] == "download_failed" and res["backends"]["ytdlp"] == "cookie 过期"


def test_wechat_tool_uses_safe_link_dispatch(monkeypatch):
    monkeypatch.setattr(mcp_server.pipeline, "run_safe", lambda text, force=False: {"text": text, "force": force})
    result = mcp_server.wechat_link_to_text("https://weixin.qq.com/sph/A6uI5lLvuQ", force=True)
    assert result["force"] is True and "weixin.qq.com" in result["text"]
