"""Transcribe one interview folder into transcript.json.

Run as `python -m interviews.pipeline <folder>`; progress is written to status.json.

1. afconvert makes the playback copy (audio.m4a) and a 16 kHz mono WAV for the models.
2. Qwen3-ASR transcribes the audio in ~30 s chunks.
3. Qwen3-ForcedAligner times every word of each chunk; the timed words are put back
   on the punctuated text and grouped into sentences.
4. Each sentence gets a voice embedding; sentences are clustered into speakers and the
   speaker asking the most questions becomes the journalist.
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import store

SAMPLE_RATE = 16000
ASR_MODEL = os.environ.get("INTERVIEWS_ASR_MODEL", "Qwen/Qwen3-ASR-1.7B")
ALIGNER_MODEL = os.environ.get("INTERVIEWS_ALIGNER_MODEL", "Qwen/Qwen3-ForcedAligner-0.6B")
MODELS_DIR = Path(os.environ.get("INTERVIEWS_MODELS") or Path.home() / ".qwen-transcribe" / "models")
EMBEDDING_MODEL = MODELS_DIR / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"

SENTENCE_END = (".", "?", "!", "…")
CLOSING_MARKS = "»\"”’')]"
OPENING_MARKS = "«“‘([¿¡—–"
MAX_PAUSE_IN_SENTENCE = 1.2     # seconds of silence that always end a sentence
MAX_WORDS_IN_SENTENCE = 60
# Voice clustering, tuned on real and synthetic voices with the eres2net embedding model.
SAME_SPEAKER_SIMILARITY = 0.4   # cosine similarity above which two groups of sentences are one voice
RELIABLE_SENTENCE_SECONDS = 2.0 # shorter sentences don't shape the voices, they only join the nearest one
MIN_SPEAKER_SECONDS = 4.0       # a "voice" with less speech than this is folded into its closest neighbour

Progress = Callable[[str, float], None]


class UserFacingError(Exception):
    """An error whose message can be shown as is in the app."""


@dataclass
class Word:
    text: str
    start: Optional[float]
    end: Optional[float]


@dataclass
class Sentence:
    text: str
    start: float
    end: float


# ---------- Audio ----------

def afconvert(*args: str) -> None:
    subprocess.run(["afconvert", *args], check=True, capture_output=True)


def prepare_audio(source: Path, folder: Path) -> Path:
    """Create audio.m4a (playback) and work16k.wav (models); return the WAV path."""
    wav = folder / "work16k.wav"
    try:
        afconvert("-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", str(source), str(wav))
    except subprocess.CalledProcessError as error:
        raise UserFacingError("macOS ne parvient pas à lire l'audio de ce fichier (format non pris en charge).") from error
    playback = folder / "audio.m4a"
    if not playback.exists():
        try:
            afconvert("-f", "m4af", "-d", "aac", str(source), str(playback))
        except subprocess.CalledProcessError:
            # e.g. sample rates AAC can't encode: fall back to the 16 kHz copy
            afconvert("-f", "m4af", "-d", "aac", str(wav), str(playback))
    return wav


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


# ---------- Transcription and word timing ----------

def transcribe_chunks(audio: np.ndarray, progress: Progress) -> tuple[list[dict], str]:
    from mlx_qwen3_asr import transcribe

    def on_progress(event: dict) -> None:
        if event.get("event") in {"chunk_completed", "completed"}:
            progress("Transcription", float(event.get("progress") or 0.0))

    result = transcribe(audio, model=ASR_MODEL, return_chunks=True, on_progress=on_progress)
    return result.chunks or [], result.language


def release_asr_model() -> None:
    """Free the ASR model before loading the aligner (best effort: private API)."""
    try:
        import mlx.core as mx
        from mlx_qwen3_asr.load_models import _ModelHolder

        _ModelHolder.clear()
        mx.clear_cache()
    except Exception:  # noqa: BLE001 - memory hygiene only
        pass


def timed_words(chunk_text: str, units_per_token: list[int], aligned: list, offset: float, chunk_end: float) -> list[Word]:
    """Give each whitespace token of the punctuated text the time span of its aligner units.

    The aligner strips punctuation, so tokens like "?" or "«" have no unit of their own:
    closing marks are glued to the previous word and opening marks to the next one,
    which keeps French spacing ("présenter ?", "« Oui »").
    """
    words: list[Word] = []
    prefix: list[str] = []
    cursor = 0
    for token, count in zip(chunk_text.split(), units_per_token):
        units = aligned[cursor:cursor + count]
        cursor += count
        if count == 0:
            if words and not prefix and not all(ch in OPENING_MARKS for ch in token):
                words[-1].text += f" {token}"
            else:
                prefix.append(token)
            continue
        text = " ".join([*prefix, token])
        prefix.clear()
        if units:
            words.append(Word(text, offset + units[0].start_time, offset + units[-1].end_time))
        else:
            words.append(Word(text, None, None))
    if prefix and words:
        words[-1].text += " " + " ".join(prefix)
    fill_missing_times(words, offset, chunk_end)
    return words


def fill_missing_times(words: list[Word], low: float, high: float) -> None:
    """Spread words the aligner didn't time evenly over the gap between their timed neighbours."""
    i = 0
    while i < len(words):
        if words[i].start is not None:
            i += 1
            continue
        j = i
        while j < len(words) and words[j].start is None:
            j += 1
        before = words[i - 1].end if i > 0 else low
        after = max(before, words[j].start if j < len(words) else high)
        step = (after - before) / (j - i)
        for k in range(i, j):
            words[k].start, words[k].end = before + (k - i) * step, before + (k - i + 1) * step
        i = j


