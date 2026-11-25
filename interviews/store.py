"""Interview library on disk.

Each interview is a folder under LIBRARY:
  meta.json        id, title, original file name, import date   (edited by the server)
  status.json      queued / processing / done / error, progress  (written by the pipeline)
  transcript.json  speakers and timed sentences                (created by the pipeline, edited by the server)
  source.<ext>     the imported file, removed once transcription succeeds
  audio.m4a        playback copy
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Optional

LIBRARY = Path(os.environ.get("INTERVIEWS_LIBRARY") or Path.home() / ".qwen-transcribe" / "library")
_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")


def folder(iid: str) -> Path:
    if not _ID_RE.match(iid):
        raise ValueError(f"invalid interview id: {iid!r}")
    return LIBRARY / iid


def read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def write_json(path: Path, data: dict) -> None:
    """Write atomically so a reader never sees a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_status(iid: str) -> dict:
    return read_json(folder(iid) / "status.json") or {}


def update_status(iid: str, **fields) -> None:
    path = folder(iid) / "status.json"
    write_json(path, {**(read_json(path) or {}), **fields})


def update_meta(iid: str, **fields) -> None:
    path = folder(iid) / "meta.json"
    write_json(path, {**(read_json(path) or {}), **fields})


def read_transcript(iid: str) -> Optional[dict]:
    return read_json(folder(iid) / "transcript.json")


def write_transcript(iid: str, transcript: dict) -> None:
    write_json(folder(iid) / "transcript.json", transcript)


def source_path(iid: str) -> Optional[Path]:
    return next(folder(iid).glob("source*"), None)


def create(filename: str, stream: BinaryIO) -> str:
    now = datetime.now()
    iid = now.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    target = folder(iid)
    target.mkdir(parents=True)
    suffix = Path(filename).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        suffix = ""
    with (target / f"source{suffix}").open("wb") as out:
        shutil.copyfileobj(stream, out, 1024 * 1024)
    write_json(target / "meta.json", {
        "id": iid,
        "title": Path(filename).stem or "Interview",
        "source_name": Path(filename).name,
        "created_at": now.isoformat(timespec="seconds"),
    })
    write_json(target / "status.json", {"status": "queued", "stage": None, "progress": 0.0, "error": None})
    return iid


def ids() -> list[str]:
    """Interview ids, newest first."""
    if not LIBRARY.exists():
        return []
    found = [p.name for p in LIBRARY.iterdir() if _ID_RE.match(p.name) and (p / "meta.json").exists()]
    return sorted(found, reverse=True)


def summary(iid: str) -> dict:
    return {**(read_json(folder(iid) / "meta.json") or {}), **read_status(iid)}


def list_summaries() -> list[dict]:
    return [summary(iid) for iid in ids()]


def load(iid: str) -> Optional[dict]:
    try:
        if not (folder(iid) / "meta.json").exists():
            return None
    except ValueError:
        return None
    transcript = read_transcript(iid) or {}
    return {**summary(iid), "speakers": transcript.get("speakers"), "sentences": transcript.get("sentences")}


def next_queued() -> Optional[str]:
    """Oldest interview waiting for transcription."""
    return next((iid for iid in reversed(ids()) if read_status(iid).get("status") == "queued"), None)


def requeue_interrupted() -> None:
    """Interviews left 'processing' when the app was closed start over."""
    for iid in ids():
        if read_status(iid).get("status") == "processing":
            update_status(iid, status="queued", stage=None, progress=0.0)


def delete(iid: str) -> None:
    shutil.rmtree(folder(iid), ignore_errors=True)
