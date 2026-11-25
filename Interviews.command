#!/bin/bash
#
# Interviews — moteur de l'application : installation, démarrage et arrêt du serveur local.
#
# Normalement appelé par Interviews.app. Peut aussi servir seul, dans le Terminal :
#   ./Interviews.command              installe si besoin, démarre et ouvre le navigateur
#   ./Interviews.command check        "ok", "need-install", ou le problème à afficher
#   ./Interviews.command install      installe (affiche la progression)
#   ./Interviews.command install-start / install-state   installation en arrière-plan + suivi
#   ./Interviews.command serve        démarre le serveur en arrière-plan, affiche son PID
#   ./Interviews.command port         affiche le port si l'application répond
#   ./Interviews.command stop         arrête l'application
#   ./Interviews.command version      affiche le numéro de version
#
# Tout est installé dans le dossier caché ~/.qwen-transcribe (modèles compris).

set -euo pipefail

MODE="${1:-all}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_HOME="${QWEN_TRANSCRIBE_HOME:-$HOME/.qwen-transcribe}"
ASR_MODEL="${INTERVIEWS_ASR_MODEL:-Qwen/Qwen3-ASR-1.7B}"
ALIGNER_MODEL="Qwen/Qwen3-ForcedAligner-0.6B"
VOICE_MODEL="3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
VOICE_MODEL_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/$VOICE_MODEL"
PACKAGES=(mlx-qwen3-asr==0.4.0 sherpa-onnx==1.13.8 fastapi==0.141.1 uvicorn==0.52.4 python-multipart==0.0.32 python-docx==1.2.0)
PYTHON_VERSION="3.12"
FIRST_PORT=8765

# Tout reste dans APP_HOME ; on ignore les Python/conda/Homebrew déjà présents sur le Mac.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV CONDA_PREFIX
export UV_CACHE_DIR="$APP_HOME/cache/uv"
export UV_PYTHON_INSTALL_DIR="$APP_HOME/python"
export UV_PYTHON_PREFERENCE="only-managed"
export HF_HOME="$APP_HOME/cache/huggingface"
export HF_HUB_DISABLE_TELEMETRY=1
export HF_HUB_VERBOSITY=error
export UV_CONCURRENT_DOWNLOADS=4  # plus doux sur les connexions capricieuses

UV="$APP_HOME/bin/uv"
VENV="$APP_HOME/venv"
APP_DIR="$APP_HOME/app"
STAMP="$APP_HOME/installed-interviews.txt"
PROGRESS="$APP_HOME/install-progress"
INSTALL_LOG="$APP_HOME/install.log"
SERVER_LOG="$APP_HOME/server.log"
PID_FILE="$APP_HOME/server.pid"
PORT_FILE="$APP_HOME/port"

# --- Mises à jour automatiques ------------------------------------------------
# Au lancement, l'app regarde s'il existe une version plus récente sur GitHub (dossier updates/ du dépôt). Un fichier
# téléchargé par l'app elle-même n'est pas bloqué par macOS, contrairement à un nouveau zip.
# Chaque mise à jour est signée par la clé privée de l'auteur : sans signature valide, rien n'est installé.
VERSION="$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo 0)"
UPDATE_URL="${INTERVIEWS_UPDATE_URL:-https://raw.githubusercontent.com/robinszym/interviews/main/updates}"
UPDATE_DIR="$APP_HOME/update"
RELEASE_PUBLIC_KEY='-----BEGIN PUBLIC KEY-----
MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEA+YgtcUU0nLniuIX/kJu7
0lA1OkF0udXOofIJ1Pzlc2SuNdYU71UwR5ubACxh3OFZa9X6NXgtwpwFeRLbA9QX
Q9iLlVNSqolzZTkUMZHqKCTrMLAmylpVRYf8kqncgfjPiL1IfYFDcH7yZXhRIyKG
eWefs3ed/mhiXjOyDXRnBtubPzcGkkGS18eSe0KXGUfQb32TPGAM9k7kx703m6Oq
MGP5c65wCTFlJLXGOHnSAhZf8Un5XgKulf3pMD7eniOqHaSBTAtixOscqzBFTyPL
J/gUvHHh/wMqfWm76pzXWYebyve5fgkysnlE62jZwDEoPP9OyctZEnCqHpPHpzQc
EJnCxFFGsY3jdjDAXABngxWck8lg371gegiq/z+qm749xz0ZmiT3MNT7iUPDUmOA
GVN2bvImKCzWVVlUZdMXeu2L1iWlb+NlaFbWf2v0L7ysQnL8O+uRj9g8MHzq+wB1
NRrMFGbuVNcUYarOb1SExizNTPYcFcMEe4oKUDVG/1s/AgMBAAE=
-----END PUBLIC KEY-----'

