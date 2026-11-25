"use strict";

const $ = (id) => document.getElementById(id);
const els = {
  history: $("history"), viewSwitch: $("view-switch"), viewFolders: $("view-folders"), viewAll: $("view-all"), fileInput: $("file-input"), offline: $("offline"), scroll: $("scroll"),
  empty: $("empty"), doc: $("doc"), title: $("title"), meta: $("meta"), folder: $("folder"), folderList: $("folder-list"),
  editToggle: $("edit-toggle"), exportBox: $("export"), exportButton: $("export-button"), exportMenu: $("export-menu"),
  exportDocx: $("export-docx"), exportTxt: $("export-txt"), saveState: $("save-state"), deleteButton: $("delete-button"),
  speakers: $("speakers"), processing: $("processing"), editHint: $("edit-hint"), transcript: $("transcript"), assign: $("assign"),
  quitButton: $("quit-button"), favPop: $("fav-pop"), markers: $("markers"),
  player: $("player"), audio: $("audio"), play: $("play"), back: $("back"), forward: $("forward"),
  time: $("time"), seek: $("seek"), duration: $("duration"), rate: $("rate"), drop: $("drop"),
};

const state = {
  interviews: [],        // summaries shown in the sidebar
  uploads: [],           // { name, progress } while files are being sent
  current: null,         // full interview on screen
  editing: false,
  currentIndex: -1,      // sentence highlighted by playback
  lastManualScroll: 0,
  seeking: false,
  view: "folders",                // "folders" or "all"
  openFolders: new Set([""]),     // folders are folded by default; "Sans dossier" stays open so new imports show up
};
const DRAG_TYPE = "text/x-interview";
const pendingSaves = new Set();

const LANGUAGES = { French: "Français", English: "Anglais", German: "Allemand", Italian: "Italien", Spanish: "Espagnol", Portuguese: "Portugais" };

