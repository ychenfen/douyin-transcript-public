"""Multi-format transcript exporters (TXT, JSON, Markdown, SRT, WebVTT).

Formats a transcription result dict (`title`, `author`, `aweme_id`, `duration_s`,
`share_url`, `sha256`, `transcript`, and optional `segments`) into:
- `to_txt`: Plain-text header + transcript
- `to_json`: Pretty-printed UTF-8 JSON
- `to_markdown`: YAML frontmatter + structured paragraphs for Obsidian / Notion / LLM context
- `to_srt`: SubRip `.srt` subtitle cues with segment or proportional timestamps
- `to_vtt`: WebVTT `.vtt` subtitle cues with segment or proportional timestamps
"""

from __future__ import annotations

import json
import re
from typing import Any

SUPPORTED_FORMATS = ("txt", "json", "md", "srt", "vtt")

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])\s*")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[，,、：:])\s*")
_MAX_CUE_CHARS = 36


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


def _extract_transcript(item: dict[str, Any]) -> str:
    return str(item.get("transcript") or item.get("text") or "").strip()


def _split_long_sentence(sentence: str, max_chars: int = _MAX_CUE_CHARS) -> list[str]:
    sentence = sentence.strip()
    if not sentence:
        return []
    if len(sentence) <= max_chars:
        return [sentence]
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(sentence) if c.strip()]
    if len(clauses) <= 1:
        return [sentence[i : i + max_chars].strip() for i in range(0, len(sentence), max_chars) if sentence[i : i + max_chars].strip()]
    merged: list[str] = []
    current = ""
    for clause in clauses:
        if not current:
            current = clause
        elif len(current) + len(clause) <= max_chars:
            current += clause
        else:
            merged.append(current)
            current = clause
    if current:
        merged.append(current)
    return merged


def split_subtitle_segments(
    transcript: str,
    duration_s: float | int | None = None,
    segments: list[dict[str, Any]] | None = None,
) -> list[tuple[float, float, str]]:
    """Return `(start_s, end_s, text)` subtitle cues.

    Uses explicit `segments` (`[{"start": ..., "end": ..., "text": ...}]`) when available;
    otherwise splits `transcript` into natural sentence/clause cues and allocates timestamps
    proportionally across `duration_s` (or an estimated speaking rate of ~4.5 chars/sec).
    """
    if segments:
        explicit: list[tuple[float, float, str]] = []
        for seg in segments:
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            start = float(seg.get("start") or 0.0)
            end = float(seg.get("end") or (start + max(1.5, len(text) / 4.5)))
            if end <= start:
                end = start + 1.0
            explicit.append((start, end, text))
        if explicit:
            return explicit

    cleaned = transcript.strip()
    if not cleaned:
        return []

    raw_sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(cleaned) if s.strip()]
    cues: list[str] = []
    for sentence in raw_sentences:
        cues.extend(_split_long_sentence(sentence))
    if not cues:
        cues = [cleaned]

    weights = [max(len(re.sub(r"\s+", "", cue)), 1) for cue in cues]
    total_weight = sum(weights)

    if duration_s is not None and float(duration_s) > 0:
        total_duration = float(duration_s)
    else:
        total_duration = max(len(cues) * 1.5, total_weight / 4.5)

    out: list[tuple[float, float, str]] = []
    cursor = 0.0
    for idx, (cue, weight) in enumerate(zip(cues, weights)):
        if idx == len(cues) - 1:
            end = max(total_duration, cursor + 0.5)
        else:
            span = total_duration * (weight / total_weight)
            end = cursor + max(0.4, span)
        # Strip trailing Chinese/English sentence-ending punctuation for cleaner subtitle lines
        cue_text = cue.rstrip("。；;") or cue
        out.append((round(cursor, 3), round(end, 3), cue_text))
        cursor = end
    return out