# Une version plus récente déjà téléchargée prend la main (elle apporte son propre code).
if [ -z "${INTERVIEWS_DELEGATED:-}" ] && [ -f "$UPDATE_DIR/VERSION" ] \
   && [ "$(cat "$UPDATE_DIR/VERSION")" -gt "$VERSION" ] 2>/dev/null; then
  INTERVIEWS_DELEGATED=1 exec /bin/bash "$UPDATE_DIR/Interviews.command" "$@"
fi

step() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# Télécharge et installe une version plus récente, si elle est correctement signée.
# Sans internet, ou au moindre doute, on garde simplement la version actuelle. Renvoie 0 si mise à jour.
self_update() {
  local remote tmp
  remote="$(curl -fsSL --max-time 4 "$UPDATE_URL/VERSION" 2>/dev/null | tr -dc '0-9')" || return 1
  [ -n "$remote" ] && [ "$remote" -gt "$VERSION" ] || return 1
  mkdir -p "$APP_HOME"
  tmp="$(mktemp -d "$APP_HOME/update-download.XXXXXX")" || return 1
  printf '%s\n' "$RELEASE_PUBLIC_KEY" > "$tmp/key.pem"
  if curl -fsSL --max-time 120 -o "$tmp/update.zip" "$UPDATE_URL/interviews-update.zip" \
     && curl -fsSL --max-time 15 -o "$tmp/update.zip.sig" "$UPDATE_URL/interviews-update.zip.sig" \
     && openssl dgst -sha256 -verify "$tmp/key.pem" -signature "$tmp/update.zip.sig" "$tmp/update.zip" >/dev/null 2>&1 \
     && ditto -x -k "$tmp/update.zip" "$tmp/new" \
     && [ "$(cat "$tmp/new/VERSION" 2>/dev/null)" = "$remote" ] && [ -f "$tmp/new/Interviews.command" ]; then
    rm -rf "$UPDATE_DIR.old"
    if [ -d "$UPDATE_DIR" ]; then mv "$UPDATE_DIR" "$UPDATE_DIR.old"; fi
    mv "$tmp/new" "$UPDATE_DIR"
    rm -rf "$UPDATE_DIR.old" "$tmp"
    return 0
  fi
  rm -rf "$tmp"
  return 1
}

# Au démarrage de l'app : si une mise à jour vient d'arriver, on relance la même commande avec elle.
update_then_continue() {
  [ -z "${INTERVIEWS_SKIP_UPDATE:-}" ] || return 0
  if self_update; then
    INTERVIEWS_DELEGATED=1 INTERVIEWS_SKIP_UPDATE=1 exec /bin/bash "$UPDATE_DIR/Interviews.command" "$MODE"
  fi
}

# Pendant une installation, l'app lit ce fichier pour afficher la progression.
note() {
  if [ "$MODE" = "install" ] || [ "$MODE" = "all" ]; then
    printf 'running|%s|%s\n' "$1" "$2" > "$PROGRESS.tmp" 2>/dev/null && mv "$PROGRESS.tmp" "$PROGRESS" 2>/dev/null || true
  fi
  step "$1  $2"
}

fail() {
  if [ "$MODE" = "install" ]; then
    printf 'error|%s\n' "$*" > "$PROGRESS.tmp" 2>/dev/null && mv "$PROGRESS.tmp" "$PROGRESS" 2>/dev/null || true
  fi
  printf '\n\033[31m%s\033[0m\n' "$*" >&2
  exit 1
}

# Les téléchargements échouent sur une connexion instable ; 7 Go ne doivent pas mourir sur un hoquet.
retry() {
  local attempt=1
  until "$@"; do
    if [ "$attempt" -ge 3 ]; then return 1; fi
    attempt=$((attempt + 1))
    printf '\nTéléchargement interrompu, nouvelle tentative (%s/3)…\n' "$attempt"
    sleep 5
  done
}

health() { curl -s --max-time 1 "http://127.0.0.1:$1/api/health" 2>/dev/null | grep -q '"interviews"'; }

requirements_problem() {
  if [ "$(uname -m)" != "arm64" ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != "1" ]; then
    echo "Il faut un Mac avec une puce Apple (M1 ou plus récent). Ce Mac a un processeur Intel."
    return
  fi
  local version major minor
  version="$(sw_vers -productVersion)"
  major="$(echo "$version" | cut -d. -f1)"
  minor="$(echo "$version" | cut -d. -f2)"
  # MLX refuse de démarrer en dessous de macOS 13.5.
  if [ "$major" -lt 13 ] || { [ "$major" -eq 13 ] && [ "${minor:-0}" -lt 5 ]; }; then
    echo "Il faut macOS 13.5 ou plus récent (ce Mac a macOS $version). Mettez-le à jour gratuitement dans Réglages Système > Général > Mise à jour de logiciels, puis relancez."
  fi
}

