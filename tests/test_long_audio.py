import json
import wave
from types import SimpleNamespace

import pytest

np = pytest.importorskip('numpy')

from douyin_workflow import transcribe as asr


def wav_file(path, seconds=75, amplitude=100):
    with wave.open(str(path), 'wb') as handle:
        handle.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        for _ in range(seconds):
            handle.writeframes(np.full(16000, amplitude, dtype='<i2').tobytes())
    return path


class Recognizer:
    def __init__(self, fail_at=None):
        self.count, self.fail_at = 0, fail_at

    def create_stream(self):
        return SimpleNamespace(accept_waveform=lambda sr, data: None, result=SimpleNamespace(text='测试。'))

    def decode_stream(self, stream):
        self.count += 1
        if self.count == self.fail_at:
            raise RuntimeError('simulated interruption')


def test_long_wav_read_is_bounded_and_coverage_contiguous(tmp_path, monkeypatch):
    path = wav_file(tmp_path/'long.wav', seconds=185)
    original = wave.Wave_read.readframes
    reads = []
    def guarded(self, count):
        reads.append(count)
        assert count <= 16000 * 30.02
        return original(self, count)
    monkeypatch.setattr(wave.Wave_read, 'readframes', guarded)
    chunks = list(asr._pcm_chunks(path))
    assert chunks[0][0] == 0 and chunks[-1][1] == 185
    assert all(a[1] == b[0] for a, b in zip(chunks, chunks[1:]))
    assert all(end-start <= 30 for start, end, _ in chunks)
    assert sum(len(data) for _, _, data in chunks) == 185*16000
    assert len(reads) > 1


def test_checkpoint_resumes_after_interruption_and_discards_partial_line(tmp_path, monkeypatch):
    path = wav_file(tmp_path/'audio.wav')
    checkpoint = tmp_path/'checkpoints'
    broken = Recognizer(fail_at=2)
    monkeypatch.setattr(asr, '_sherpa_recognizer', lambda: broken)
    with pytest.raises(RuntimeError, match='interruption'):
        asr.transcribe_segments(path, checkpoint_dir=checkpoint)
    first = json.loads((checkpoint/'segments.jsonl').read_text())
    with (checkpoint/'segments.jsonl').open('a') as handle:
        handle.write('{"start":')
    working = Recognizer()
    monkeypatch.setattr(asr, '_sherpa_recognizer', lambda: working)
    rows = asr.transcribe_segments(path, checkpoint_dir=checkpoint)
    assert rows[0] == first and rows[-1]['end'] == 75
    assert working.count == len(rows)-1
    working.count = 0
    assert asr.transcribe_segments(path, checkpoint_dir=checkpoint) == rows
    assert working.count == 0
    asr.transcribe_segments(path, checkpoint_dir=checkpoint, force=True)
    assert working.count == len(rows)


def test_changed_audio_invalidates_checkpoint(tmp_path, monkeypatch):
    path = wav_file(tmp_path/'audio.wav', seconds=2)
    recognizer = Recognizer()
    monkeypatch.setattr(asr, '_sherpa_recognizer', lambda: recognizer)
    asr.transcribe_segments(path, checkpoint_dir=tmp_path/'state')
    wav_file(path, seconds=2, amplitude=200)
    asr.transcribe_segments(path, checkpoint_dir=tmp_path/'state')
    assert recognizer.count == 2


def test_existing_text_api_keeps_returning_string(tmp_path, monkeypatch):
    path = wav_file(tmp_path/'audio.wav', seconds=2)
    monkeypatch.setattr(asr, '_sherpa_recognizer', lambda: Recognizer())
    assert asr.transcribe(path, asr.SHERPA_MODEL) == '测试。'


@pytest.mark.parametrize('fail', [False, True])
def test_optional_model_release_after_success_or_failure(monkeypatch, fail):
    class Model:
        def generate(self, **kwargs):
            if fail:
                raise RuntimeError('failure')
            return [{'text': '结果'}]

    model = Model()
    monkeypatch.setenv('DOUYIN_RELEASE_ASR_AFTER_JOB', '1')
    monkeypatch.setattr(asr, '_models', {('paraformer-zh', 'cpu'): model})
    monkeypatch.setattr(asr, '_load', lambda *a: model)
    if fail:
        with pytest.raises(RuntimeError):
            asr.transcribe(None, 'paraformer-zh', 'cpu')
    else:
        assert asr.transcribe(None, 'paraformer-zh', 'cpu') == '结果'
    assert asr._models == {}


def test_models_remain_cached_by_default(monkeypatch):
    model = SimpleNamespace(generate=lambda **kwargs: [{'text': '结果'}])
    monkeypatch.delenv('DOUYIN_RELEASE_ASR_AFTER_JOB', raising=False)
    monkeypatch.setattr(asr, '_models', {('paraformer-zh', 'cpu'): model})
    monkeypatch.setattr(asr, '_load', lambda *a: model)
    assert asr.transcribe(None, 'paraformer-zh', 'cpu') == '结果'
    assert len(asr._models) == 1
