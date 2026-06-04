#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

export MODEL="${MODEL:-$HOME/Downloads/gemma-4-12b-it-UD-Q4_K_XL.gguf}"
export ALIAS="${ALIAS:-gemma-4-12b-it-ud-q4kxl-turbo3-65k}"
export CACHE_DIR="${CACHE_DIR:-$HOME/.cache/llama.cpp-launch-scripts/gemma4-12b-it-ud-q4kxl}"
export CTX="${CTX:-65536}"
export CACHE_K="${CACHE_K:-turbo3}"
export CACHE_V="${CACHE_V:-turbo3}"
export SPEC_TYPE="${SPEC_TYPE:-ngram-mod}"
export MTP="${MTP:-0}"

exec "$SCRIPT_DIR/run-lmcache-proxy-stack.sh" "$@"
