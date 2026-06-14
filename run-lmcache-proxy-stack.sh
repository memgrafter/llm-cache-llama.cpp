#!/usr/bin/env bash
set -euo pipefail

# Supervisor for the local LMCache proxy stack.
# It starts llama.cpp as a private backend, starts the proxy as the public endpoint,
# and stops llama.cpp whenever the proxy/supervisor stops.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SCRIPT_DIR"

BACKGROUND="${BACKGROUND:-0}"
for arg in "$@"; do
  case "$arg" in
    --background) BACKGROUND=1 ;;
    --foreground) BACKGROUND=0 ;;
    -h|--help)
      cat <<'EOF'
Usage: ./run-lmcache-proxy-stack.sh [--background|--foreground]

Starts supervised llama.cpp backend + LMCache proxy stack.
Foreground is the default. --background starts a detached supervisor and exits.
Configure ports/cache/model with environment variables documented in README.md.
EOF
      exit 0
      ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

PUBLIC_HOST="${PUBLIC_HOST:-127.0.0.1}"
PUBLIC_PORT="${PUBLIC_PORT:-8081}"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8082}"
CACHE_DIR="${CACHE_DIR:-$HOME/.cache/llama.cpp-launch-scripts/slot-kv}"
TOP_K="${TOP_K:-3}"
MIN_SAVE_TOKENS="${MIN_SAVE_TOKENS:-256}"
PREFIX_CACHE_MAX_BYTES="${PREFIX_CACHE_MAX_BYTES:-8GiB}"
PREFIX_CACHE_MIN_FREE_BYTES="${PREFIX_CACHE_MIN_FREE_BYTES:-512MiB}"
NO_AUTO_SAVE="${NO_AUTO_SAVE:-0}"
NO_PREFIX_CACHE="${NO_PREFIX_CACHE:-0}"
NO_GENERATED_PREFIX_CACHE="${NO_GENERATED_PREFIX_CACHE:-0}"
ALLOW_EXACT_PREFIX_RESTORE="${ALLOW_EXACT_PREFIX_RESTORE:-0}"
STOP_EXISTING="${STOP_EXISTING:-1}"
STARTUP_TIMEOUT="${STARTUP_TIMEOUT:-180}"

# Model/backend defaults for this stack. Override with env vars as needed.
export GGML_METAL_NO_RESIDENCY="${GGML_METAL_NO_RESIDENCY:-1}"
export CTX="${CTX:-60000}"
export NGL="${NGL:-999}"
export BATCH="${BATCH:-4096}"
export UBATCH="${UBATCH:-1024}"
export CACHE_K="${CACHE_K:-turbo3}"
export CACHE_V="${CACHE_V:-turbo3}"
export MTP="${MTP:-0}"
export HOST="$BACKEND_HOST"
export PORT="$BACKEND_PORT"
if [[ -z "${ALIAS:-}" && -n "${MODEL:-}" ]]; then
  model_alias="$(basename "${MODEL%.gguf}")"
  export ALIAS="local-model,$model_alias"
else
  export ALIAS="${ALIAS:-local-model}"
fi
export SLOT_SAVE_PATH="$CACHE_DIR"
export CACHE_RAM="${CACHE_RAM:-0}"
export BACKEND_SCRIPT="${BACKEND_SCRIPT:-_llama-engine.sh}"

LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/logs}"
mkdir -p "$LOG_DIR" "$CACHE_DIR"
STAMP="${STAMP:-$(date +%Y%m%d-%H%M%S)}"
BACKEND_LOG="${BACKEND_LOG:-$LOG_DIR/qwen36-backend-${STAMP}.log}"
PROXY_LOG="${PROXY_LOG:-$LOG_DIR/lmcache-proxy-${STAMP}.log}"
STACK_PID_FILE="${STACK_PID_FILE:-/tmp/lmcache-proxy-stack.pid}"
STACK_LOG="${STACK_LOG:-$LOG_DIR/stack-${STAMP}.log}"
PROXY_PID_FILE="${PROXY_PID_FILE:-/tmp/lmcache-proxy.pid}"
BACKEND_PID_FILE="${BACKEND_PID_FILE:-/tmp/qwen36-llamacpp-backend.pid}"

