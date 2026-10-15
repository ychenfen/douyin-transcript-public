import json
import wave
from types import SimpleNamespace

import pytest
import requests

from douyin_workflow import pipeline
from douyin_workflow.bilibili import BilibiliError, BilibiliSource, resolve_bilibili, is_bilibili_share_text
from douyin_workflow.config import Settings
from douyin_workflow.server import App
from douyin_workflow.share import ShareParseError

BV = "BV1VE411A7Ak"
URL = f"https://www.bilibili.com/video/{BV}?p=16"


class Response:
    def __init__(self, data=None, status=200, headers=None, content=b"audio"):
        self.data, self.status_code, self.headers, self.content = data, status, headers or {}, content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("https://x.bilivideo.com/audio?secret=DO_NOT_LEAK")

    def json(self):
        return {"code": 0, "data": self.data}

    def iter_content(self, chunk_size):
        yield self.content


class Session:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.replies.pop(0)

    def close(self):
        pass


def test_parts_are_distinct_and_tracking_is_removed():
    assert resolve_bilibili('【课程】' + URL + '&vd_source=private')[0] == URL
    assert resolve_bilibili(URL)[1] == f'bili-{BV}-p16'
    assert resolve_bilibili(URL.replace('p=16', 'p=1'))[1] != resolve_bilibili(URL)[1]
    assert pipeline.resolve_source(URL) == (URL, f'bili-{BV}-p16', 'bilibili')


@pytest.mark.parametrize('query', ['p=0', 'p=-1', 'p=abc', 'p=', 'p=1&p=2', 'p=../x'])
def test_bad_part_never_falls_back_to_part_one(query):
    with pytest.raises(ShareParseError):
        resolve_bilibili(f'https://www.bilibili.com/video/{BV}?{query}')


@pytest.mark.parametrize('host', ['www.bilibili.com.evil.test', 'bilibili.com@127.0.0.1', '127.0.0.1'])
def test_reject_lookalike_hosts(host):
    assert not is_bilibili_share_text(f'https://{host}/video/{BV}')


def test_shortlink_retains_part_and_rejects_external_redirect():
    session = Session([Response(status=302, headers={'Location': URL})])
    assert resolve_bilibili('https://b23.tv/test', session=session)[0] == URL
    assert session.calls[0][1]['allow_redirects'] is False
    session = Session([Response(status=302, headers={'Location': 'http://127.0.0.1/private'})])
    with pytest.raises(ShareParseError):
        resolve_bilibili('https://b23.tv/test', session=session)
    assert len(session.calls) == 1


def test_selected_cid_title_and_missing_part():
    data = {'aid': 7, 'title': '合集', 'owner': {'name': '老师'}, 'pages': [
        {'page': 1, 'cid': 10, 'part': '2009', 'duration': 12},
        {'page': 16, 'cid': 20, 'part': '2024', 'duration': 7648},
    ]}
    source = BilibiliSource(session=Session([Response(data), Response(data)]))
    meta = source.metadata(URL)
    assert (meta['cid'], meta['title'], meta['duration_s']) == (20, '2024', 7648)
    with pytest.raises(BilibiliError, match='没有第'):
        source.metadata(URL.replace('p=16', 'p=17'))


def playback(duration=60):
    return {'timelength': duration * 1000, 'dash': {'audio': [
        {'bandwidth': 100, 'baseUrl': 'https://a.bilivideo.com/high?token=DO_NOT_LEAK'},
        {'bandwidth': 10, 'base_url': 'https://a.bilivideo.com/low?token=DO_NOT_LEAK'},
    ]}}


def test_lowest_audio_stream_and_no_signed_urls_in_metadata(tmp_path):
    session = Session([Response(playback()), Response(content=b'complete audio')])
    meta = {'aid': 7, 'cid': 20, 'share_url': URL, 'duration_s': 60}
    path = BilibiliSource(session=session).download_audio(meta, tmp_path/'source.m4a')
    assert '/low?' in session.calls[1][0]
    assert session.calls[0][1]['params']['cid'] == 20
    assert path.read_bytes() == b'complete audio'
    assert 'DO_NOT_LEAK' not in json.dumps(meta)


def test_preview_rejected_before_download(tmp_path):
    session = Session([Response(playback(duration=6))])
    with pytest.raises(BilibiliError, match='试看'):
        BilibiliSource(session=session).download_audio({'aid': 7, 'cid': 20, 'share_url': URL, 'duration_s': 600}, tmp_path/'audio')
    assert len(session.calls) == 1


def test_cdn_redirect_cannot_access_private_host_and_errors_are_redacted(tmp_path):
    session = Session([Response(playback()), Response(status=302, headers={'Location': 'http://127.0.0.1/private'})])
    with pytest.raises(BilibiliError) as exc:
        BilibiliSource(session=session).download_audio({'aid': 7, 'cid': 20, 'share_url': URL, 'duration_s': 60}, tmp_path/'audio')
    assert len(session.calls) == 2
    assert 'DO_NOT_LEAK' not in str(exc.value)
    assert not (tmp_path/'audio.part').exists()


def test_pipeline_exports_cache_and_public_server(monkeypatch, tmp_path):
    class Source:
        calls = 0
        session = SimpleNamespace(close=lambda: None)

        def __init__(self, timeout):
            pass

        def metadata(self, url):
            return {'cid': 20, 'title': '2024', 'duration_s': 1, 'page': 16,
                    'share_url': url, 'source_platform': 'bilibili'}

        def download_audio(self, meta, dest):
            Source.calls += 1
            dest.write_bytes(b'audio')
            return dest

    def extract(src, dest):
        with wave.open(str(dest), 'wb') as f:
            f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            f.writeframes(b'\0\0'*16000)
        return dest

    monkeypatch.setattr(pipeline, 'BilibiliSource', Source)
    monkeypatch.setattr(pipeline, 'extract_audio', extract)
    monkeypatch.setattr(pipeline, 'transcribe_segments', lambda *a, **k: [{'start': 0, 'end': 1, 'text': '带宽与吞吐量。'}])
    settings = Settings(data_dir=tmp_path)
    result = pipeline.link_to_text(URL, settings=settings)
    assert result['page'] == 16 and result['cached'] is False
    assert '00:00:00,000 --> 00:00:01,000' in result['exports']['srt']
    assert 'p=16&t=0' in result['exports']['markdown']
    assert pipeline.link_to_text(URL, settings=settings)['cached'] is True
    assert Source.calls == 1
    app = App(settings=settings, require_auth=False)
    assert app.authorized_for_text(None, URL)
    payload = app.transcribe(URL)
    assert payload['source_platform'] == 'bilibili' and payload['segments'] == result['segments']
    assert not payload['video_available']
    assert not app.authorized_for_text(None, 'https://weixin.qq.com/sph/example')
    app.jobs.pool.shutdown()


def test_expected_error_is_returned_to_clients(monkeypatch):
    def fail(*args, **kwargs):
        raise BilibiliError('无法获取公开音轨')
    monkeypatch.setattr(pipeline, 'bilibili_to_text', fail)
    assert pipeline.run_safe(URL)['error'] == 'bilibili_unavailable'
