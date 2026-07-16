#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export MODELS_DIR="${MODELS_DIR:-$SCRIPT_DIR/models}"

export PUBLIC_HOST="0.0.0.0"
export DEVICE="${DEVICE:-CUDA0}"
export TASKSET_CPUS="0-7"  # first half of P-cores (HT) for CUDA0
export MODEL="${MODEL:-$MODELS_DIR/gemma-4-31B_q4_0-it.gguf}"
export ALIAS="${ALIAS:-local-model,gemma-4-31B-q4_0-it}"
export CACHE_DIR="${CACHE_DIR:-$HOME/.cache/llama.cpp-launch-scripts/gemma4-31b-q40-nonthinking}"
export CTX="${CTX:-100000}"
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
export REASONING="${REASONING:-on}"
export PREFIX_CACHE_MAX_BYTES="${PREFIX_CACHE_MAX_BYTES:-100GiB}"
export EXTRA_FLAGS="${EXTRA_FLAGS:-}"

exec "$SCRIPT_DIR/run-lmcache-proxy-stack.sh" "$@"
