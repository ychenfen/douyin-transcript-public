"""Public Bilibili BV/short links and selected-part audio (no browser cookies)."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

from .share import ShareParseError, _URL_RE

UA = "Mozilla/5.0"
API = "https://api.bilibili.com"
PAGE_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
CDN_SUFFIXES = ("bilivideo.com", "bilivideo.cn", "akamaized.net")
MAX_AUDIO_BYTES = 512 * 1024 * 1024


class BilibiliError(RuntimeError):
    pass


def _host(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return ""
        return parsed.hostname or ""
    except ValueError:
        return ""


def _find_url(text: str) -> str:
    for match in _URL_RE.finditer(text):
        url = match.group().rstrip(".,，。)）】」")
        if _host(url) in PAGE_HOSTS | {"b23.tv"}:
            return url
    raise ShareParseError("没有找到 B 站视频链接，请使用 /video/BV... 或 b23.tv 分享链接")


def is_bilibili_share_text(text: str) -> bool:
    try:
        _find_url(text)
        return True
    except ShareParseError:
        return False


def resolve_bilibili(text: str, timeout: float = 20, session=None) -> tuple[str, str]:
    url = _find_url(text)
    own_session = session is None
    session = session or requests.Session()
    try:
        for _ in range(5):
            if _host(url) in PAGE_HOSTS:
                break
            if _host(url) != "b23.tv":
                raise ShareParseError("B 站短链跳转到了不支持的域名")
            with session.get(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=False, stream=True) as response:
                if response.status_code not in {301, 302, 303, 307, 308}:
                    raise BilibiliError("B 站短链未返回视频跳转，请使用完整 BV 链接")
                url = urljoin(url, response.headers.get("Location", ""))
        parsed = urlsplit(url)
        match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10})/?", parsed.path)
        if _host(url) not in PAGE_HOSTS or not match:
            raise ShareParseError("只支持 B 站 BV 视频链接，不支持番剧、直播或个人空间")
        values = parse_qs(parsed.query, keep_blank_values=True).get("p", ["1"])
        if len(values) != 1 or not re.fullmatch(r"[1-9][0-9]{0,3}", values[0]):
            raise ShareParseError("B 站分 P 参数必须为正整数")
        page = int(values[0])
        bvid = match.group(1)
        return f"https://www.bilibili.com/video/{bvid}?p={page}", f"bili-{bvid}-p{page}"
    except requests.RequestException:
        raise BilibiliError("B 站短链网络请求失败，请稍后重试或使用完整 BV 链接") from None
    finally:
        if own_session:
            session.close()


class BilibiliSource:
    def __init__(self, timeout: float = 20, session=None):
        self.timeout = timeout
        self.session = session or requests.Session()

    def _json(self, path: str, params: dict, referer: str) -> dict:
        try:
            with self.session.get(API + path, params=params, headers={"User-Agent": UA, "Referer": referer},
                                  timeout=self.timeout, allow_redirects=False) as response:
                response.raise_for_status()
                result = response.json()
        except (requests.RequestException, ValueError):
            raise BilibiliError("B 站接口请求失败或被限流，请稍后重试") from None
        if not isinstance(result, dict) or result.get("code") != 0 or not isinstance(result.get("data"), dict):
            raise BilibiliError("B 站未提供公开可播放的数据，视频可能不可用或需要登录")
        return result["data"]

    def metadata(self, url: str) -> dict:
        bvid = urlsplit(url).path.rsplit("/", 1)[-1]
        page = int(parse_qs(urlsplit(url).query)["p"][0])
        data = self._json("/x/web-interface/view", {"bvid": bvid}, url)
        selected = next((p for p in data.get("pages", []) if p.get("page") == page), None)
        if not selected:
            raise BilibiliError(f"视频没有第 {page} P，请检查链接中的 p 参数")
        try:
            aid, cid = int(data["aid"]), int(selected["cid"])
            duration = float(selected["duration"])
        except (KeyError, ValueError, TypeError):
            raise BilibiliError("B 站视频元数据不完整") from None
        return {"bvid": bvid, "aid": aid, "cid": cid, "page": page,
                "collection_title": data.get("title", ""), "title": selected.get("part") or data.get("title", ""),
                "author": (data.get("owner") or {}).get("name", ""), "duration_s": duration,
                "source_platform": "bilibili", "share_url": url, "backend": "bilibili-audio"}

    def download_audio(self, meta: dict, dest: Path) -> Path:
        data = self._json("/x/player/playurl", {"avid": meta["aid"], "cid": meta["cid"],
                          "qn": 16, "fnval": 16, "fourk": 0}, meta["share_url"])
        streams = (data.get("dash") or {}).get("audio") or []
        if not streams:
            raise BilibiliError("未取得公开音轨；不支持登录、付费或仅提供试看音轨的视频")
        # Never quietly accept a preview instead of the selected full part.
        try:
            play_duration = float(data.get("timelength", 0)) / 1000 or float(data["dash"].get("duration", 0))
        except (TypeError, ValueError):
            play_duration = 0
        if play_duration <= 0 or abs(play_duration - meta["duration_s"]) > 3:
            raise BilibiliError("公开音轨时长与所选分 P 不一致，可能仅提供试看内容")
        stream = min(streams, key=lambda s: int(s.get("bandwidth", 2**31)))
        urls = [stream.get("baseUrl") or stream.get("base_url")]
        urls.extend(stream.get("backupUrl") or stream.get("backup_url") or [])
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_suffix(dest.suffix + ".part")
        for url in urls:
            try:
                for _ in range(5):
                    host = _host(url or "")
                    if not any(host == suffix or host.endswith("." + suffix) for suffix in CDN_SUFFIXES):
                        raise BilibiliError("音轨地址不是受支持的 B 站 CDN")
                    url = "https:" + url[url.index(":") + 1:]
                    with self.session.get(url, headers={"User-Agent": UA, "Referer": meta["share_url"]},
                                          stream=True, timeout=self.timeout, allow_redirects=False) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            url = urljoin(url, response.headers.get("Location", ""))
                            continue
                        response.raise_for_status()
                        if any(t in response.headers.get("Content-Type", "") for t in ("text/", "json")):
                            raise BilibiliError("音轨请求返回了网页")
                        total = 0
                        with partial.open("wb") as handle:
                            for block in response.iter_content(chunk_size=1024 * 1024):
                                total += len(block)
                                if total > MAX_AUDIO_BYTES:
                                    raise BilibiliError("音轨超过单次 512 MiB 下载上限")
                                handle.write(block)
                        if not total:
                            raise BilibiliError("音轨为空")
                        partial.replace(dest)
                        return dest
            except (requests.RequestException, BilibiliError):
                # Signed URLs must not appear in logs, exceptions, or metadata.
                pass
            finally:
                partial.unlink(missing_ok=True)
        raise BilibiliError("B 站音轨下载失败，请稍后重试；可能被限流或超过单次下载上限")