def align_words(audio: np.ndarray, chunks: list[dict], fallback_language: str, progress: Progress) -> list[Word]:
    from mlx_qwen3_asr import ForcedAligner
    from mlx_qwen3_asr.forced_aligner import ForcedAlignTextProcessor

    aligner = ForcedAligner(ALIGNER_MODEL)
    words: list[Word] = []
    for index, chunk in enumerate(chunks):
        text = (chunk.get("text") or "").strip()
        start, end = float(chunk["start"]), float(chunk["end"])
        if text:
            language = chunk.get("language") or fallback_language
            if not language or language == "unknown":
                language = fallback_language
            tokens = text.split()
            units_per_token = [len(ForcedAlignTextProcessor.tokenize_text(token, language)) for token in tokens]
            samples = audio[int(round(start * SAMPLE_RATE)):int(round(end * SAMPLE_RATE))]
            aligned = aligner.align(samples, text, language) if sum(units_per_token) else []
            words.extend(timed_words(text, units_per_token, aligned, start, end))
        progress("Horodatage", (index + 1) / len(chunks))
    return words


def split_sentences(words: list[Word]) -> list[Sentence]:
    sentences: list[Sentence] = []
    current: list[Word] = []

    def flush() -> None:
        if current:
            sentences.append(Sentence(" ".join(w.text for w in current), current[0].start, current[-1].end))
            current.clear()

    for word in words:
        if current and word.start - current[-1].end > MAX_PAUSE_IN_SENTENCE:
            flush()
        current.append(word)
        if word.text.rstrip(CLOSING_MARKS).endswith(SENTENCE_END) or len(current) >= MAX_WORDS_IN_SENTENCE:
            flush()
    flush()
    return sentences


# ---------- Speakers ----------

def voice_embeddings(audio: np.ndarray, sentences: list[Sentence], progress: Progress) -> np.ndarray:
    import sherpa_onnx

    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMBEDDING_MODEL), num_threads=2)
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
    min_samples = SAMPLE_RATE // 2
    embeddings = []
    for index, sentence in enumerate(sentences):
        samples = audio[int(max(0.0, sentence.start - 0.1) * SAMPLE_RATE):int((sentence.end + 0.1) * SAMPLE_RATE)]
        if len(samples) < min_samples:
            samples = np.pad(samples, (0, min_samples - len(samples)))
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=samples)
        stream.input_finished()
        vector = np.asarray(extractor.compute(stream), dtype=np.float32)
        embeddings.append(vector / (np.linalg.norm(vector) + 1e-9))
        if index % 20 == 0:
            progress("Reconnaissance des voix", index / len(sentences))
    return np.stack(embeddings)


