"""Resolve a WeChat Channels share URL with an authenticated Yuanbao session.

The public ``weixin.qq.com/sph/...`` page only exposes metadata and a cover.
Yuanbao's authenticated parser supplies the short-lived playback URL used here.
The raw Cookie is injected through configuration and is never logged or written
to the material library.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from .downloaders.base import DownloadError, VideoInfo, stream_to_file
from .share import MOBILE_UA, ShareParseError, _URL_RE


YUANBAO_PARSE_URL = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
WECHAT_FEED_URL = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
WECHAT_SHARE_HOSTS = {"weixin.qq.com", "channels.weixin.qq.com"}


class WechatResolveError(RuntimeError):
    pass


def extract_wechat_url(share_text: str) -> str:
    """Return a canonical ``https://weixin.qq.com/sph/<id>`` URL."""
    for match in _URL_RE.finditer(share_text):
        raw = match.group(0).rstrip(".,，。)）】」")
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if host not in WECHAT_SHARE_HOSTS:
            continue
        share_id = ""
        if host == "weixin.qq.com" and parsed.path.startswith("/sph/"):
            share_id = parsed.path.removeprefix("/sph/").strip("/")
        elif host == "channels.weixin.qq.com" and parsed.path == "/finder-preview/pages/sph":
            share_id = parse_qs(parsed.query).get("id", [""])[0].strip()
        if share_id and all(char.isalnum() or char in "_-" for char in share_id):
            return f"https://weixin.qq.com/sph/{share_id}"
    raise ShareParseError(f"没有在口令里找到视频号分享链接：{share_text[:80]!r}")


def is_wechat_share_text(text: str) -> bool:
    try:
        extract_wechat_url(text)
        return True
    except ShareParseError:
        return False


def wechat_item_id(url: str) -> str:
    return "wx-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


@dataclass
class WechatProfile:
    share_url: str
    video_url: str
    title: str
    author: str
    cover_url: str = ""


