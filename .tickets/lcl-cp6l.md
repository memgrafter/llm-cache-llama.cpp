---
id: lcl-cp6l
status: closed
deps: []
links: []
created: 2026-06-14T07:57:25Z
type: chore
priority: 2
assignee: memgrafter
---
# Extract run-qwen36-reap.sh engine into _llama-engine.sh, make new thin wrapper

Rename the 272-line run-qwen36-reap.sh (the llama-server backend engine) to _llama-engine.sh. Create a new thin run-qwen36-reap.sh that sources _shared.sh, sets MODEL + CACHE_DIR, and execs run-lmcache-proxy-stack.sh — same pattern as the other 4 wrappers. Update BACKEND_SCRIPT default in run-lmcache-proxy-stack.sh. Update doc references in CONFIGURATION.md, QUICKSTART.md, docs/spec-decoding-plan.md.