// ---------- Helpers ----------

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!response.ok) {
    let message = response.statusText;
    try { message = (await response.json()).detail || message; } catch { /* not JSON */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function timecode(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  const two = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${two(m)}:${two(s)}` : `${two(m)}:${two(s)}`;
}

function durationLabel(seconds) {
  if (!seconds) return "";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, "0")}`;
}

function dateLabel(iso) {
  return new Date(iso).toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
}

function statusLabel(item) {
  if (item.status === "queued") return "En attente";
  if (item.status === "processing") return `${item.stage || "Traitement"} · ${Math.round((item.progress || 0) * 100)} %`;
  if (item.status === "error") return "Erreur";
  return "";
}

function isReady(interview) {
  return interview?.status === "done" && Array.isArray(interview.sentences);
}

// ---------- Sidebar & polling ----------

let dragging = false;  // the sidebar is not redrawn mid-drag, or the polling refresh would drop the dragged item
function renderHistory() {
  if (dragging) return;
  els.history.replaceChildren();
  for (const upload of state.uploads) {
    const li = el("li", "uploading");
    li.append(el("div", "h-title", upload.name), el("span", "pill", `Envoi · ${Math.round(upload.progress * 100)} %`));
    els.history.append(li);
  }
  const groups = new Map();
  for (const item of state.interviews) {
    const name = item.folder || "";
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(item);
  }
  const folders = [...groups.keys()].filter(Boolean).sort((a, b) => a.localeCompare(b, "fr"));
  const order = [...folders, ""];  // "Sans dossier" always shown: it is where you drop an interview to take it out of its folder
  const flat = !folders.length || state.view === "all";
  els.viewSwitch.hidden = !folders.length;
  els.viewFolders.classList.toggle("on", !flat);
  els.viewAll.classList.toggle("on", flat);
  if (flat) {
    for (const item of state.interviews) els.history.append(historyItem(item, true));
  } else {
    for (const name of order) {
      const members = groups.get(name) || [];
      els.history.append(folderHeader(name, members.length));
      if (state.openFolders.has(name)) for (const item of members) els.history.append(historyItem(item, false));
    }
  }
  els.folderList.replaceChildren(...folders.map((name) => Object.assign(el("option"), { value: name })));
}

function historyItem(item, showFolder) {
  const li = el("li");
  li.classList.toggle("active", state.current?.id === item.id);
  li.draggable = true;
  li.addEventListener("dragstart", (event) => {
    event.dataTransfer.setData(DRAG_TYPE, item.id);
    event.dataTransfer.effectAllowed = "move";
    dragging = true;
  });
  li.addEventListener("dragend", () => {
    dragging = false;
    renderHistory();
  });
  li.append(el("div", "h-title", item.title));
  li.append(el("div", "h-meta", [dateLabel(item.created_at), durationLabel(item.duration)].filter(Boolean).join(" · ")));
  if (showFolder && item.folder) li.append(el("span", "pill folder-pill", item.folder));
  const label = statusLabel(item);
  if (label) li.append(el("span", item.status === "error" ? "pill error" : "pill", label));
  li.addEventListener("click", () => openInterview(item.id));
  return li;
}

function folderHeader(name, count) {
  const open = state.openFolders.has(name);
  const li = el("li", "folder");
  const toggle = el("button", "folder-toggle");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", String(open));
  toggle.append(el("span", "chevron", open ? "▾" : "▸"), el("span", "folder-name", name || "Sans dossier"), el("span", "folder-count", String(count)));
  toggle.addEventListener("click", () => {
    if (open) state.openFolders.delete(name);
    else state.openFolders.add(name);
    renderHistory();
  });
  li.append(toggle);
  // Drop an interview on a folder title to move it there ("Sans dossier" takes it out of its folder).
  li.addEventListener("dragover", (event) => {
    if (!event.dataTransfer.types.includes(DRAG_TYPE)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    li.classList.add("drop-target");
  });
  li.addEventListener("dragleave", () => li.classList.remove("drop-target"));
  li.addEventListener("drop", (event) => {
    const id = event.dataTransfer.getData(DRAG_TYPE);
    li.classList.remove("drop-target");
    dragging = false;
    if (!id) return;
    event.preventDefault();
    event.stopPropagation();
    moveToFolder(id, name);
  });
  return li;
}

async function moveToFolder(id, folder) {
  const item = state.interviews.find((interview) => interview.id === id);
  if (!item || (item.folder || "") === folder) return;
  try {
    await api(`/api/interviews/${id}`, { method: "PATCH", body: JSON.stringify({ folder }) });
  } catch (error) {
    alert(`Impossible de déplacer l'interview : ${error.message}`);
    return;
  }
  state.openFolders.add(folder);
  if (state.current?.id === id) {
    state.current.folder = folder;
    if (document.activeElement !== els.folder) els.folder.value = folder;
  }
  refresh();
}

function setView(view) {
  state.view = view;
  try { localStorage.setItem("view", view); } catch { /* storage unavailable */ }
  renderHistory();
}
els.viewFolders.addEventListener("click", () => setView("folders"));
els.viewAll.addEventListener("click", () => setView("all"));

let pollTimer = null;
async function refresh() {
  clearTimeout(pollTimer);
  try {
    state.interviews = await api("/api/interviews");
    els.offline.hidden = true;
  } catch {
    els.offline.hidden = false;
    pollTimer = setTimeout(refresh, 3000);
    return;
  }
  renderHistory();

  const current = state.current;
  if (current) {
    const summary = state.interviews.find((item) => item.id === current.id);
    if (!summary) {
      state.current = null;
      showCurrent();
    } else if (!isReady(current)) {
      if (summary.status === "done") {
        await openInterview(current.id);
      } else if (summary.status !== current.status || summary.stage !== current.stage || summary.progress !== current.progress) {
        Object.assign(current, summary);
        showCurrent();
      }
    }
  } else if (!state.interviews.length) {
    showCurrent();
  }

  const busy = state.uploads.length || state.interviews.some((item) => item.status === "queued" || item.status === "processing");
  pollTimer = setTimeout(refresh, busy ? 1500 : 10000);
}

async function openInterview(id) {
  let data;
  try {
    data = await api(`/api/interviews/${id}`);
  } catch (error) {
    alert(`Impossible d'ouvrir cette interview : ${error.message}`);
    return;
  }
  const switching = state.current?.id !== id;
  state.current = data;
  if (location.hash.slice(1) !== id) history.replaceState(null, "", `#${id}`);
  if (switching) {
    state.editing = false;
    state.currentIndex = -1;
    els.scroll.scrollTop = 0;
    state.openFolders.add(data.folder || "");
  }
  showCurrent();
  renderHistory();
}

// ---------- Interview view ----------

function showCurrent() {
  const current = state.current;
  els.empty.hidden = Boolean(current);
  els.doc.hidden = !current;
  if (!current) {
    setupPlayer(false);
    return;
  }
  const ready = isReady(current);
  if (document.activeElement !== els.title) els.title.value = current.title;
  if (document.activeElement !== els.folder) els.folder.value = current.folder || "";
  els.meta.textContent = [
    dateLabel(current.created_at),
    durationLabel(current.duration),
    LANGUAGES[current.language] || current.language,
    current.source_name,
  ].filter(Boolean).join(" · ");

  els.editToggle.hidden = !ready;
  els.exportBox.hidden = !ready;
  els.editToggle.classList.toggle("on", state.editing);
  els.editToggle.textContent = state.editing ? "Terminer les corrections" : "Corriger le texte";
  els.doc.classList.toggle("editing", state.editing && ready);
  els.editHint.hidden = !(state.editing && ready);
  els.processing.hidden = ready;
  if (!ready) renderProcessing();
  renderSpeakers(ready);
  renderTranscript(ready);
  setupPlayer(ready);
  renderMarkers();
}

function renderMarkers() {
  els.markers.replaceChildren();
  const current = state.current;
  if (!isReady(current)) return;
  const duration = Number.isFinite(els.audio.duration) && els.audio.duration > 0 ? els.audio.duration : current.duration;
  if (!duration) return;
  current.sentences.forEach((sentence, index) => {
    if (!sentence.favorite) return;
    const marker = el("button", "marker", "★");
    marker.type = "button";
    marker.style.left = `${Math.min(100, (sentence.start / duration) * 100)}%`;
    marker.title = `${timecode(sentence.start)} — ${sentence.text}`;
    marker.setAttribute("aria-label", `Favori à ${timecode(sentence.start)}`);
    marker.addEventListener("click", () => playFrom(index));
    els.markers.append(marker);
  });
}

function renderProcessing() {
  const current = state.current;
  els.processing.replaceChildren();
  if (current.status === "error") {
    const retry = el("button", "primary", "Réessayer");
    retry.type = "button";
    retry.addEventListener("click", async () => {
      await api(`/api/interviews/${current.id}/retry`, { method: "POST" });
      await openInterview(current.id);
      refresh();
    });
    els.processing.append(el("h2", null, "La transcription a échoué"), el("pre", null, current.error || "Erreur inconnue"), retry);
    return;
  }
  const waiting = current.status === "queued";
  const bar = el("div", "bar");
  const fill = el("div");
  fill.style.width = `${Math.round((current.progress || 0) * 100)}%`;
  bar.append(fill);
  els.processing.append(
    el("h2", null, waiting ? "En attente de transcription" : `${current.stage || "Traitement"}…`),
    bar,
    el("p", null, waiting
      ? "Elle démarrera dès que la transcription en cours sera terminée."
      : "Vous pouvez continuer à utiliser l'application pendant ce temps. Gardez la fenêtre Terminal ouverte."),
  );
}

function renderSpeakers(ready) {
  els.speakers.replaceChildren();
  els.speakers.hidden = !ready;
  if (!ready) return;
  const used = new Set(state.current.sentences.map((sentence) => sentence.speaker));
  for (const speaker of state.current.speakers) {
    if (!used.has(speaker.id) && !state.editing) continue;
    const chip = el("div", speaker.questions ? "speaker questions" : "speaker");
    const name = el("input");
    name.type = "text";
    name.value = speaker.name;
    name.size = Math.max(6, speaker.name.length);
    name.setAttribute("aria-label", "Nom de l'intervenant");
    name.addEventListener("input", () => { name.size = Math.max(6, name.value.length); });
    name.addEventListener("change", () => save({ speakers: [{ id: speaker.id, name: name.value }] }, { rerender: true }));
    name.addEventListener("keydown", (event) => { if (event.key === "Enter") name.blur(); });

    const label = el("label");
    const box = el("input");
    box.type = "checkbox";
    box.checked = speaker.questions;
    box.addEventListener("change", () => save({ speakers: [{ id: speaker.id, questions: box.checked }] }, { rerender: true }));
    label.append(box, "Questions en gras");
    chip.append(name, label);
    els.speakers.append(chip);
  }
  if (state.editing) {
    const add = el("button", "add-speaker", "+ Intervenant");
    add.type = "button";
    add.addEventListener("click", () => {
      const taken = new Set(state.current.speakers.map((speaker) => speaker.id));
      let n = 0;
      while (taken.has(`S${n}`)) n++;
      const guests = state.current.speakers.filter((speaker) => !speaker.questions).length;
      save({ speakers: [{ id: `S${n}`, name: `Invité ${guests + 1}`, questions: false }] }, { rerender: true });
    });
    els.speakers.append(add);

    const rerun = el("div", "rerun");
    const count = el("select");
    count.setAttribute("aria-label", "Nombre d'intervenants");
    for (const value of ["", "1", "2", "3", "4", "5", "6"]) {
      const option = el("option", null, value ? `${value} intervenant${value === "1" ? "" : "s"}` : "Nombre automatique");
      option.value = value;
      count.append(option);
    }
    count.value = used.size <= 6 ? String(used.size) : "";
    const go = el("button", null, "Relancer la reconnaissance des voix");
    go.type = "button";
    go.addEventListener("click", () => rerunVoices(count.value ? Number(count.value) : null));
    rerun.append(count, go);
    els.speakers.append(rerun);
  }
}

async function rerunVoices(count) {
  const id = state.current.id;
  await Promise.allSettled([...pendingSaves]);
  try {
    state.current = await api(`/api/interviews/${id}/speakers`, { method: "POST", body: JSON.stringify({ count }) });
  } catch (error) {
    alert(`Impossible de relancer la reconnaissance des voix : ${error.message}`);
    return;
  }
  state.editing = false;
  showCurrent();
  refresh();
}

async function removeBlock(ids) {
  await save({ delete_sentences: ids }, { rerender: true });
  els.saveState.textContent = "Bloc supprimé — ⌘Z pour le rétablir";
  clearTimeout(saveStateTimer);
  saveStateTimer = setTimeout(() => { els.saveState.textContent = ""; }, 6000);
}

async function restoreBlock() {
  const current = state.current;
  if (!isReady(current)) return;
  await Promise.allSettled([...pendingSaves]);
  try {
    state.current = await api(`/api/interviews/${current.id}/restore`, { method: "POST" });
  } catch {
    els.saveState.textContent = "Aucun bloc supprimé à rétablir";
    return;
  }
  showCurrent();
  els.saveState.textContent = "Bloc rétabli";
}

function renderTranscript(ready) {
  hideAssign();
  hideFav();
  els.transcript.replaceChildren();
  if (!ready) return;
  const speakers = new Map(state.current.speakers.map((speaker) => [speaker.id, speaker]));
  let text = null;
  let lastSpeaker = null;
  let blockIds = [];
  state.current.sentences.forEach((sentence, index) => {
    if (sentence.speaker !== lastSpeaker || !text) {
      const speaker = speakers.get(sentence.speaker) || { name: "?", questions: false };
      const para = el("div", speaker.questions ? "para question" : "para");
      const stamp = el("button", "tc", timecode(sentence.start));
      stamp.type = "button";
      stamp.dataset.index = index;
      const body = el("div", "body");
      text = el("p", "text");
      body.append(text);
      if (!speaker.questions || state.editing) body.append(el("div", "who", `— ${speaker.name}`));
      if (state.editing) {
        const ids = (blockIds = []);
        const remove = el("button", "remove-block", "Supprimer");
        remove.type = "button";
        remove.addEventListener("click", () => removeBlock(ids));
        body.append(remove);
      }
      para.append(stamp, body);
      els.transcript.append(para);
      lastSpeaker = sentence.speaker;
    }
    if (text.childNodes.length) text.append(" ");
    const span = el("span", "s", sentence.text);
    span.dataset.index = index;
    if (state.editing) {
      try { span.contentEditable = "plaintext-only"; } catch { span.contentEditable = "true"; }
      span.spellcheck = true;
    }
    if (index === state.currentIndex) span.classList.add("current");
    if (sentence.favorite) span.classList.add("favorite");
    text.append(span);
    blockIds.push(sentence.id);
  });
}

// ---------- Saving corrections ----------

let saveStateTimer = null;
function save(change, { rerender = false } = {}) {
  const id = state.current.id;
  els.saveState.textContent = "Enregistrement…";
  const request = (async () => {
    try {
      // keepalive lets the request finish even if the page is being closed
      const data = await api(`/api/interviews/${id}`, { method: "PATCH", body: JSON.stringify(change), keepalive: true });
      if (state.current?.id !== id) return;
      if (rerender) {
        state.current = data;
        showCurrent();
      }
      if (change.title !== undefined || change.folder !== undefined) {
        state.current.title = data.title;
        state.current.folder = data.folder;
        state.openFolders.add(data.folder || "");
        refresh();
      }
      els.saveState.textContent = "Modifications enregistrées";
      clearTimeout(saveStateTimer);
      saveStateTimer = setTimeout(() => { els.saveState.textContent = ""; }, 2500);
    } catch (error) {
      els.saveState.textContent = `Échec de l'enregistrement : ${error.message}`;
    }
  })();
  pendingSaves.add(request);
  request.finally(() => pendingSaves.delete(request));
  return request;
}

function sentenceText(span) {
  return span.textContent.replace(/\s+/g, " ").trim();
}

let assignIndex = -1;
function showAssign(span) {
  assignIndex = Number(span.dataset.index);
  const sentence = state.current.sentences[assignIndex];
  els.assign.replaceChildren(el("span", "assign-label", "Dit par"));
  for (const speaker of state.current.speakers) {
    const button = el("button", speaker.id === sentence.speaker ? "on" : "", speaker.name);
    button.type = "button";
    button.addEventListener("mousedown", (event) => event.preventDefault()); // keep the caret in the sentence
    button.addEventListener("click", () => assignSpeaker(assignIndex, speaker.id));
    els.assign.append(button);
  }
  placePopover(els.assign, span);
}

function placePopover(pop, span) {
  pop.hidden = false;
  pop.style.left = "0px"; // measure the toolbar at full width before placing it
  const docBox = els.doc.getBoundingClientRect();
  const line = span.getClientRects()[0] || span.getBoundingClientRect();
  const maxLeft = els.doc.clientWidth - pop.offsetWidth - 8;
  pop.style.left = `${Math.max(0, Math.min(line.left - docBox.left, maxLeft))}px`;
  pop.style.top = `${line.top - docBox.top - pop.offsetHeight - 6}px`;
}

let favIndex = -1;
function showFav(span) {
  favIndex = Number(span.dataset.index);
  const favorite = Boolean(state.current.sentences[favIndex].favorite);
  const button = el("button", favorite ? "on" : "", favorite ? "★ Retirer des favoris" : "☆ Favori");
  button.type = "button";
  button.addEventListener("click", () => toggleFavorite(favIndex));
  els.favPop.replaceChildren(button);
  placePopover(els.favPop, span);
}

function hideFav() {
  els.favPop.hidden = true;
  favIndex = -1;
}

function toggleFavorite(index) {
  const sentence = state.current.sentences[index];
  const favorite = !sentence.favorite;
  sentence.favorite = favorite;
  els.transcript.querySelector(`.s[data-index="${index}"]`)?.classList.toggle("favorite", favorite);
  hideFav();
  renderMarkers();
  save({ sentences: [{ id: sentence.id, favorite }] });
}

function hideAssign() {
  els.assign.hidden = true;
  assignIndex = -1;
}

async function assignSpeaker(index, speakerId) {
  const sentence = state.current.sentences[index];
  if (!sentence || sentence.speaker === speakerId) return;
  const change = { id: sentence.id, speaker: speakerId };
  const span = els.transcript.querySelector(`.s[data-index="${index}"]`);
  if (span && sentenceText(span) !== sentence.text) change.text = sentenceText(span);
  await save({ sentences: [change] }, { rerender: true });
  els.transcript.querySelector(`.s[data-index="${index}"]`)?.focus();
}

els.transcript.addEventListener("click", (event) => {
  const stamp = event.target.closest(".tc");
  if (stamp) return playFrom(Number(stamp.dataset.index));
  const span = event.target.closest(".s");
  if (span && !state.editing) {
    playFrom(Number(span.dataset.index));
    showFav(span);
  }
});

els.transcript.addEventListener("focusin", (event) => {
  const span = event.target.closest(".s");
  if (span && state.editing) showAssign(span);
});

const textSaveTimers = new Map();
function saveSentenceText(span) {
  const index = Number(span.dataset.index);
  clearTimeout(textSaveTimers.get(index));
  textSaveTimers.delete(index);
  const sentence = state.current?.sentences[index];
  const text = sentenceText(span);
  if (!sentence || text === sentence.text) return;
  sentence.text = text;
  save({ sentences: [{ id: sentence.id, text }] });
}

// Save while typing too, so closing the tab right after a correction doesn't lose it.
els.transcript.addEventListener("input", (event) => {
  const span = event.target.closest(".s");
  if (!span || !state.editing) return;
  const index = Number(span.dataset.index);
  clearTimeout(textSaveTimers.get(index));
  textSaveTimers.set(index, setTimeout(() => saveSentenceText(span), 800));
});

window.addEventListener("pagehide", () => {
  const span = document.activeElement?.closest?.(".s");
  if (span && state.editing) saveSentenceText(span);
});

els.transcript.addEventListener("focusout", (event) => {
  const span = event.target.closest(".s");
  if (!span || !state.editing) return;
  saveSentenceText(span);
  setTimeout(() => {
    if (!document.activeElement?.closest?.(".s")) hideAssign();
  }, 0);
});

els.transcript.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.closest(".s")) {
    event.preventDefault();
    event.target.blur();
  }
});