def _format_timestamp(seconds: float, sep: str) -> str:
    ms_total = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(ms_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def to_txt(item: dict[str, Any]) -> str:
    """Render human-readable plain text with title, metadata line, and transcript."""
    title = str(item.get("title") or "（无标题）").strip()
    duration = item.get("duration_s")
    meta_parts = [
        str(item.get("author") or "").strip(),
        f"{duration}秒" if duration else "",
    ]
    meta = " · ".join(p for p in meta_parts if p)
    transcript = _extract_transcript(item)
    lines = [title]
    if meta:
        lines.append(meta)
    lines.append("")
    if transcript:
        lines.append(transcript)
    return "\n".join(lines).strip() + "\n"


def to_json(item: dict[str, Any]) -> str:
    """Render pretty-printed UTF-8 JSON."""
    return json.dumps(item, ensure_ascii=False, indent=2) + "\n"


def to_markdown(item: dict[str, Any]) -> str:
    """Render Markdown with YAML frontmatter and structured transcript paragraphs."""
    title = str(item.get("title") or "无标题").strip()
    author = str(item.get("author") or "").strip()
    aweme_id = str(item.get("aweme_id") or "").strip()
    duration_s = item.get("duration_s")
    source_url = str(item.get("share_url") or item.get("source_url") or "").strip()
    sha256 = item.get("sha256")
    if isinstance(sha256, dict):
        sha256 = sha256.get("video") or sha256.get("transcript") or ""
    sha256_str = str(sha256 or "").strip()
    backend = str(item.get("backend") or "").strip()
    created_at = str(item.get("created_at") or "").strip()

    frontmatter = ["---", f"title: {_yaml_quote(title)}"]
    if author:
        frontmatter.append(f"author: {_yaml_quote(author)}")
    if aweme_id:
        frontmatter.append(f"aweme_id: {_yaml_quote(aweme_id)}")
    if duration_s is not None:
        frontmatter.append(f"duration_s: {duration_s}")
    if source_url:
        frontmatter.append(f"source_url: {_yaml_quote(source_url)}")
    if sha256_str:
        frontmatter.append(f"sha256: {_yaml_quote(sha256_str)}")
    if backend:
        frontmatter.append(f"backend: {_yaml_quote(backend)}")
    if created_at:
        frontmatter.append(f"created_at: {_yaml_quote(created_at)}")
    frontmatter.append("---")

    transcript = _extract_transcript(item)
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(transcript) if s.strip()] if transcript else []
    paragraphs: list[str] = []
    # Group every ~3 sentences into a readable Markdown paragraph
    for i in range(0, len(sentences), 3):
        paragraphs.append("".join(sentences[i : i + 3]))
    if not paragraphs and transcript:
        paragraphs = [transcript]

    meta_badges = []
    if author:
        meta_badges.append(f"**作者**：{author}")
    if duration_s is not None:
        meta_badges.append(f"**时长**：{duration_s}s")
    if aweme_id:
        meta_badges.append(f"**ID**：`{aweme_id}`")
    if source_url:
        meta_badges.append(f"**来源**：{source_url}")

    body = ["\n".join(frontmatter), "", f"# {title}"]
    if meta_badges:
        body.extend(["", "> " + " · ".join(meta_badges)])
    body.extend(["", "## 逐字稿", ""])
    if paragraphs:
        body.append("\n\n".join(paragraphs))
    return "\n".join(body).rstrip() + "\n"


def to_srt(item: dict[str, Any]) -> str:
    """Render SubRip (`.srt`) subtitle content."""
    transcript = _extract_transcript(item)
    cues = split_subtitle_segments(
        transcript,
        duration_s=item.get("duration_s"),
        segments=item.get("segments"),
    )
    if not cues:
        return ""
    blocks: list[str] = []
    for idx, (start, end, text) in enumerate(cues, start=1):
        blocks.append(
            f"{idx}\n{_format_timestamp(start, ',')} --> {_format_timestamp(end, ',')}\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def to_vtt(item: dict[str, Any]) -> str:
    """Render WebVTT (`.vtt`) subtitle content."""
    transcript = _extract_transcript(item)
    cues = split_subtitle_segments(
        transcript,
        duration_s=item.get("duration_s"),
        segments=item.get("segments"),
    )
    lines = ["WEBVTT", ""]
    for idx, (start, end, text) in enumerate(cues, start=1):
        lines.append(f"{idx}")
        lines.append(f"{_format_timestamp(start, '.')} --> {_format_timestamp(end, '.')}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_item(item: dict[str, Any], fmt: str = "txt") -> str:
    """Dispatch `item` formatting by `fmt` (`txt`, `json`, `md`, `srt`, `vtt`)."""
    normalized = fmt.strip().lower().lstrip(".")
    if normalized in ("markdown", "md"):
        return to_markdown(item)
    if normalized == "srt":
        return to_srt(item)
    if normalized in ("vtt", "webvtt"):
        return to_vtt(item)
    if normalized == "json":
        return to_json(item)
    if normalized in ("txt", "text"):
        return to_txt(item)
    raise ValueError(f"不支持的导出格式：{fmt}（可选：{', '.join(SUPPORTED_FORMATS)}）")
