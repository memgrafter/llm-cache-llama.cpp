#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export MODELS_DIR="${MODELS_DIR:-$SCRIPT_DIR/models}"

export MODEL="${MODEL:-$MODELS_DIR/gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf}"
export ALIAS="${ALIAS:-local-model}"
export CACHE_DIR="${CACHE_DIR:-$HOME/.cache/llama.cpp-launch-scripts/gemma4-e4b-it-ud-q4kxl}"
export CTX="${CTX:-60000}"
export BATCH="${BATCH:-4096}"
export UBATCH="${UBATCH:-1024}"
export CACHE_K="${CACHE_K:-turbo3}"
export CACHE_V="${CACHE_V:-turbo3}"
export SPEC_TYPE="${SPEC_TYPE:-ngram-mod}"
export MTP="${MTP:-0}"
export EXTRA_FLAGS="${EXTRA_FLAGS:---no-mmproj}"

exec "$SCRIPT_DIR/run-lmcache-proxy-stack.sh" "$@"