els.editToggle.addEventListener("click", () => {
  if (document.activeElement?.closest?.(".s")) document.activeElement.blur();
  state.editing = !state.editing;
  showCurrent();
});

els.title.addEventListener("change", () => save({ title: els.title.value }));
els.folder.addEventListener("change", () => save({ folder: els.folder.value }));
els.folder.addEventListener("keydown", (event) => { if (event.key === "Enter") els.folder.blur(); });
els.title.addEventListener("keydown", (event) => { if (event.key === "Enter") els.title.blur(); });

els.exportButton.addEventListener("click", (event) => {
  event.stopPropagation();
  els.exportMenu.hidden = !els.exportMenu.hidden;
});
for (const [link, format] of [[els.exportDocx, "docx"], [els.exportTxt, "txt"]]) {
  link.addEventListener("click", async (event) => {
    event.preventDefault();
    els.exportMenu.hidden = true;
    await Promise.allSettled([...pendingSaves]);
    location.href = `/api/interviews/${state.current.id}/export?format=${format}`;
  });
}
document.addEventListener("click", (event) => {
  els.exportMenu.hidden = true;
  if (!event.target.closest?.(".s, #fav-pop")) hideFav();
});

els.deleteButton.addEventListener("click", async () => {
  const current = state.current;
  if (!current || !confirm(`Supprimer « ${current.title} » ? La transcription et l'enregistrement seront effacés.`)) return;
  await api(`/api/interviews/${current.id}`, { method: "DELETE" });
  state.current = null;
  history.replaceState(null, "", location.pathname);
  await refresh();
  if (state.interviews[0]) await openInterview(state.interviews[0].id);
  else showCurrent();
});

