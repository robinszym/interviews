"""Self-checks for the speaker and editing logic. Run: ~/.qwen-transcribe/venv/bin/python tests/test_logic.py"""

import io
import os
import sys
import tempfile
from pathlib import Path

os.environ["INTERVIEWS_LIBRARY"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from fastapi.testclient import TestClient

from interviews import pipeline as P, store
from interviews.server import app

# --- cluster_voices: three clear voices -------------------------------------
rng = np.random.default_rng(0)
axes = np.eye(3, 16)
emb = np.concatenate([axes[k] + 0.05 * rng.standard_normal((6, 16)) for k in range(3)])
emb /= np.linalg.norm(emb, axis=1, keepdims=True)
dur = np.full(len(emb), 3.0)
assert len(set(P.cluster_voices(emb, dur).tolist())) == 3, "auto finds the 3 voices"
for k in (1, 2, 3):
    assert len(set(P.cluster_voices(emb, dur, count=k).tolist())) == k, f"count={k} is obeyed"
assert len(set(P.cluster_voices(emb, dur, count=50).tolist())) <= len(emb), "count above the number of sentences is capped"
labels = P.cluster_voices(emb, dur, count=3).tolist()
assert all(len(set(labels[g * 6:(g + 1) * 6])) == 1 for g in range(3)), "each voice stays in one group"

# --- one voice asked for: no question/answer split ------------------------------
S = P.Sentence
speakers, ids = P.name_speakers([S("Bonjour ?", 0, 1), S("Oui.", 1, 2)], [0, 0], split_single=False)
assert [s["name"] for s in speakers] == ["Intervenant"] and ids == ["S0", "S0"]
speakers, ids = P.name_speakers([S("Bonjour ?", 0, 1), S("Oui.", 1, 2)], [0, 0])
assert ids == ["S0", "S1"], "auto mode still splits a single voice on punctuation"

# --- names typed by hand follow their voice --------------------------------------
old_speakers = [{"id": "S0", "name": "Marie", "questions": True}, {"id": "S1", "name": "Paul Rochat", "questions": False}]
new_speakers = [{"id": "S0", "name": "Journaliste", "questions": True}, {"id": "S1", "name": "Invité", "questions": False},
                {"id": "S2", "name": "Invité 2", "questions": False}]
P.carry_names(["S0", "S1", "S1", "S1"], old_speakers, ["S1", "S0", "S0", "S2"], new_speakers)
assert new_speakers[0]["name"] == "Paul Rochat" and new_speakers[1]["name"] == "Marie" and new_speakers[1]["questions"]
assert new_speakers[2]["name"] == "Invité 2", "a voice with no match keeps its default name"

# --- server: folder, block deletion, voices rerun ------------------------------
client = TestClient(app)
iid = store.create("Entretien.m4a", io.BytesIO(b"audio"))
store.write_transcript(iid, {"speakers": old_speakers, "sentences": [
    {"id": i, "start": i, "end": i + 1, "text": f"Phrase {i}.", "speaker": "S0"} for i in range(4)]})
store.update_status(iid, status="done")

r = client.patch(f"/api/interviews/{iid}", json={"folder": "  Fromage   d'alpage "})
assert r.status_code == 200 and r.json()["folder"] == "Fromage d'alpage"
assert client.get("/api/interviews").json()[0]["folder"] == "Fromage d'alpage"
r = client.patch(f"/api/interviews/{iid}", json={"delete_sentences": [1, 2]})
assert [s["id"] for s in r.json()["sentences"]] == [0, 3], "only the chosen block is removed"
r = client.patch(f"/api/interviews/{iid}", json={"sentences": [{"id": 3, "favorite": True}]})
assert [s.get("favorite") for s in r.json()["sentences"]] == [None, True], "sentence 3 is a favorite"
r = client.patch(f"/api/interviews/{iid}", json={"sentences": [{"id": 3, "favorite": False}]})
assert "favorite" not in r.json()["sentences"][1], "un-favoriting removes the flag"
r = client.post(f"/api/interviews/{iid}/restore")
assert [s["id"] for s in r.json()["sentences"]] == [0, 1, 2, 3], "undo puts the block back in place"
assert client.post(f"/api/interviews/{iid}/restore").status_code == 409, "nothing left to undo"

assert client.post(f"/api/interviews/{iid}/speakers", json={"count": 0}).status_code == 400
r = client.post(f"/api/interviews/{iid}/speakers", json={"count": 3})
assert r.status_code == 200 and r.json()["status"] == "queued"
assert store.read_status(iid)["job"] == "voices" and store.read_status(iid)["speakers_count"] == 3
assert client.post(f"/api/interviews/{iid}/speakers", json={"count": 2}).status_code == 409, "not while it is queued"

print("tous les tests passent")
