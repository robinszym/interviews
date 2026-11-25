"""Local web app for interview transcripts: history, import, playback, corrections, export.

Listens on 127.0.0.1 only. Interviews are transcribed one at a time by a child
process (interviews.pipeline), so the models' memory is released after each one.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import export, store

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"


class Worker(threading.Thread):
    """Runs queued interviews through the pipeline, oldest first, one at a time."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.wake = threading.Event()
        self._lock = threading.Lock()
        self._current: Optional[tuple[str, subprocess.Popen]] = None

    def run(self) -> None:
        while True:
            iid = store.next_queued()
            if iid is None:
                self.wake.wait(timeout=5)
                self.wake.clear()
                continue
            self._process(iid)

    def _process(self, iid: str) -> None:
        folder = store.folder(iid)
        store.update_status(iid, status="processing", stage="Préparation", progress=0.0, error=None)
        script = os.environ.get("INTERVIEWS_PIPELINE")  # dev hook: run an alternative pipeline script
        argv = [sys.executable, script] if script else [sys.executable, "-m", "interviews.pipeline"]
        with self._lock:
            proc = subprocess.Popen([*argv, str(folder)], cwd=PACKAGE_DIR.parent)
            self._current = (iid, proc)
        code = proc.wait()
        with self._lock:
            self._current = None
        if not folder.exists():
            return  # deleted while processing
        if code != 0 and store.read_status(iid).get("status") != "error":
            store.update_status(iid, status="error", error=f"Le traitement s'est arrêté de façon inattendue (code {code}).")

    def cancel(self, iid: str) -> None:
        with self._lock:
            if self._current and self._current[0] == iid:
                self._current[1].kill()

    def cancel_current(self) -> None:
        with self._lock:
            if self._current:
                self._current[1].kill()


class SpeakerChange(BaseModel):
    id: str
    name: Optional[str] = None
    questions: Optional[bool] = None


class SentenceChange(BaseModel):
    id: int
    text: Optional[str] = None
    speaker: Optional[str] = None
    favorite: Optional[bool] = None


class InterviewChange(BaseModel):
    title: Optional[str] = None
    folder: Optional[str] = None
    speakers: Optional[list[SpeakerChange]] = None
    sentences: Optional[list[SentenceChange]] = None
    delete_sentences: Optional[list[int]] = None


class SpeakersRun(BaseModel):
    count: Optional[int] = None  # None: let the app find the number of voices