if [[ "$BACKGROUND" == "1" ]]; then
  echo "Starting supervised stack in background"
  echo "Supervisor log: $STACK_LOG"
  BACKGROUND=0 nohup "$0" --foreground > "$STACK_LOG" 2>&1 &
  bg_pid=$!
  echo "$bg_pid" > "$STACK_PID_FILE"
  echo "Stack supervisor PID: $bg_pid"
  echo "Stop with: kill \"\$(cat $STACK_PID_FILE)\""
  exit 0
fi

backend_pid=""
proxy_pid=""

echo $$ > "$STACK_PID_FILE"

stop_pid() {
  local pid="${1:-}"
  local label="${2:-process}"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Stopping $label PID $pid"
    kill "$pid" 2>/dev/null || true
  fi
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  stop_pid "$proxy_pid" "proxy"
  stop_pid "$backend_pid" "llama.cpp backend"
  if [[ -n "$proxy_pid" ]]; then wait "$proxy_pid" 2>/dev/null || true; fi
  if [[ -n "$backend_pid" ]]; then wait "$backend_pid" 2>/dev/null || true; fi
  [[ -f "$STACK_PID_FILE" ]] && [[ "$(cat "$STACK_PID_FILE" 2>/dev/null || true)" == "$$" ]] && rm -f "$STACK_PID_FILE"
  [[ -n "$proxy_pid" ]] && [[ -f "$PROXY_PID_FILE" ]] && [[ "$(cat "$PROXY_PID_FILE" 2>/dev/null || true)" == "$proxy_pid" ]] && rm -f "$PROXY_PID_FILE"
  [[ -n "$backend_pid" ]] && [[ -f "$BACKEND_PID_FILE" ]] && [[ "$(cat "$BACKEND_PID_FILE" 2>/dev/null || true)" == "$backend_pid" ]] && rm -f "$BACKEND_PID_FILE"
  exit "$status"
}
trap cleanup EXIT INT TERM

if [[ "$STOP_EXISTING" != "0" ]]; then
  for pid_file in "$PROXY_PID_FILE" "$BACKEND_PID_FILE"; do
    if [[ -f "$pid_file" ]]; then
      pid="$(cat "$pid_file" 2>/dev/null || true)"
      stop_pid "$pid" "$(basename "$pid_file")"
    fi
  done
  for port in "$PUBLIC_PORT" "$BACKEND_PORT"; do
    pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN || true)"
    if [[ -n "$pids" ]]; then
      echo "Stopping existing listener(s) on $port: $pids"
      # shellcheck disable=SC2086
      kill $pids 2>/dev/null || true
    fi
  done
  for _ in $(seq 1 30); do
    if ! lsof -tiTCP:"$PUBLIC_PORT" -sTCP:LISTEN >/dev/null 2>&1 && \
       ! lsof -tiTCP:"$BACKEND_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi

if lsof -tiTCP:"$PUBLIC_PORT" -sTCP:LISTEN >/dev/null 2>&1 || \
   lsof -tiTCP:"$BACKEND_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Public/backend port still busy; set STOP_EXISTING=1 or clear the listener manually." >&2
  lsof -nP -iTCP:"$PUBLIC_PORT" -sTCP:LISTEN >&2 || true
  lsof -nP -iTCP:"$BACKEND_PORT" -sTCP:LISTEN >&2 || true
  exit 1
fi

echo "Starting llama.cpp backend on $BACKEND_HOST:$BACKEND_PORT (via $BACKEND_SCRIPT)"
echo "Backend log: $BACKEND_LOG"
./"$BACKEND_SCRIPT" --serve > "$BACKEND_LOG" 2>&1 &
backend_pid=$!
echo "$backend_pid" > "$BACKEND_PID_FILE"