els.quitButton.addEventListener("click", async () => {
  if (!confirm("Quitter Interviews ? Une transcription en cours sera interrompue — vous pourrez la relancer plus tard.")) return;
  try {
    await api("/api/shutdown", { method: "POST" });
  } catch {
    // the server may close the connection before answering; that's the point
  }
  clearTimeout(pollTimer);
  els.offline.textContent = "Interviews est arrêté. Vous pouvez fermer cet onglet.";
  els.offline.hidden = false;
});

// ---------- Player ----------

function setupPlayer(ready) {
  els.player.hidden = !ready;
  const audio = els.audio;
  if (!ready) {
    if (audio.dataset.id) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
      delete audio.dataset.id;
    }
    return;
  }
  if (audio.dataset.id !== state.current.id) {
    audio.src = `/api/interviews/${state.current.id}/audio`;
    audio.dataset.id = state.current.id;
    audio.playbackRate = audio.defaultPlaybackRate = Number(els.rate.value);
    updateClock();
  }
}

function playFrom(index) {
  const sentence = state.current?.sentences[index];
  if (!sentence) return;
  els.audio.currentTime = sentence.start;
  setCurrent(index, false);
  els.audio.play().catch(() => {});
}

function togglePlay() {
  if (els.audio.paused) els.audio.play().catch(() => {});
  else els.audio.pause();
}

