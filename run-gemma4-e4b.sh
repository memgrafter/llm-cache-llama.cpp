#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/_shared.sh"

export MODEL="${MODEL:-$MODELS_DIR/gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf}"
export CACHE_DIR="${CACHE_DIR:-$HOME/.cache/llama.cpp-launch-scripts/gemma4-e4b-it-ud-q4kxl}"

exec "$SCRIPT_DIR/run-lmcache-proxy-stack.sh" "$@"
