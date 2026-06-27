#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export MODELS_DIR="${MODELS_DIR:-$SCRIPT_DIR/models}"

export DEVICE="${DEVICE:-CUDA1}"
export MODEL="${MODEL:-$MODELS_DIR/Huihui-Qwen3.6-27B-abliterated-MTP-Q4_K.gguf}"
export ALIAS="${ALIAS:-local-model,Huihui-Qwen3.6-27B-abliterated-MTP-Q4_K}"
export CACHE_DIR="${CACHE_DIR:-$HOME/.cache/llama.cpp-launch-scripts/huihui-qwen36-27b-abliterated-mtp-q4k-nonthinking}"
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
