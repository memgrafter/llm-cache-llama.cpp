#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export MODELS_DIR="${MODELS_DIR:-$SCRIPT_DIR/models}"

export MODEL="${MODEL:-$MODELS_DIR/Qwen3.6-35B-A3B-UD-IQ2_M.gguf}"
export ALIAS="${ALIAS:-local-model,Qwen3.6-35B-A3B-UD-IQ2_M}"
export CACHE_DIR="${CACHE_DIR:-$HOME/.cache/llama.cpp-launch-scripts/qwen36-35b-a3b-ud-iq2m}"
export CTX="${CTX:-60000}"
export BATCH="${BATCH:-4096}"
export UBATCH="${UBATCH:-1024}"
export CACHE_K="${CACHE_K:-turbo3}"
export CACHE_V="${CACHE_V:-turbo3}"
export SPEC_TYPE="${SPEC_TYPE:-ngram-mod}"
export MTP="${MTP:-0}"
export EXTRA_FLAGS="${EXTRA_FLAGS:---no-mmproj}"

exec "$SCRIPT_DIR/run-lmcache-proxy-stack.sh" "$@"