def cluster_voices(embeddings: np.ndarray, durations: np.ndarray, threshold: float = SAME_SPEAKER_SIMILARITY,
                   count: Optional[int] = None) -> np.ndarray:
    """Average-linkage clustering of the longer sentences; every sentence then joins its nearest voice.

    With `count`, merging goes on until exactly that many voices are left (the user knows best).
    """
    reliable = np.flatnonzero(durations >= RELIABLE_SENTENCE_SECONDS)
    if len(reliable) == 0:
        reliable = np.arange(len(embeddings))
    points = embeddings[reliable]
    similarity = points @ points.T
    np.fill_diagonal(similarity, -np.inf)
    sizes = np.ones(len(points))
    members = [[i] for i in range(len(points))]
    active = np.ones(len(points), dtype=bool)
    target = 1 if count is None else max(1, min(count, len(points)))
    while active.sum() > target:
        i, j = np.unravel_index(np.argmax(similarity), similarity.shape)
        if count is None and similarity[i, j] < threshold:
            break
        merged = (sizes[i] * similarity[i] + sizes[j] * similarity[j]) / (sizes[i] + sizes[j])
        similarity[i], similarity[:, i] = merged, merged
        similarity[i, i] = -np.inf
        similarity[j], similarity[:, j] = -np.inf, -np.inf
        sizes[i] += sizes[j]
        members[i] += members[j]
        active[j] = False
    centroids = np.stack([points[members[k]].mean(axis=0) for k in np.flatnonzero(active)])
    centroids /= np.linalg.norm(centroids, axis=1, keepdims=True)
    labels = np.argmax(embeddings @ centroids.T, axis=1)
    while count is None and len(centroids) > 1:
        talk = np.bincount(labels, weights=durations, minlength=len(centroids))
        smallest = int(np.argmin(talk))
        if talk[smallest] >= MIN_SPEAKER_SECONDS:
            break
        centroids = np.delete(centroids, smallest, axis=0)
        labels = np.argmax(embeddings @ centroids.T, axis=1)
    return labels


def is_question(text: str) -> bool:
    return text.rstrip(CLOSING_MARKS + " ").endswith("?")


def name_speakers(sentences: list[Sentence], voices: list[int], split_single: bool = True) -> tuple[list[dict], list[str]]:
    """Pick the journalist and name everyone; returns (speakers, speaker id per sentence)."""
    order = list(dict.fromkeys(voices))  # voices by first appearance
    if len(order) == 1 and not split_single:
        return [{"id": "S0", "name": "Intervenant", "questions": False}], ["S0"] * len(sentences)
    if len(order) == 1:
        # Only one voice found: fall back on punctuation, questions vs. the rest.
        voices = [0 if is_question(s.text) else 1 for s in sentences]
        order = [0, 1]
        journalist = 0
    else:
        questions = {v: 0 for v in order}
        talk = {v: 0.0 for v in order}
        for sentence, voice in zip(sentences, voices):
            questions[voice] += is_question(sentence.text)
            talk[voice] += sentence.end - sentence.start
        journalist = max(order, key=lambda v: (questions[v], -talk[v]))
        if questions[journalist] == 0:
            journalist = order[0]

    guests = [v for v in order if v != journalist]
    ids = {journalist: "S0", **{voice: f"S{i + 1}" for i, voice in enumerate(guests)}}
    speakers = [{"id": "S0", "name": "Journaliste", "questions": True}]
    for i, _ in enumerate(guests):
        speakers.append({"id": f"S{i + 1}", "name": "Invité" if len(guests) == 1 else f"Invité {i + 1}", "questions": False})
    return speakers, [ids[v] for v in voices]


def carry_names(old_ids: list[str], old_speakers: list[dict], new_ids: list[str], new_speakers: list[dict]) -> None:
    """Keep names typed by hand: each new voice takes the name of the old voice it shares most sentences with."""
    old = {s["id"]: s for s in old_speakers}
    taken: set[str] = set()
    for speaker in new_speakers:
        shared = Counter(o for o, n in zip(old_ids, new_ids) if n == speaker["id"] and o in old)
        match = next((o for o, _ in shared.most_common() if o not in taken), None)
        if match:
            taken.add(match)
            speaker["name"], speaker["questions"] = old[match]["name"], old[match]["questions"]


