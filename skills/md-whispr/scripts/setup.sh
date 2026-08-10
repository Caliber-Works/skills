#!/usr/bin/env bash
# md-whispr setup — install the TTS backend, find a free port, start it, prove it works.
#
# Designed to be run BY AN AGENT as well as by a human: every stage prints a
# stable "STAGE <name> <ok|fail> <detail>" line, and the last line is always
# "RESULT <ok|fail> port=<n> url=<url> engine=<x>" so the caller can parse it
# without scraping prose.
#
#   bash setup.sh                 # install + start (default port 8000)
#   bash setup.sh --port 8080     # pin a port
#   bash setup.sh --venv ~/main-venv
#   bash setup.sh --check         # report only, change nothing
#   bash setup.sh --stop
#
# Idempotent. Re-running when everything is already up is a no-op that reports so.

set -uo pipefail

STATE_DIR="$HOME/.md-whispr"
LOG="$STATE_DIR/server.log"
PIDFILE="$STATE_DIR/server.pid"
ENVFILE="$STATE_DIR/env"
MODEL="${MD_WHISPR_MODEL:-mlx-community/Kokoro-82M-bf16}"
VOICE="${MD_WHISPR_VOICE:-af_heart}"
WANT_PORT=8000
VENV=""
ACTION="up"
PLAY=1

while [ $# -gt 0 ]; do
  case "$1" in
    --port)  WANT_PORT="$2"; shift 2 ;;
    --venv)  VENV="$2"; shift 2 ;;
    --check) ACTION="check"; shift ;;
    --stop)  ACTION="stop"; shift ;;
    --quiet) PLAY=0; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$STATE_DIR"

stage() { printf 'STAGE %-10s %-4s %s\n' "$1" "$2" "${3:-}"; }
result() { printf 'RESULT %s port=%s url=%s\n' "$1" "${2:-none}" "${3:-none}"; }
die() { stage "$1" fail "${2:-}"; result fail; exit 1; }