class YuanbaoWechatResolver:
    def __init__(self, cookie: str, timeout: float = 20, session: requests.Session | None = None):
        self.cookie = cookie.strip()
        self.timeout = timeout
        self.session = session or requests.Session()

    def _yuanbao_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": "https://yuanbao.tencent.com",
            "Referer": "https://yuanbao.tencent.com/chat/naQivTmsDa/cf4d0079-ed1b-4c55-a3f3-2ca1379727d1",
            "User-Agent": MOBILE_UA,
            "Cookie": self.cookie,
            "X-Requested-With": "XMLHttpRequest",
            "X-Language": "zh-CN",
            "X-Platform": "mac",
            "X-Source": "web",
        }

    def resolve(self, share_text: str) -> WechatProfile:
        share_url = extract_wechat_url(share_text)
        if not self.cookie:
            raise WechatResolveError("视频号直链解析尚未配置元宝登录态")

        try:
            parsed_response = self.session.post(
                YUANBAO_PARSE_URL,
                json={"type": "video_channel_url", "url": share_url, "scene": 1},
                headers=self._yuanbao_headers(),
                timeout=self.timeout,
            )
            parsed_response.raise_for_status()
            parsed_payload = parsed_response.json()
        except (requests.RequestException, ValueError) as error:
            raise WechatResolveError(f"元宝解析请求失败：{error}") from error

        if not isinstance(parsed_payload, dict):
            raise WechatResolveError("元宝返回了无法识别的数据，登录态可能已失效")
        if parsed_payload.get("code") not in (0, None):
            message = str(parsed_payload.get("msg") or "元宝解析失败").strip()
            raise WechatResolveError(message)
        parse_data = parsed_payload.get("data")
        if not isinstance(parse_data, dict):
            raise WechatResolveError("元宝登录态失效或没有返回视频信息")
        playable_url = str(parse_data.get("playable_url") or "")
        playable_query = parse_qs(urlparse(playable_url).query)
        export_id = (playable_query.get("eid") or [parse_data.get("wx_export_id") or ""])[0]
        general_token = (playable_query.get("token") or [""])[0]
        if not export_id or not general_token:
            raise WechatResolveError("元宝返回的播放凭证不完整，登录态可能已失效")

        rid = f"{int(time.time()):x}-{secrets.token_hex(4)}"
        feed_api = f"{WECHAT_FEED_URL}?_rid={rid}&_pageUrl=https:%2F%2Fchannels.weixin.qq.com%2Ffinder-preview%2Fpages%2Ffeed"
        referer = (
            "https://channels.weixin.qq.com/finder-preview/pages/feed"
            f"?entry_card_type=48&comment_scene=39&token={general_token}&eid={export_id}"
        )
        try:
            feed_response = self.session.post(
                feed_api,
                json={"baseReq": {"generalToken": general_token}, "exportId": export_id},
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Origin": "https://channels.weixin.qq.com",
                    "Referer": referer,
                    "User-Agent": MOBILE_UA,
                },
                timeout=self.timeout,
            )
            feed_response.raise_for_status()
            feed_payload = feed_response.json()
        except (requests.RequestException, ValueError) as error:
            raise WechatResolveError(f"视频号详情请求失败：{error}") from error

        if not isinstance(feed_payload, dict):
            raise WechatResolveError("视频号返回了无法识别的数据")
        if feed_payload.get("errCode") not in (0, None):
            raise WechatResolveError(str(feed_payload.get("errMsg") or "视频号返回错误"))
        data = feed_payload.get("data") or {}
        if not isinstance(data, dict):
            raise WechatResolveError("视频号返回的视频信息格式不正确")
        error_info = data.get("errMsg") or {}
        if not isinstance(error_info, dict):
            raise WechatResolveError("视频号返回的错误信息格式不正确")
        if error_info.get("type") not in (None, 0):
            message = "：".join(str(error_info.get(key) or "").strip() for key in ("title", "content")).strip("：")
            raise WechatResolveError(message or "此视频暂时无法播放")
        feed = data.get("feedInfo") or {}
        if not isinstance(feed, dict):
            raise WechatResolveError("视频号返回的视频信息格式不正确")
        video_url = next(
            (
                str(value).strip()
                for value in (
                    (feed.get("h264VideoInfo") or {}).get("videoUrl"),
                    feed.get("videoUrl"),
                    (feed.get("h265VideoInfo") or {}).get("videoUrl"),
                )
                if value
            ),
            "",
        )
        if not video_url:
            raise WechatResolveError("该分享页没有可下载的视频流，可能是图文、私密或登录态失效")
        author = data.get("authorInfo") or {}
        if not isinstance(author, dict):
            author = {}
        return WechatProfile(
            share_url=share_url,
            video_url=video_url,
            title=str(feed.get("description") or parse_data.get("desc") or "微信视频号").strip(),
            author=str(author.get("nickname") or parse_data.get("author") or "").strip(),
            cover_url=str(feed.get("coverUrl") or parse_data.get("cover_url") or "").strip(),
        )

    def download(self, share_text: str, item_id: str, out_dir: Path) -> VideoInfo:
        profile = self.resolve(share_text)
        out_dir.mkdir(parents=True, exist_ok=True)
        video_path = out_dir / "video.mp4"
        try:
            stream_to_file(
                [profile.video_url],
                video_path,
                self.session,
                self.timeout,
                referer=profile.share_url,
                user_agent=MOBILE_UA,
            )
        except DownloadError as error:
            raise WechatResolveError(f"视频号视频下载失败：{error}") from error
        return VideoInfo(
            aweme_id=item_id,
            title=profile.title,
            author=profile.author,
            video_path=video_path,
            # Never persist the short-lived signed playback URL or its token.
            source_url=profile.share_url,
            backend="wechat-yuanbao",
            # Cover URLs can also carry short-lived signatures; do not persist them.
            extra={"source_platform": "wechat"},
        )
