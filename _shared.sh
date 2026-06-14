#!/usr/bin/env bash
# Shared defaults for model-specific wrapper scripts.
# Source this from any run-*.sh that delegates to run-lmcache-proxy-stack.sh.
# Override any value with an env var before sourcing, or set model-specific
# values after sourcing (MODEL, CACHE_DIR).

# SCRIPT_DIR must be set by the caller before sourcing this file.
export MODELS_DIR="${MODELS_DIR:-$SCRIPT_DIR/models}"
export ALIAS="${ALIAS:-local-model}"
export CTX="${CTX:-60000}"
export CACHE_K="${CACHE_K:-turbo3}"
export CACHE_V="${CACHE_V:-turbo3}"
export SPEC_TYPE="${SPEC_TYPE:-ngram-mod}"
export MTP="${MTP:-0}"
export EXTRA_FLAGS="${EXTRA_FLAGS:---no-mmproj}"
