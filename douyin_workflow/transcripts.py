"""Raw ASR exports. No course-specific substitutions or invented equations."""

from __future__ import annotations

import json
from pathlib import Path


def timestamp(seconds: float, srt: bool = False) -> str:
    milliseconds = round(seconds * 1000)
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    sec, ms = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{sec:02}" + (f",{ms:03}" if srt else "")


def exports(meta: dict, segments: list[dict]) -> dict[str, str]:
    # Untrusted titles/text are plain source material; escape Markdown markup.
    def escape(value):
        return "".join("\\" + c if c in "\\`*_{}[]<>()#!|" else c for c in str(value)).replace("\n", " ")

    md = ["# " + escape(meta.get("title", "")), "", "来源：" + meta["share_url"], "",
          "语音识别初稿，未逐字校对；数字、公式和缩写需对照原视频。时间戳为识别片段边界，未提取画面文字。", ""]
    srt = []
    for i, row in enumerate(segments, 1):
        md.extend([f"**[{timestamp(row['start'])}—{timestamp(row['end'])}]({meta['share_url']}&t={int(row['start'])})**", "",
                   escape(row["text"]) or "［此段未识别出文字］", ""])
        srt.extend([str(i), f"{timestamp(row['start'], True)} --> {timestamp(row['end'], True)}", row["text"], ""])
    return {"markdown": "\n".join(md), "srt": "\n".join(srt)}


def write_exports(directory: Path, meta: dict, segments: list[dict]) -> dict[str, str]:
    content = exports(meta, segments)
    (directory / "transcript.md").write_text(content["markdown"], encoding="utf-8")
    (directory / "transcript.srt").write_text(content["srt"], encoding="utf-8")
    (directory / "segments.json").write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
    return content
