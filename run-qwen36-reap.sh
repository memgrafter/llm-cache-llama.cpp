#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/_shared.sh"

export MODEL="${MODEL:-$MODELS_DIR/Qwen3.6-28B-REAP.i1-IQ3_XXS.gguf}"
export CACHE_DIR="${CACHE_DIR:-$HOME/.cache/llama.cpp-launch-scripts/qwen36-reap-iq3xxs}"

exec "$SCRIPT_DIR/run-lmcache-proxy-stack.sh" "$@"
