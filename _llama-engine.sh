#!/usr/bin/env bash
set -euo pipefail

# Llama.cpp backend engine for the LMCache proxy stack.
# All configuration is set by run-lmcache-proxy-stack.sh via env vars.
# Run directly with: ./_llama-engine.sh [--serve]

SERVE=0
if [[ "${1:-}" == "--serve" ]]; then
  SERVE=1
  shift
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export MODELS_DIR="${MODELS_DIR:-$SCRIPT_DIR/models}"
LLAMA_DIR="${LLAMA_DIR:-$HOME/clones/llama.cpp}"
LOCAL_TURBO_BUILD="${LOCAL_TURBO_BUILD:-$SCRIPT_DIR/builds/llama-cpp-turboquant-build-metal}"
LOCAL_B9222_BUILD="${LOCAL_B9222_BUILD:-$SCRIPT_DIR/builds/llama-b9222}"
MODEL="${MODEL:-$MODELS_DIR/Qwen3.6-28B-REAP.i1-IQ3_XXS.gguf}"
# MODEL="${MODEL:-$MODELS_DIR/Qwen3.6-28B-REAP.i1-IQ3_M.gguf}"
# GGML_METAL_NO_RESIDENCY is set by the proxy stack.

# Binary autodetect. Override with BIN=/path/to/llama-cli or SERVER_BIN=/path/to/llama-server if needed.
if [[ "$SERVE" == "1" ]]; then
  if [[ -n "${SERVER_BIN:-}" ]]; then
    BIN="$SERVER_BIN"
  elif command -v llama-server >/dev/null 2>&1; then
    BIN="$(command -v llama-server)"
  elif [[ -x "llama-server" ]]; then
    BIN="llama-server"
  elif [[ -x "$LOCAL_TURBO_BUILD/bin/llama-server" ]]; then
    BIN="$LOCAL_TURBO_BUILD/bin/llama-server"
  elif [[ -x "$LOCAL_B9222_BUILD/llama-server" ]]; then
    BIN="$LOCAL_B9222_BUILD/llama-server"
  elif [[ -x "$HOME/clones/llama-cpp-turboquant/build-metal/bin/llama-server" ]]; then
    BIN="$HOME/clones/llama-cpp-turboquant/build-metal/bin/llama-server"
  elif [[ -x "$LLAMA_DIR/build/bin/llama-server" ]]; then
    BIN="$LLAMA_DIR/build/bin/llama-server"
  else
    BIN="./llama-server"
  fi
else
  if [[ -n "${BIN:-}" ]]; then
    :
  elif [[ -x "$LOCAL_TURBO_BUILD/bin/llama-cli" ]]; then
    BIN="$LOCAL_TURBO_BUILD/bin/llama-cli"
  elif [[ -x "$LOCAL_B9222_BUILD/llama-cli" ]]; then
    BIN="$LOCAL_B9222_BUILD/llama-cli"
  elif [[ -x "./llama-cli" ]]; then
    BIN="./llama-cli"
  elif [[ -x "$LLAMA_DIR/build/bin/llama-cli" ]]; then
    BIN="$LLAMA_DIR/build/bin/llama-cli"
  elif [[ -x "$LLAMA_DIR/build/bin/main" ]]; then
    BIN="$LLAMA_DIR/build/bin/main"
  elif command -v llama-cli >/dev/null 2>&1; then
    BIN="$(command -v llama-cli)"
  else
    BIN="./llama-cli"
  fi
fi

# Context/memory knobs. Set by the proxy stack via env vars.
NPRED="${NPRED:-8192}"
THREADS="${THREADS:-8}"

# Server knobs. Set by the proxy stack via env vars.

# Speculative decoding. Set by the proxy stack via env vars.
# SPEC_TYPE, SPEC_NGRAM_MOD_N_MATCH/MIN/MAX are read from env.
SPEC_NGRAM_MOD_N_MIN_EFFECTIVE="$SPEC_NGRAM_MOD_N_MIN"
SPEC_NGRAM_MOD_N_MAX_EFFECTIVE="$SPEC_NGRAM_MOD_N_MAX"
if [[ ",$SPEC_TYPE," == *,ngram-mod,* && "$BATCH" =~ ^[0-9]+$ && "$SPEC_NGRAM_MOD_N_MAX" =~ ^[0-9]+$ ]]; then
  # llama-server verifies the sampled token plus draft tokens in one logical batch.
  # Keep ngram-mod's draft length within BATCH-1 to avoid overflowing llama_batch.
  spec_ngram_mod_batch_limit=$(( BATCH > 1 ? BATCH - 1 : 0 ))
  if (( SPEC_NGRAM_MOD_N_MAX_EFFECTIVE > spec_ngram_mod_batch_limit )); then
    SPEC_NGRAM_MOD_N_MAX_EFFECTIVE="$spec_ngram_mod_batch_limit"
  fi
  if [[ "$SPEC_NGRAM_MOD_N_MIN" =~ ^[0-9]+$ ]] && (( SPEC_NGRAM_MOD_N_MIN_EFFECTIVE > SPEC_NGRAM_MOD_N_MAX_EFFECTIVE )); then
    SPEC_NGRAM_MOD_N_MIN_EFFECTIVE="$SPEC_NGRAM_MOD_N_MAX_EFFECTIVE"
  fi