app = FastAPI(title="Interviews", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
worker = Worker()
edit_lock = threading.Lock()


@app.middleware("http")
async def revalidate_page_assets(request, call_next):
    """Make the browser check for a newer page/script on every load, so an app update is never masked by its cache."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def load_or_404(iid: str) -> dict:
    interview = store.load(iid)
    if interview is None:
        raise HTTPException(404, "Interview introuvable.")
    return interview


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/shutdown")
def shutdown() -> dict:
    """Quit from the page: stop a running transcription, answer, then end the process."""
    worker.cancel_current()
    threading.Timer(0.4, lambda: os._exit(0)).start()
    return {"stopping": True}


@app.get("/api/health")
def health() -> dict:
    # the launcher compares this with its own version to restart an outdated instance
    return {"app": "interviews", "version": int(os.environ.get("INTERVIEWS_VERSION") or 0)}


@app.get("/api/interviews")
def list_interviews() -> list[dict]:
    return store.list_summaries()


@app.post("/api/interviews")
def import_interview(file: UploadFile = File(...)) -> dict:
    iid = store.create(file.filename or "Interview", file.file)
    worker.wake.set()
    return {"id": iid}


@app.get("/api/interviews/{iid}")
def get_interview(iid: str) -> dict:
    return load_or_404(iid)


@app.patch("/api/interviews/{iid}")
def change_interview(iid: str, change: InterviewChange) -> dict:
    load_or_404(iid)
    with edit_lock:
        if change.title is not None:
            store.update_meta(iid, title=change.title.strip() or "Sans titre")
        if change.folder is not None:
            store.update_meta(iid, folder=" ".join(change.folder.split()))
        if change.speakers or change.sentences or change.delete_sentences:
            transcript = store.read_transcript(iid)
            if transcript is None:
                raise HTTPException(409, "La transcription n'est pas terminée.")
            speakers = {s["id"]: s for s in transcript["speakers"]}
            for sc in change.speakers or []:
                speaker = speakers.get(sc.id)
                if speaker is None:
                    speaker = speakers[sc.id] = {"id": sc.id, "name": sc.id, "questions": False}
                    transcript["speakers"].append(speaker)
                if sc.name is not None and sc.name.strip():
                    speaker["name"] = sc.name.strip()
                if sc.questions is not None:
                    speaker["questions"] = sc.questions
            sentences = {s["id"]: s for s in transcript["sentences"]}
            for sc in change.sentences or []:
                sentence = sentences.get(sc.id)
                if sentence is None:
                    continue
                if sc.text is not None:
                    sentence["text"] = " ".join(sc.text.split())
                if sc.speaker is not None and sc.speaker in speakers:
                    sentence["speaker"] = sc.speaker
                if sc.favorite:
                    sentence["favorite"] = True
                elif sc.favorite is False:
                    sentence.pop("favorite", None)
            if change.delete_sentences:
                gone = set(change.delete_sentences)
                removed = [s for s in transcript["sentences"] if s["id"] in gone]
                if removed:  # kept aside so the deletion can be undone, even after a reload
                    transcript.setdefault("trash", []).append(removed)
                transcript["sentences"] = [s for s in transcript["sentences"] if s["id"] not in gone]
            store.write_transcript(iid, transcript)
    return load_or_404(iid)


@app.delete("/api/interviews/{iid}", status_code=204)
def delete_interview(iid: str) -> Response:
    load_or_404(iid)
    worker.cancel(iid)
    store.delete(iid)
    return Response(status_code=204)


@app.post("/api/interviews/{iid}/retry")
def retry_interview(iid: str) -> dict:
    if load_or_404(iid).get("status") != "error":
        raise HTTPException(409, "Cette interview n'est pas en erreur.")
    store.update_status(iid, status="queued", stage=None, progress=0.0, error=None)
    worker.wake.set()
    return load_or_404(iid)


@app.post("/api/interviews/{iid}/restore")
def restore_block(iid: str) -> dict:
    """Undo the last block deletion."""
    load_or_404(iid)
    with edit_lock:
        transcript = store.read_transcript(iid)
        if not transcript or not transcript.get("trash"):
            raise HTTPException(409, "Aucun bloc supprimé à rétablir.")
        restored = transcript["trash"].pop()
        transcript["sentences"] = sorted(transcript["sentences"] + restored, key=lambda s: (s["start"], s["id"]))
        store.write_transcript(iid, transcript)
    return load_or_404(iid)


@app.post("/api/interviews/{iid}/speakers")
def rerun_speakers(iid: str, body: SpeakersRun) -> dict:
    """Redo only the voice recognition, optionally with the number of voices given by the user."""
    if load_or_404(iid).get("status") != "done":
        raise HTTPException(409, "La transcription n'est pas terminée.")
    if body.count is not None and not 1 <= body.count <= 10:
        raise HTTPException(400, "Le nombre d'intervenants doit être entre 1 et 10.")
    store.update_status(iid, status="queued", job="voices", speakers_count=body.count, stage=None, progress=0.0, error=None)
    worker.wake.set()
    return load_or_404(iid)


@app.get("/api/interviews/{iid}/audio")
def interview_audio(iid: str) -> FileResponse:
    load_or_404(iid)
    path = store.folder(iid) / "audio.m4a"
    if not path.exists():
        raise HTTPException(404, "Audio pas encore prêt.")
    return FileResponse(path, media_type="audio/mp4")


@app.get("/api/interviews/{iid}/export")
def export_interview(iid: str, format: str = "docx") -> Response:
    interview = load_or_404(iid)
    if not interview.get("sentences"):
        raise HTTPException(409, "La transcription n'est pas terminée.")
    if format == "txt":
        body, media_type = export.to_txt(interview).encode("utf-8"), "text/plain; charset=utf-8"
    elif format == "docx":
        body = export.to_docx(interview)
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        raise HTTPException(400, "Format inconnu.")
    filename = quote(f"{interview['title']}.{format}")
    return Response(body, media_type=media_type, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Interviews web app")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    store.LIBRARY.mkdir(parents=True, exist_ok=True)
    store.requeue_interrupted()
    worker.start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