function nudge(delta) {
  const audio = els.audio;
  audio.currentTime = Math.max(0, Math.min(audio.duration || 0, audio.currentTime + delta));
}

function updateClock() {
  const { currentTime, duration } = els.audio;
  els.time.textContent = timecode(currentTime);
  els.duration.textContent = Number.isFinite(duration) ? timecode(duration) : "--:--";
  if (!state.seeking && Number.isFinite(duration) && duration > 0) {
    els.seek.value = Math.round((currentTime / duration) * 1000);
  }
}

function sentenceAt(time) {
  const list = state.current?.sentences;
  if (!list?.length) return -1;
  let low = 0, high = list.length - 1, found = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (list[mid].start <= time + 0.05) { found = mid; low = mid + 1; } else { high = mid - 1; }
  }
  return found;
}

function setCurrent(index, scroll) {
  els.transcript.querySelector(".s.current")?.classList.remove("current");
  state.currentIndex = index;
  const span = index >= 0 ? els.transcript.querySelector(`.s[data-index="${index}"]`) : null;
  if (!span) return;
  span.classList.add("current");
  if (scroll && !state.editing && Date.now() - state.lastManualScroll > 4000) {
    const box = els.scroll.getBoundingClientRect();
    const rect = span.getBoundingClientRect();
    if (rect.top < box.top + box.height * 0.2 || rect.bottom > box.top + box.height * 0.7) {
      span.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
}

function tick() {
  updateClock();
  const index = sentenceAt(els.audio.currentTime);
  if (index !== state.currentIndex) setCurrent(index, !els.audio.paused);
}

let looping = false;
function startLoop() {
  if (looping) return;
  looping = true;
  const step = () => {
    tick();
    if (els.audio.paused) { looping = false; return; }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

els.audio.addEventListener("play", () => { els.play.classList.add("playing"); startLoop(); });
els.audio.addEventListener("pause", () => els.play.classList.remove("playing"));
els.audio.addEventListener("loadedmetadata", () => { updateClock(); renderMarkers(); });
els.audio.addEventListener("seeked", tick);
els.audio.addEventListener("timeupdate", tick); // also covers tabs where animation frames are throttled
els.play.addEventListener("click", togglePlay);
els.back.addEventListener("click", () => nudge(-5));
els.forward.addEventListener("click", () => nudge(5));
els.seek.addEventListener("input", () => {
  state.seeking = true;
  const { duration } = els.audio;
  if (Number.isFinite(duration)) els.audio.currentTime = (els.seek.value / 1000) * duration;
});
els.seek.addEventListener("change", () => { state.seeking = false; });
els.rate.addEventListener("change", () => {
  els.audio.playbackRate = els.audio.defaultPlaybackRate = Number(els.rate.value);
  try { localStorage.setItem("rate", els.rate.value); } catch { /* storage unavailable */ }
});
for (const type of ["wheel", "touchmove"]) {
  els.scroll.addEventListener(type, () => { state.lastManualScroll = Date.now(); }, { passive: true });
}

document.addEventListener("keydown", (event) => {
  const target = event.target;
  if (target.isContentEditable || target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return;
  // ⌘Z outside a text field brings back the last deleted block (inside one, it undoes typing as usual)
  if ((event.metaKey || event.ctrlKey) && !event.shiftKey && event.key.toLowerCase() === "z") {
    event.preventDefault();
    restoreBlock();
    return;
  }
  if (!state.current || els.player.hidden) return;
  if (event.key === " " && !(target instanceof HTMLButtonElement)) {
    event.preventDefault();
    togglePlay();
  } else if (event.key === "ArrowLeft") {
    nudge(-5);
  } else if (event.key === "ArrowRight") {
    nudge(5);
  }
});

// ---------- Import ----------

function uploadFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/interviews");
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(event.loaded / event.total); };
    xhr.onload = () => (xhr.status < 300 ? resolve(JSON.parse(xhr.responseText)) : reject(new Error(xhr.statusText || `HTTP ${xhr.status}`)));
    xhr.onerror = () => reject(new Error("connexion interrompue"));
    xhr.send(form);
  });
}

async function importFiles(files) {
  for (const file of files) {
    const upload = { name: file.name, progress: 0 };
    state.uploads.push(upload);
    renderHistory();
    try {
      const { id } = await uploadFile(file, (progress) => { upload.progress = progress; renderHistory(); });
      state.uploads.splice(state.uploads.indexOf(upload), 1);
      await refresh();
      await openInterview(id);
    } catch (error) {
      state.uploads.splice(state.uploads.indexOf(upload), 1);
      renderHistory();
      alert(`Impossible d'importer « ${file.name} » : ${error.message}`);
    }
  }
}

for (const button of [$("import-button"), $("empty-import")]) {
  button.addEventListener("click", () => els.fileInput.click());
}
els.fileInput.addEventListener("change", () => {
  importFiles([...els.fileInput.files]);
  els.fileInput.value = "";
});

let dragDepth = 0;
const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
window.addEventListener("dragenter", (event) => {
  if (!hasFiles(event)) return;
  event.preventDefault();
  dragDepth++;
  els.drop.hidden = false;
});
window.addEventListener("dragover", (event) => { if (hasFiles(event)) event.preventDefault(); });
window.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) els.drop.hidden = true;
});
window.addEventListener("drop", (event) => {
  event.preventDefault();
  dragDepth = 0;
  els.drop.hidden = true;
  const files = [...(event.dataTransfer?.files || [])];
  if (files.length) importFiles(files);
});

// ---------- Start ----------

(async function init() {
  try {
    const rate = localStorage.getItem("rate");
    if (rate) els.rate.value = rate;
    if (localStorage.getItem("view") === "all") state.view = "all";
  } catch { /* storage unavailable */ }
  await refresh();
  const wanted = state.interviews.find((item) => item.id === location.hash.slice(1)) || state.interviews[0];
  if (wanted) await openInterview(wanted.id);
  else showCurrent();
})();