fi

# Runtime behavior. FLASH_ATTN, KV_OFFLOAD, MLOCK set by the proxy stack via env vars.
MLock="${MLOCK:-0}"
TEMP="${TEMP:-0.6}"
TOP_P="${TOP_P:-0.95}"
TOP_K="${TOP_K:-20}"
MIN_P="${MIN_P:-0.0}"
REPEAT_PENALTY="${REPEAT_PENALTY:-1.05}"
SEED="${SEED:--1}"
PROMPT="${PROMPT:-Write a short test response.}"
PROMPT_FILE="${PROMPT_FILE:-}"
SINGLE_TURN="${SINGLE_TURN:-1}"
SIMPLE_IO="${SIMPLE_IO:-1}"
DISPLAY_PROMPT="${DISPLAY_PROMPT:-0}"
CONVERSATION="${CONVERSATION:-auto}"

# TurboQuant. Set by the proxy stack via env vars.

if [[ ! -x "$BIN" ]]; then
  echo "llama binary is not executable or not found: $BIN" >&2
  echo "Build llama.cpp first, or run with BIN=/path/to/llama-cli" >&2
  exit 2
fi

if [[ ! -f "$MODEL" ]]; then
  echo "model file not found yet: $MODEL" >&2
  echo "Override with MODEL=/path/to/model.gguf if needed." >&2
  exit 2
fi

args=(
  --model "$MODEL"
  --ctx-size "$CTX"
  --gpu-layers "$NGL"
  --threads "$THREADS"
  --batch-size "$BATCH"
  --ubatch-size "$UBATCH"
  --cache-type-k "$CACHE_K"
  --cache-type-v "$CACHE_V"
  --temp "$TEMP"
  --top-p "$TOP_P"
  --top-k "$TOP_K"
  --min-p "$MIN_P"
  --repeat-penalty "$REPEAT_PENALTY"
  --seed "$SEED"
)

  if [[ -n "${DEVICE:-}" ]]; then
    args+=(--device "$DEVICE")
  fi

# Multi-GPU split mode. Set by the wrapper script via SPLIT_MODE env var.
# Only passed when explicitly set; otherwise llama.cpp uses its own default.
if [[ -n "${SPLIT_MODE:-}" ]]; then
  args+=(--split-mode "$SPLIT_MODE")
fi

if [[ "$SERVE" == "1" ]]; then
  [[ -n "$SLOT_SAVE_PATH" ]] && mkdir -p "$SLOT_SAVE_PATH"

  args+=(
    --host "$HOST"
    --port "$PORT"
    --alias "$ALIAS"
    --parallel "$PARALLEL"
    ${KV_UNIFIED:+--kv-unified}
    --no-warmup
    --reasoning "${REASONING:-on}"
    --metrics
  )

  if [[ -n "${REASONING_MAX_TOKENS:-}" ]]; then
    args+=(--reasoning-budget "$REASONING_MAX_TOKENS")
  fi

  if [[ -n "$SLOT_SAVE_PATH" ]]; then
    args+=(--slot-save-path "$SLOT_SAVE_PATH")
  fi

  if [[ -n "$CACHE_RAM" ]]; then
    args+=(--cache-ram "$CACHE_RAM")
  fi

  if [[ "$CACHE_REUSE" != "0" ]]; then
    args+=(--cache-reuse "$CACHE_REUSE")
  fi
else
  args+=(--n-predict "$NPRED")

  if [[ -n "$PROMPT_FILE" ]]; then
    args+=(--file "$PROMPT_FILE")
  else
    args+=(--prompt "$PROMPT")
  fi

  case "$CONVERSATION" in
    0|off|OFF|false|FALSE|no|NO) args+=(--no-conversation) ;;
    1|on|ON|true|TRUE|yes|YES) args+=(--conversation) ;;
    auto|AUTO) : ;;
    *) args+=("$CONVERSATION") ;;
  esac

  if [[ "$SINGLE_TURN" != "0" ]]; then
    args+=(--single-turn)
  fi

  if [[ "$SIMPLE_IO" != "0" ]]; then
    args+=(--simple-io)
  fi

  if [[ "$DISPLAY_PROMPT" == "0" ]]; then
    args+=(--no-display-prompt)
  fi
fi

