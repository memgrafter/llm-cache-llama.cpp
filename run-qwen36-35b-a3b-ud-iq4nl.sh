#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export MODELS_DIR="${MODELS_DIR:-$SCRIPT_DIR/models}"

export MODEL="${MODEL:-$MODELS_DIR/Qwen3.6-35B-A3B-UD-IQ4_NL.gguf}"
export ALIAS="${ALIAS:-local-model}"
export CACHE_DIR="${CACHE_DIR:-$HOME/.cache/llama.cpp-launch-scripts/qwen36-35b-a3b-ud-iq4nl}"
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
export MTP="${MTP:-2}"
export EXTRA_FLAGS="${EXTRA_FLAGS:---no-mmproj}"

exec "$SCRIPT_DIR/run-lmcache-proxy-stack.sh" "$@"
