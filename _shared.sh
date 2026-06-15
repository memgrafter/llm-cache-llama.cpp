#!/usr/bin/env bash
# Shared defaults for model-specific wrapper scripts.
# Source this from any run-*.sh that delegates to run-lmcache-proxy-stack.sh.
# All backend parameters (CTX, CACHE_K, SPEC_TYPE, etc.) are set by the proxy stack.

# SCRIPT_DIR must be set by the caller before sourcing this file.
export MODELS_DIR="${MODELS_DIR:-$SCRIPT_DIR/models}"