if [[ "$MTP" != "0" ]]; then
  args+=(--spec-type draft-mtp --spec-draft-n-max "$MTP" --spec-draft-n-min "$MTP")
elif [[ -n "$SPEC_TYPE" && "$SPEC_TYPE" != "none" ]]; then
  args+=(--spec-type "$SPEC_TYPE")
  if [[ ",$SPEC_TYPE," == *,ngram-mod,* ]]; then
    args+=(
      --spec-ngram-mod-n-match "$SPEC_NGRAM_MOD_N_MATCH"
      --spec-ngram-mod-n-min "$SPEC_NGRAM_MOD_N_MIN_EFFECTIVE"
      --spec-ngram-mod-n-max "$SPEC_NGRAM_MOD_N_MAX_EFFECTIVE"
    )
  fi
fi

case "$FLASH_ATTN" in
  0|off|OFF|false|FALSE) args+=(--flash-attn off) ;;
  1|on|ON|true|TRUE) args+=(--flash-attn on) ;;
  auto|AUTO) args+=(--flash-attn auto) ;;
  *) args+=(--flash-attn "$FLASH_ATTN") ;;
esac

if [[ "$KV_OFFLOAD" == "0" ]]; then
  args+=(--no-kv-offload)
fi

if [[ "$MLock" != "0" ]]; then
  args+=(--mlock)
fi

if [[ "$TURBOQUANT" != "0" ]]; then
  if [[ -n "$TURBOQUANT_FLAGS" ]]; then
    # Intentional word splitting for extra CLI flags.
    # shellcheck disable=SC2206
    tq_extra=( $TURBOQUANT_FLAGS )
    args+=("${tq_extra[@]}")
  elif "$BIN" --help 2>&1 | grep -q -- '--turboquant'; then
    args+=(--turboquant)
  elif "$BIN" --help 2>&1 | grep -q -- '--tq'; then
    args+=(--tq)
  fi
fi

if [[ -n "${EXTRA_FLAGS:-}" ]]; then
  # Intentional word splitting for ad-hoc CLI flags.
  # shellcheck disable=SC2206
  extra=( $EXTRA_FLAGS )
  args+=("${extra[@]}")
fi

echo "== Qwen3.6 REAP $( [[ "$SERVE" == "1" ]] && echo server || echo context probe ) =="
echo "BIN=$BIN"
echo "MODEL=$MODEL"
echo "CTX=$CTX NPRED=$NPRED NGL=$NGL THREADS=$THREADS BATCH=$BATCH UBATCH=$UBATCH"
echo "CACHE_K=$CACHE_K CACHE_V=$CACHE_V FLASH_ATTN=$FLASH_ATTN KV_OFFLOAD=$KV_OFFLOAD MLOCK=$MLock"
echo "GGML_METAL_NO_RESIDENCY=$GGML_METAL_NO_RESIDENCY iogpu.wired_limit_mb=$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo unknown)"
if [[ "$SERVE" == "1" ]]; then
  echo "SERVE=1 URL=http://$HOST:$PORT/v1 MODEL_ALIAS=$ALIAS PARALLEL=$PARALLEL MTP=$MTP SPEC_TYPE=$SPEC_TYPE SLOT_SAVE_PATH=${SLOT_SAVE_PATH:-<disabled>} CACHE_RAM=${CACHE_RAM:-<default>} CACHE_REUSE=$CACHE_REUSE"
else
  echo "CONVERSATION=$CONVERSATION SINGLE_TURN=$SINGLE_TURN SIMPLE_IO=$SIMPLE_IO DISPLAY_PROMPT=$DISPLAY_PROMPT PROMPT_FILE=${PROMPT_FILE:-<none>}"
fi
echo "SPEC_NGRAM_MOD_N_MATCH=$SPEC_NGRAM_MOD_N_MATCH SPEC_NGRAM_MOD_N_MIN=$SPEC_NGRAM_MOD_N_MIN_EFFECTIVE SPEC_NGRAM_MOD_N_MAX=$SPEC_NGRAM_MOD_N_MAX_EFFECTIVE"
echo "TURBOQUANT=$TURBOQUANT TURBOQUANT_FLAGS=${TURBOQUANT_FLAGS:-<auto/implicit>} EXTRA_FLAGS=${EXTRA_FLAGS:-<none>} SPLIT_MODE=${SPLIT_MODE:-<unset>}"
echo

# CPU pinning. Set by the proxy stack via env vars.
TASKSET_CPUS="${TASKSET_CPUS:-}"

if [[ "$SERVE" == "1" && -n "$TASKSET_CPUS" ]]; then
  exec taskset -c "$TASKSET_CPUS" "$BIN" "${args[@]}"
elif [[ "$SERVE" == "1" ]]; then
  exec "$BIN" "${args[@]}"
else
  exec "$BIN" "${args[@]}"
fi
