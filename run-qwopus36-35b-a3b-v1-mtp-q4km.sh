#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export MODELS_DIR="${MODELS_DIR:-$SCRIPT_DIR/models}"

export MODEL="${MODEL:-$MODELS_DIR/Qwopus3.6-35B-A3B-v1-MTP-Q4_K_M.gguf}"
export ALIAS="${ALIAS:-local-model}"
export CACHE_DIR="${CACHE_DIR:-$HOME/.cache/llama.cpp-launch-scripts/qwopus36-35b-a3b-v1-mtp-q4km}"
export CTX="${CTX:-128000}"
if [[ "$(uname -s)" == "Linux" ]]; then
  # Mainline CUDA llama.cpp does not support TurboQuant's turbo3 KV cache type.
  export CACHE_K="${CACHE_K:-q8_0}"
  export CACHE_V="${CACHE_V:-q8_0}"
else
  export CACHE_K="${CACHE_K:-turbo3}"
  export CACHE_V="${CACHE_V:-turbo3}"
fi
export SPEC_TYPE="${SPEC_TYPE:-none}"
export MTP="${MTP:-2}"
export EXTRA_FLAGS="${EXTRA_FLAGS:---no-mmproj}"

exec "$SCRIPT_DIR/run-lmcache-proxy-stack.sh" "$@"