for i in $(seq 1 "$STARTUP_TIMEOUT"); do
  if curl -sS --max-time 2 "http://$BACKEND_HOST:$BACKEND_PORT/health" 2>/dev/null | grep -q '"status":"ok"'; then
    echo "Backend health OK after ${i}s"
    break
  fi
  if ! kill -0 "$backend_pid" 2>/dev/null; then
    echo "llama.cpp backend exited during startup" >&2
    tail -n 80 "$BACKEND_LOG" >&2 || true
    exit 1
  fi
  if [[ "$i" == "$STARTUP_TIMEOUT" ]]; then
    echo "Timed out waiting for backend health" >&2
    tail -n 80 "$BACKEND_LOG" >&2 || true
    exit 1
  fi
  sleep 1
done

echo "Backend slot starts empty; prefix cache restore happens on the first proxied request."

echo "Starting LMCache proxy on $PUBLIC_HOST:$PUBLIC_PORT -> $BACKEND_HOST:$BACKEND_PORT"
echo "Proxy log: $PROXY_LOG"
proxy_args=(
  --host "$PUBLIC_HOST"
  --port "$PUBLIC_PORT"
  --server "$BACKEND_HOST"
  --llama-port "$BACKEND_PORT"
  --cache-dir "$CACHE_DIR"
  --top-k "$TOP_K"
  --min-save-tokens "$MIN_SAVE_TOKENS"
  --prefix-cache-max-bytes "$PREFIX_CACHE_MAX_BYTES"
  --prefix-cache-min-free-bytes "$PREFIX_CACHE_MIN_FREE_BYTES"
)
if [[ "$NO_AUTO_SAVE" == "1" ]]; then proxy_args+=(--no-auto-save); fi
if [[ "$NO_PREFIX_CACHE" == "1" ]]; then proxy_args+=(--no-prefix-cache); fi
if [[ "$NO_GENERATED_PREFIX_CACHE" == "1" ]]; then proxy_args+=(--no-generated-prefix-cache); fi
if [[ "$ALLOW_EXACT_PREFIX_RESTORE" == "1" ]]; then proxy_args+=(--allow-exact-prefix-restore); fi

python3 lmcache-proxy-on-demand.py "${proxy_args[@]}" > "$PROXY_LOG" 2>&1 &
proxy_pid=$!
echo "$proxy_pid" > "$PROXY_PID_FILE"

for i in $(seq 1 30); do
  if lsof -tiTCP:"$PUBLIC_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Proxy listening after ${i}s"
    break
  fi
  if ! kill -0 "$proxy_pid" 2>/dev/null; then
    echo "LMCache proxy exited during startup" >&2
    cat "$PROXY_LOG" >&2 || true
    exit 1
  fi
  if [[ "$i" == "30" ]]; then
    echo "Timed out waiting for proxy listener" >&2
    cat "$PROXY_LOG" >&2 || true
    exit 1
  fi
  sleep 1
done

cat <<EOF

Stack ready.
Public endpoint: http://$PUBLIC_HOST:$PUBLIC_PORT/v1
Model alias:     $ALIAS
Backend:         http://$BACKEND_HOST:$BACKEND_PORT
Stack PID:       $$
Proxy PID:       $proxy_pid
Backend PID:     $backend_pid

Stop with:
  kill $(cat "$STACK_PID_FILE")

EOF

while true; do
  if ! kill -0 "$proxy_pid" 2>/dev/null; then
    echo "LMCache proxy stopped; stopping llama.cpp backend."
    wait "$proxy_pid" 2>/dev/null || true
    exit 0
  fi
  if ! kill -0 "$backend_pid" 2>/dev/null; then
    echo "llama.cpp backend stopped; stopping LMCache proxy." >&2
    stop_pid "$proxy_pid" "proxy"
    wait "$backend_pid" 2>/dev/null || true
    exit 1
  fi
  sleep 1
done
