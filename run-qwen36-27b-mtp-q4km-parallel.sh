#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export MODELS_DIR="${MODELS_DIR:-$SCRIPT_DIR/models}"

export PUBLIC_HOST="0.0.0.0"
export MODEL="${MODEL:-$MODELS_DIR/Qwen3.6-27B-Q4_K_M.gguf}"
export ALIAS="${ALIAS:-local-model,Qwen3.6-27B-MTP-Q4_K_M}"
export CACHE_DIR="${CACHE_DIR:-$HOME/.cache/llama.cpp-launch-scripts/qwen36-27b-mtp-q4km}"
export KV_UNIFIED="true"  # each slot gets full n_ctx, VRAM scales with actual usage
#export CTX="${CTX:-360000}" # KV_UNIFIED=true means this is the pooled ctx of PARALLEL, in this case 2*120000 total, but each can use up to 240000
export CTX="${CTX:-240000}"
#export CTX="${CTX:-160000}"
export BATCH="${BATCH:-4096}"
#export UBATCH="${UBATCH:-4096}"
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
export REASONING="${REASONING:-off}"
export PARALLEL="${PARALLEL:-2}"
#export PARALLEL="${PARALLEL:-2}"
export PREFIX_CACHE_MAX_BYTES="${PREFIX_CACHE_MAX_BYTES:-100GiB}"
export EXTRA_FLAGS="${EXTRA_FLAGS:---no-mmproj}"
export TASKSET_CPUS="0-15"  # all P-cores + HT (16 logical threads)
export THREADS="${THREADS:-16}"
export SPLIT_MODE="${SPLIT_MODE:-layer}"

exec "$SCRIPT_DIR/run-lmcache-proxy-stack.sh" "$@"
