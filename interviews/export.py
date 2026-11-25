"""Word and plain-text exports of an interview transcript."""

from __future__ import annotations

import io
from datetime import datetime

MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
          "août", "septembre", "octobre", "novembre", "décembre"]


def timecode(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def paragraphs(interview: dict) -> list[tuple[dict, float, str]]:
    """Consecutive sentences by the same speaker, as (speaker, start, text)."""
    speakers = {s["id"]: s for s in interview["speakers"]}
    unknown = {"name": "?", "questions": False}
    result: list[tuple[dict, float, str]] = []
    for sentence in interview["sentences"]:
        if not sentence["text"]:
            continue
        speaker = speakers.get(sentence["speaker"], unknown)
        if result and result[-1][0] is speaker:
            previous = result[-1]
            result[-1] = (speaker, previous[1], f"{previous[2]} {sentence['text']}")
        else:
            result.append((speaker, sentence["start"], sentence["text"]))
    return result


def subtitle(interview: dict) -> str:
    created = datetime.fromisoformat(interview["created_at"])
    parts = [f"{created.day} {MONTHS[created.month - 1]} {created.year}"]
    if interview.get("duration"):
        parts.append(f"durée {timecode(round(interview['duration']))}")
    return " · ".join(parts)


def to_txt(interview: dict) -> str:
    lines = [interview["title"], subtitle(interview), ""]
    for speaker, start, text in paragraphs(interview):
        lines += [f"[{timecode(start)}] {speaker['name']}", text, ""]
    return "\n".join(lines)


def to_docx(interview: dict) -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor

    grey = RGBColor(0x80, 0x80, 0x80)
    document = Document()
    document.add_heading(interview["title"], level=1)
    info = document.add_paragraph().add_run(subtitle(interview))
    info.font.color.rgb = grey

    for speaker, start, text in paragraphs(interview):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(10)
        stamp = paragraph.add_run(f"{timecode(start)}   ")
        stamp.font.size = Pt(9)
        stamp.font.color.rgb = grey
        paragraph.add_run(text).bold = speaker["questions"]
        if not speaker["questions"]:
            who = paragraph.add_run(f"\n— {speaker['name']}")
            who.italic = True
            who.font.size = Pt(9)
            who.font.color.rgb = grey

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
