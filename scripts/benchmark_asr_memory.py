"""Linux-only real-model RSS comparison using a short existing PCM16 WAV sample.

PYTHONPATH=. python scripts/benchmark_asr_memory.py /path/to/audio.wav
Does not restart services or alter their environment files.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time
import wave

from douyin_workflow import transcribe as asr


def rss_mib():
    pages = int(Path('/proc/self/statm').read_text().split()[1])
    return round(pages * os.sysconf('SC_PAGE_SIZE') / 1024**2, 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('wav', type=Path)
    parser.add_argument('--seconds', type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 120:
        parser.error('--seconds must be between 1 and 120')
    with tempfile.TemporaryDirectory(prefix='asr-memory-') as folder:
        sample = Path(folder)/'sample.wav'
        with wave.open(str(args.wav)) as source, wave.open(str(sample), 'wb') as target:
            target.setparams(source.getparams())
            target.writeframes(source.readframes(args.seconds * source.getframerate()))
        report = {'baseline_rss_mib': rss_mib(), 'sample_seconds': args.seconds}
        os.environ['DOUYIN_RELEASE_ASR_AFTER_JOB'] = '0'
        started = time.monotonic()
        text = asr.transcribe(sample, asr.SHERPA_MODEL, 'cpu')
        report.update(warm_rss_mib=rss_mib(), first_run_s=round(time.monotonic()-started, 2))
        asr.release_models()
        report['released_rss_mib'] = rss_mib()
        os.environ['DOUYIN_RELEASE_ASR_AFTER_JOB'] = '1'
        second = asr.transcribe(sample, asr.SHERPA_MODEL, 'cpu')
        report.update(automatic_release_rss_mib=rss_mib(), cached_models=len(asr._models),
                      same_transcript=text == second, transcript_characters=len(text))
        assert text and text == second and not asr._models
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