expected_stamp() { printf '%s\n' "${PACKAGES[@]}" "$ASR_MODEL" "$ALIGNER_MODEL" "$VOICE_MODEL"; }

installed() {
  [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$(expected_stamp)" ] \
    && [ -f "$APP_HOME/models/$VOICE_MODEL" ] && [ -x "$VENV/bin/python" ]
}

install_everything() {
  local free_kb
  free_kb="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
  if [ "$free_kb" -lt 12000000 ]; then
    fail "Pas assez d'espace disque : il faut au moins 12 Go libres."
  fi

  mkdir -p "$APP_HOME/bin" "$APP_HOME/models"
  note "1/4" "Préparation de l'environnement Python…"
  if [ ! -x "$UV" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="$APP_HOME/bin" sh
    [ -x "$UV" ] || fail "Impossible d'installer uv. Vérifiez la connexion internet et relancez."
  fi

  note "2/4" "Installation du logiciel de transcription…"
  if [ ! -x "$VENV/bin/python" ]; then "$UV" venv --python "$PYTHON_VERSION" "$VENV"; fi
  local packages=("${PACKAGES[@]}")
  # Les versions récentes de MLX ne gèrent que macOS 14+ ; 0.29.3 est la dernière pour macOS 13.
  if [ "$(sw_vers -productVersion | cut -d. -f1)" -lt 14 ]; then packages+=("mlx==0.29.3"); fi
  retry "$UV" pip install --python "$VENV/bin/python" "${packages[@]}" \
    || fail "Le téléchargement des logiciels a échoué. Vérifiez la connexion internet et relancez."
  "$VENV/bin/python" -c 'import mlx.core' \
    || fail "Le logiciel de transcription ne peut pas fonctionner sur ce Mac."

  note "3/4" "Téléchargement des modèles de transcription (environ 6 Go)…"
  retry "$VENV/bin/python" -c 'import sys; from huggingface_hub import snapshot_download; [snapshot_download(m) for m in sys.argv[1:]]' "$ASR_MODEL" "$ALIGNER_MODEL" \
    || fail "Le téléchargement des modèles a échoué. Relancez : les fichiers déjà reçus sont conservés."

  note "4/4" "Téléchargement du modèle de reconnaissance des voix…"
  if [ ! -f "$APP_HOME/models/$VOICE_MODEL" ]; then
    retry curl -fL --progress-bar --retry 3 --retry-delay 2 -C - -o "$APP_HOME/models/$VOICE_MODEL.part" "$VOICE_MODEL_URL" \
      || fail "Le téléchargement du modèle de reconnaissance des voix a échoué. Relancez."
    mv "$APP_HOME/models/$VOICE_MODEL.part" "$APP_HOME/models/$VOICE_MODEL"
  fi

  expected_stamp > "$STAMP"
}

# Le code de l'application voyage à côté de ce fichier ; on garde la copie installée à jour.
sync_app_code() {
  if [ -d "$SCRIPT_DIR/interviews" ]; then
    rm -rf "$APP_DIR.new"
    mkdir -p "$APP_DIR.new"
    cp -R "$SCRIPT_DIR/interviews" "$APP_DIR.new/"
    rm -rf "$APP_DIR"
    mv "$APP_DIR.new" "$APP_DIR"
  elif [ ! -d "$APP_DIR/interviews" ]; then
    fail "Les fichiers de l'application sont introuvables. Retéléchargez Interviews."
  fi
}

pick_port() {
  local port=$FIRST_PORT
  # SO_REUSEADDR : sans lui, un port tout juste libéré paraît occupé et l'adresse changerait à chaque fois
  while ! "$VENV/bin/python" -c 'import socket, sys; s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", int(sys.argv[1])))' "$port" 2>/dev/null; do
    port=$((port + 1))
    [ "$port" -lt $((FIRST_PORT + 20)) ] || fail "Aucun port local disponible pour l'application."
  done
  echo "$port"
}

start_server() {  # démarre en arrière-plan, affiche le PID
  sync_app_code
  local port pid
  port="$(pick_port)"
  echo "$port" > "$PORT_FILE"
  cd "$APP_DIR"
  HF_HUB_OFFLINE=1 INTERVIEWS_LIBRARY="$APP_HOME/library" INTERVIEWS_MODELS="$APP_HOME/models" \
    INTERVIEWS_ASR_MODEL="$ASR_MODEL" INTERVIEWS_APP_HOME="$APP_HOME" INTERVIEWS_VERSION="$VERSION" \
    nohup "$VENV/bin/python" -m interviews.server --port "$port" >"$SERVER_LOG" 2>&1 &
  pid=$!
  echo "$pid" > "$PID_FILE"
  echo "$pid"
}

running_version() {  # vide pour les instances d'avant la version 3, qui ne l'annonçaient pas
  curl -s --max-time 1 "http://127.0.0.1:$1/api/health" 2>/dev/null | grep -o '"version":[0-9]*' | cut -d: -f2 || true
}

transcribing() {
  curl -s --max-time 2 "http://127.0.0.1:$1/api/interviews" 2>/dev/null | grep -qE '"status":"(queued|processing)"'
}

stop_server() {  # seulement l'instance de ce dossier d'installation, jamais celles d'un autre
  if [ -f "$PID_FILE" ]; then kill "$(cat "$PID_FILE")" 2>/dev/null || true; fi
  pkill -f "$VENV/bin/python -m interviews.server" 2>/dev/null || true
  pkill -f "$VENV/bin/python -m interviews.pipeline" 2>/dev/null || true
  rm -f "$PORT_FILE" "$PID_FILE"
}

case "$MODE" in
  check)
    update_then_continue
    problem="$(requirements_problem)"
    if [ -n "$problem" ]; then echo "$problem"; echo "$problem" >&2; exit 2; fi
    if installed; then echo "ok"; else echo "need-install"; fi
    ;;

  install)
    mkdir -p "$APP_HOME"
    install_everything
    echo "done" > "$PROGRESS"
    ;;

  install-start)
    mkdir -p "$APP_HOME"
    printf 'running|1/4|Préparation…\n' > "$PROGRESS"
    nohup "$0" install > "$INSTALL_LOG" 2>&1 &
    echo $!
    ;;

  install-state)
    cat "$PROGRESS" 2>/dev/null || echo "running|1/4|Préparation…"
    ;;

  serve)
    start_server
    ;;

  port)  # appelé à chaque ouverture de l'app : rouvre l'instance en cours, sauf si elle est dépassée
    if [ -f "$PORT_FILE" ] && health "$(cat "$PORT_FILE")"; then
      port="$(cat "$PORT_FILE")"
      started=$(stat -f %m "$PID_FILE" 2>/dev/null || echo 0)
      # une instance qui vient de démarrer est celle qu'on attend : pas de nouvelle vérification pour elle
      if [ -z "${INTERVIEWS_SKIP_UPDATE:-}" ] && [ $(( $(date +%s) - started )) -gt 60 ] \
         && ! transcribing "$port" && self_update; then
        INTERVIEWS_DELEGATED=1 INTERVIEWS_SKIP_UPDATE=1 exec /bin/bash "$UPDATE_DIR/Interviews.command" port
      fi
      running="$(running_version "$port")"
      if [ "${running:-0}" -lt "$VERSION" ] && ! transcribing "$port"; then
        stop_server  # rien d'affiché : l'app redémarre alors la bonne version
      else
        echo "$port"
      fi
    fi
    ;;

  version)
    echo "$VERSION"
    ;;

  stop)
    stop_server
    ;;

  all)  # usage direct dans le Terminal, pratique pour voir ce qui se passe
    update_then_continue
    problem="$(requirements_problem)"
    [ -z "$problem" ] || fail "$problem"
    if [ -f "$PORT_FILE" ] && health "$(cat "$PORT_FILE")"; then
      open "http://127.0.0.1:$(cat "$PORT_FILE")/"
      echo "Interviews tourne déjà : le navigateur vient d'être rouvert."
      exit 0
    fi
    installed || { step "Première installation (environ 7 Go, 15 à 45 minutes)…"; install_everything; }
    pid="$(start_server)"
    for _ in $(seq 1 120); do
      health "$(cat "$PORT_FILE")" && break
      kill -0 "$pid" 2>/dev/null || fail "L'application n'a pas pu démarrer. Voir $SERVER_LOG"
      sleep 0.5
    done
    health "$(cat "$PORT_FILE")" || fail "L'application ne répond pas. Voir $SERVER_LOG"
    open "http://127.0.0.1:$(cat "$PORT_FILE")/"
    echo "Interviews est ouvert : http://127.0.0.1:$(cat "$PORT_FILE")"
    echo "Pour l'arrêter : bouton « Quitter » dans la page, ou ./Interviews.command stop"
    ;;

  *)
    fail "Commande inconnue : $MODE"
    ;;
esac
