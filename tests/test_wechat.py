import json

import pytest

from douyin_workflow import pipeline
from douyin_workflow.config import Settings
from douyin_workflow.downloaders import VideoInfo
from douyin_workflow.share import ShareParseError
from douyin_workflow.wechat import (
    WechatResolveError,
    WechatProfile,
    YuanbaoWechatResolver,
    extract_wechat_url,
    wechat_item_id,
)


SHARE = "https://weixin.qq.com/sph/A6uI5lLvuQ"


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.responses.pop(0))


def test_extract_wechat_url_normalizes_supported_forms():
    assert extract_wechat_url(f"看看 {SHARE} 这个") == SHARE
    assert extract_wechat_url(
        "https://channels.weixin.qq.com/finder-preview/pages/sph?id=A6uI5lLvuQ"
    ) == SHARE
    with pytest.raises(ShareParseError):
        extract_wechat_url("https://example.com/sph/A6uI5lLvuQ")


def test_resolver_requires_cookie():
    with pytest.raises(WechatResolveError, match="尚未配置"):
        YuanbaoWechatResolver("").resolve(SHARE)


def test_resolver_uses_authenticated_parse_then_feed_info():
    session = Session(
        [
            {
                "code": 0,
                "data": {
                    "playable_url": "https://channels.weixin.qq.com/finder-preview/pages/feed?token=token123&eid=export123",
                    "desc": "备用标题",
                    "author": "备用作者",
                },
            },
            {
                "errCode": 0,
                "data": {
                    "errMsg": {"type": 0},
                    "authorInfo": {"nickname": "王道在线"},
                    "feedInfo": {
                        "description": "408 真题该刷了",
                        "mediaType": 4,
                        "coverUrl": "https://cdn.example/cover.jpg",
                        "h264VideoInfo": {"videoUrl": "https://cdn.example/video.mp4"},
                    },
                },
            },
        ]
    )
    profile = YuanbaoWechatResolver("secret-cookie", session=session).resolve(SHARE)
    assert (profile.title, profile.author, profile.video_url) == (
        "408 真题该刷了",
        "王道在线",
        "https://cdn.example/video.mp4",
    )
    assert session.calls[0][1]["headers"]["Cookie"] == "secret-cookie"
    assert session.calls[1][1]["json"]["baseReq"]["generalToken"] == "token123"


def test_resolver_rejects_metadata_only_response():
    session = Session(
        [
            {"data": {"playable_url": "https://x/?token=t&eid=e"}},
            {"errCode": 0, "data": {"errMsg": {"type": 0}, "feedInfo": {"coverUrl": "x"}}},
        ]
    )
    with pytest.raises(WechatResolveError, match="没有可下载的视频流"):
        YuanbaoWechatResolver("cookie", session=session).resolve(SHARE)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (["unexpected"], "无法识别的数据"),
        ({"code": 1001, "msg": "请重新登录"}, "请重新登录"),
    ],
)
def test_resolver_rejects_invalid_yuanbao_response(payload, message):
    session = Session([payload])
    with pytest.raises(WechatResolveError, match=message):
        YuanbaoWechatResolver("cookie", session=session).resolve(SHARE)


def test_resolver_rejects_non_object_feed_response():
    session = Session(
        [
            {"code": 0, "data": {"playable_url": "https://x/?token=t&eid=e"}},
            ["unexpected"],
        ]
    )
    with pytest.raises(WechatResolveError, match="视频号返回了无法识别的数据"):
        YuanbaoWechatResolver("cookie", session=session).resolve(SHARE)


def test_resolver_rejects_non_object_feed_info():
    session = Session(
        [
            {"code": 0, "data": {"playable_url": "https://x/?token=t&eid=e"}},
            {"errCode": 0, "data": {"errMsg": {"type": 0}, "feedInfo": ["unexpected"]}},
        ]
    )
    with pytest.raises(WechatResolveError, match="视频信息格式不正确"):
        YuanbaoWechatResolver("cookie", session=session).resolve(SHARE)


def test_download_does_not_persist_signed_playback_url(tmp_path, monkeypatch):
    signed = "https://finder.video.qq.com/video.mp4?token=secret"
    resolver = YuanbaoWechatResolver("cookie")
    monkeypatch.setattr(
        resolver,
        "resolve",
        lambda _text: WechatProfile(
            share_url=SHARE,
            video_url=signed,
            title="标题",
            author="作者",
        ),
    )

    def fake_stream(urls, dest, session, timeout, **kwargs):
        assert urls == [signed]
        dest.write_bytes(b"video")
        return signed

    monkeypatch.setattr("douyin_workflow.wechat.stream_to_file", fake_stream)
    info = resolver.download(SHARE, "wx-test", tmp_path)
    assert info.source_url == SHARE
    assert "cover_url" not in info.extra
    assert "secret" not in json.dumps(info.to_dict())


def test_wechat_pipeline_writes_platform_metadata_and_caches(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", wechat_yuanbao_cookie="cookie")
    calls = []

    def fake_download(self, share_text, item_id, out_dir):
        calls.append(share_text)
        out_dir.mkdir(parents=True, exist_ok=True)
        video = out_dir / "video.mp4"
        video.write_bytes(b"video")
        return VideoInfo(
            aweme_id=item_id,
            title="微信标题",
            author="微信作者",
            video_path=video,
            backend="wechat-yuanbao",
        )

    monkeypatch.setattr(YuanbaoWechatResolver, "download", fake_download)
    monkeypatch.setattr(pipeline, "extract_audio", lambda video, wav: wav.write_bytes(b"wav") or wav)
    monkeypatch.setattr(pipeline, "transcribe", lambda wav, model, device: "微信逐字稿")
    result = pipeline.link_to_text(SHARE, settings=settings)
    assert result["aweme_id"] == wechat_item_id(SHARE)
    assert result["source_platform"] == "wechat" and result["transcript"] == "微信逐字稿"
    meta = json.loads((settings.data_dir / result["aweme_id"] / "meta.json").read_text(encoding="utf-8"))
    assert meta["backend"] == "wechat-yuanbao" and meta["share_url"] == SHARE
    assert pipeline.link_to_text(SHARE, settings=settings)["cached"] is True
    assert len(calls) == 1
