#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export MODELS_DIR="${MODELS_DIR:-$SCRIPT_DIR/models}"

export PUBLIC_HOST="0.0.0.0"
export DEVICE="${DEVICE:-CUDA1}"
export MODEL="${MODEL:-$MODELS_DIR/Qwopus3.6-27B-v2-MTP-Q4_K_M.gguf}"
export ALIAS="${ALIAS:-local-model,Qwopus3.6-27B-v2-MTP-Q4_K_M}"
export CACHE_DIR="${CACHE_DIR:-$HOME/.cache/llama.cpp-launch-scripts/qwopus36-27b-v2-mtp-q4km-nonthinking-cuda1}"
export CTX="${CTX:-160000}"
export BATCH="${BATCH:-4096}"
export UBATCH="${UBATCH:-1024}"
if [[ "$(uname -s)" == "Linux" ]]; then
  # Mainline CUDA llama.cpp does not support TurboQuant's turbo3 KV cache type.
  export CACHE_K="${CACHE_K:-q8_0}"
  export CACHE_V="${CACHE_V:-q8_0}"
else
  export CACHE_K="${CACHE_K:-turbo3}"
  export CACHE_V="${CACHE_V:-turbo3}"
fi
export SPEC_TYPE="${SPEC_TYPE:-ngram-mod}"
export MTP="${MTP:-3}"
export REASONING="${REASONING:-off}"
export PREFIX_CACHE_MAX_BYTES="${PREFIX_CACHE_MAX_BYTES:-100GiB}"
export PUBLIC_PORT="${PUBLIC_PORT:-8091}"
export BACKEND_PORT="${BACKEND_PORT:-8092}"
export EXTRA_FLAGS="${EXTRA_FLAGS:---no-mmproj}"

exec "$SCRIPT_DIR/run-lmcache-proxy-stack.sh" "$@"