port_busy()     { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
server_health() { curl -fsS --max-time 3 "http://127.0.0.1:$1/v1/models" >/dev/null 2>&1; }

find_live_port() {
  for p in "$WANT_PORT" 8000 8001 8002 8003 8010 8080 8123; do
    server_health "$p" && { echo "$p"; return 0; }
  done
  return 1
}

# ------------------------------------------------------------------ stop/check

if [ "$ACTION" = "stop" ]; then
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")"; rm -f "$PIDFILE"; stage stop ok "killed pid from pidfile"
  elif pkill -f "mlx_audio.server" 2>/dev/null; then
    stage stop ok "killed stray mlx_audio.server"
  else
    stage stop ok "nothing was running"
  fi
  result ok; exit 0
fi

if [ "$ACTION" = "check" ]; then
  [ "$(uname -s)" = "Darwin" ] && stage os ok "macOS $(uname -m)" || stage os fail "not macOS"
  command -v afplay >/dev/null && stage afplay ok || stage afplay fail "missing"
  if P=$(find_live_port); then
    stage server ok "healthy on $P"
    result ok "$P" "http://127.0.0.1:$P/v1/audio/speech"
    exit 0
  fi
  stage server fail "no healthy server on 8000-8123"
  result fail
  exit 1
fi

# ------------------------------------------------------------------- 1 preflight

[ "$(uname -s)" = "Darwin" ] || die os "mlx-audio requires macOS (Apple Silicon). Detected $(uname -s)."
ARCH="$(uname -m)"
[ "$ARCH" = "arm64" ] || die os "mlx-audio requires Apple Silicon (arm64). Detected $ARCH."
stage os ok "macOS/$ARCH"

command -v afplay >/dev/null || die afplay "afplay not found — md-whispr needs it to play audio."
stage afplay ok "present"

# Venv resolution order: explicit flag > env var > an existing ~/main-venv >
# active venv > a private one under $STATE_DIR. The last branch is what a fresh
# install hits, so nothing is created outside ~/.md-whispr.
if [ -z "$VENV" ]; then
  if   [ -n "${MD_WHISPR_VENV:-}" ];        then VENV="$MD_WHISPR_VENV"
  elif [ -x "$HOME/main-venv/bin/python" ]; then VENV="$HOME/main-venv"
  elif [ -n "${VIRTUAL_ENV:-}" ];           then VENV="$VIRTUAL_ENV"
  else VENV="$STATE_DIR/venv"
  fi
fi

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" 2>/dev/null || die venv "could not create a virtualenv at $VENV"
  stage venv ok "created $VENV"
else
  stage venv ok "$VENV"
fi

PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
PYVER="$("$PY" -c 'import sys;print("%d%02d"%sys.version_info[:2])' 2>/dev/null || echo 0)"
[ "$PYVER" -ge 310 ] || die venv "mlx-audio needs Python 3.10+; $VENV has $("$PY" --version 2>&1)."

# --------------------------------------------------------------------- 2 install

if "$PY" -c "import mlx_audio" 2>/dev/null; then
  stage install ok "mlx-audio already present"
else
  "$PIP" install --quiet --upgrade pip >/dev/null 2>&1
  if ! "$PIP" install --quiet "mlx-audio[server]" misaki 2>"$STATE_DIR/pip-error.log"; then
    die install "pip install failed. Details in $STATE_DIR/pip-error.log; rerun verbosely with:
    $PIP install 'mlx-audio[server]' misaki"
  fi
  stage install ok "mlx-audio[server] + misaki installed"
fi

"$PY" -c "import misaki" 2>/dev/null || "$PIP" install --quiet misaki >/dev/null 2>&1
"$PY" -c "import misaki" 2>/dev/null \
  && stage misaki ok "present" \
  || die misaki "misaki is required for Kokoro text processing and would not install."

command -v ffmpeg >/dev/null \
  && stage ffmpeg ok "present" \
  || stage ffmpeg warn "missing — only affects 'render'; brew install ffmpeg"

# ------------------------------------------------------------------------ 3 port

REUSED=0
PORT=""
if P=$(find_live_port); then
  PORT="$P"; REUSED=1
  stage port ok "reusing healthy server already on $PORT"
else
  for candidate in "$WANT_PORT" 8001 8002 8003 8010 8080 8123; do
    if port_busy "$candidate"; then
      HOLDER="$(lsof -nP -iTCP:"$candidate" -sTCP:LISTEN -Fc 2>/dev/null | sed -n 's/^c//p' | head -1)"
      stage port warn "$candidate busy (${HOLDER:-unknown})"
      continue
    fi
    PORT="$candidate"; stage port ok "$candidate free"; break
  done
fi
[ -n "$PORT" ] || die port "no free port in 8000-8123. Free one or pass --port N."

# ----------------------------------------------------------------------- 4 start

if [ -x "$VENV/bin/mlx_audio.server" ]; then
  LAUNCH=("$VENV/bin/mlx_audio.server")
elif "$PY" -c "import mlx_audio.server" 2>/dev/null; then
  LAUNCH=("$PY" -m mlx_audio.server)
else
  die start "cannot locate the mlx-audio server entry point. Check: $PIP show mlx-audio"
fi
SERVER_CMD="${LAUNCH[*]}"

if [ "$REUSED" = "0" ]; then
  : > "$LOG"
  nohup "${LAUNCH[@]}" --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  stage start ok "pid $(cat "$PIDFILE"), log $LOG"
else
  stage start ok "already running"
fi

# ---------------------------------------------------------------------- 5 health

HEALTHY=0
for i in $(seq 1 60); do
  server_health "$PORT" && { HEALTHY=1; stage health ok "responding after ${i}s"; break; }
  if [ "$REUSED" = "0" ] && ! kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
    echo "--- last 25 log lines ---"; tail -25 "$LOG"
    die health "server process exited during startup"
  fi
  sleep 1
done
[ "$HEALTHY" = "1" ] || { tail -25 "$LOG"; die health "never became healthy within 60s"; }

# ------------------------------------------------------------------------ 6 warm

TESTWAV="$STATE_DIR/warmup.wav"
T0=$(date +%s)
HTTP=$(curl -sS -o "$TESTWAV" -w '%{http_code}' --max-time 900 \
  -X POST "http://127.0.0.1:$PORT/v1/audio/speech" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"input\":\"Whispr is online. Ready to read your documents.\",\"voice\":\"$VOICE\",\"speed\":1.15,\"lang_code\":\"a\",\"response_format\":\"wav\"}")
T1=$(date +%s)

[ "$HTTP" = "200" ] || { echo "--- last 25 log lines ---"; tail -25 "$LOG"; \
  die warm "synthesis returned HTTP $HTTP"; }
SIZE=$(stat -f%z "$TESTWAV" 2>/dev/null || echo 0)
[ "$SIZE" -gt 1000 ] || die warm "synthesis returned only ${SIZE} bytes, not audio"
stage warm ok "cold ${SIZE}B in $((T1-T0))s (model download happens here, once)"

T2=$(date +%s%N)
curl -sS -o /dev/null --max-time 60 -X POST "http://127.0.0.1:$PORT/v1/audio/speech" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"input\":\"Second pass.\",\"voice\":\"$VOICE\",\"lang_code\":\"a\",\"response_format\":\"wav\"}" 2>/dev/null
T3=$(date +%s%N)
stage latency ok "$(( (T3-T2)/1000000 ))ms warm — this is time-to-first-word"

# ------------------------------------------------------------------- 7 wire up

cat > "$ENVFILE" <<EOF
# written by md-whispr setup on $(date)
export MD_WHISPR_PORT=$PORT
export MD_WHISPR_TTS_URL=http://127.0.0.1:$PORT/v1/audio/speech
export MD_WHISPR_MODEL=$MODEL
export MD_WHISPR_VOICE=$VOICE
export MD_WHISPR_SPEED=${MD_WHISPR_SPEED:-1.15}
export MD_WHISPR_VENV=$VENV
EOF
stage env ok "$ENVFILE"

mkdir -p "$HOME/bin"
cat > "$HOME/bin/md-whispr-server" <<EOF
#!/usr/bin/env bash
# md-whispr TTS backend launcher (generated by md-whispr setup)
case "\${1:-start}" in
  stop)   pkill -f mlx_audio.server && echo stopped || echo "not running" ;;
  status) curl -fsS http://127.0.0.1:$PORT/v1/models >/dev/null 2>&1 \\
            && echo "healthy on $PORT" || echo "not running" ;;
  *)      if curl -fsS --max-time 2 http://127.0.0.1:$PORT/v1/models >/dev/null 2>&1; then
            echo "already running on $PORT"
          else
            nohup $SERVER_CMD --host 127.0.0.1 --port $PORT >> "$LOG" 2>&1 &
            echo "started on $PORT"
          fi ;;
esac
EOF
chmod +x "$HOME/bin/md-whispr-server"
stage launcher ok "~/bin/md-whispr-server [start|stop|status]"

SHELLRC="$HOME/.zshrc"; [ -n "${BASH_VERSION:-}" ] && [ ! -f "$SHELLRC" ] && SHELLRC="$HOME/.bashrc"
if [ -f "$SHELLRC" ] && ! grep -q 'md-whispr/env' "$SHELLRC" 2>/dev/null; then
  printf '\n# md-whispr TTS backend\n[ -f ~/.md-whispr/env ] && . ~/.md-whispr/env\n' >> "$SHELLRC"
  stage shellrc ok "sourced env from $SHELLRC"
else
  stage shellrc ok "already wired (or no rc file)"
fi

[ "$PLAY" = "1" ] && afplay "$TESTWAV" >/dev/null 2>&1 && stage playback ok "you should have heard a voice" \
  || stage playback warn "could not play test clip — check output device"

result ok "$PORT" "http://127.0.0.1:$PORT/v1/audio/speech"