def run_voices(folder: Path, count: Optional[int]) -> None:
    """Redo only the speaker step on a finished interview, keeping its (possibly corrected) text."""
    iid = folder.name
    transcript = store.read_transcript(iid)
    if not transcript or not transcript.get("sentences"):
        raise UserFacingError("Il n'y a pas encore de texte à analyser pour cette interview.")
    subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    store.update_status(iid, stage="Reconnaissance des voix", progress=0.02)
    wav = folder / "work16k.wav"
    afconvert("-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", str(folder / "audio.m4a"), str(wav))
    audio = read_wav(wav)

    rows = transcript["sentences"]
    sentences = [Sentence(r["text"], r["start"], r["end"]) for r in rows]
    embeddings = voice_embeddings(audio, sentences, lambda stage, f: store.update_status(iid, progress=round(0.05 + 0.9 * f, 3)))
    durations = np.array([s.end - s.start for s in sentences])
    voices = cluster_voices(embeddings, durations, count=count).tolist()
    speakers, ids = name_speakers(sentences, voices, split_single=count is None)
    carry_names([r["speaker"] for r in rows], transcript["speakers"], ids, speakers)
    for row, speaker_id in zip(rows, ids):
        row["speaker"] = speaker_id
    transcript["speakers"] = speakers
    store.write_transcript(iid, transcript)
    store.update_status(iid, status="done", stage=None, progress=1.0, error=None)
    wav.unlink(missing_ok=True)


# ---------- Main ----------

def run(folder: Path) -> None:
    iid = folder.name
    weights = {"Conversion": (0.0, 0.03), "Transcription": (0.03, 0.72), "Horodatage": (0.72, 0.88), "Reconnaissance des voix": (0.88, 0.99)}

    def progress(stage: str, fraction: float) -> None:
        low, high = weights[stage]
        store.update_status(iid, stage=stage, progress=round(low + (high - low) * min(max(fraction, 0.0), 1.0), 3))

    # Keep the Mac awake for as long as this transcription runs, and no longer.
    subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])

    source = store.source_path(iid)
    if source is None:
        raise UserFacingError("Le fichier importé est introuvable. Supprimez cette interview et importez-la à nouveau.")
    progress("Conversion", 0.0)
    wav = prepare_audio(source, folder)
    audio = read_wav(wav)
    duration = len(audio) / SAMPLE_RATE
    store.update_status(iid, duration=round(duration, 2))

    progress("Transcription", 0.0)
    chunks, language = transcribe_chunks(audio, progress)
    release_asr_model()
    progress("Horodatage", 0.0)
    sentences = split_sentences(align_words(audio, chunks, language, progress))
    if not sentences:
        raise UserFacingError("Aucune parole n'a été détectée dans cet enregistrement.")

    progress("Reconnaissance des voix", 0.0)
    embeddings = voice_embeddings(audio, sentences, progress)
    durations = np.array([s.end - s.start for s in sentences])
    count = store.read_status(iid).get("speakers_count")
    speakers, speaker_ids = name_speakers(sentences, cluster_voices(embeddings, durations, count=count).tolist(),
                                          split_single=count is None)

    store.write_transcript(iid, {
        "speakers": speakers,
        "sentences": [
            {"id": i, "start": round(s.start, 2), "end": round(s.end, 2), "text": s.text, "speaker": speaker}
            for i, (s, speaker) in enumerate(zip(sentences, speaker_ids))
        ],
    })
    store.update_status(iid, status="done", stage=None, progress=1.0, language=language, error=None)
    source.unlink(missing_ok=True)
    wav.unlink(missing_ok=True)


def main() -> None:
    folder = Path(sys.argv[1])
    status = store.read_status(folder.name)
    try:
        if status.get("job") == "voices":
            run_voices(folder, status.get("speakers_count"))
        else:
            run(folder)
    except UserFacingError as error:
        store.update_status(folder.name, status="error", error=str(error))
        sys.exit(1)
    except Exception as error:  # noqa: BLE001 - reported in the app
        traceback.print_exc()
        store.update_status(folder.name, status="error", error=f"{type(error).__name__}: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
