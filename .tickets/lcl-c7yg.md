---
id: lcl-c7yg
status: closed
deps: []
links: []
created: 2026-06-15T06:36:58Z
type: chore
priority: 2
assignee: memgrafter
---
# Unify config surface: move engine params up to proxy stack, rename TOP_K collision

Move THREADS, FLASH_ATTN, KV_OFFLOAD, MLOCK, SPEC_TYPE, SPEC_NGRAM_MOD_*, CACHE_REUSE, TURBOQUANT, TURBOQUANT_FLAGS, PARALLEL from _llama-engine.sh up to run-lmcache-proxy-stack.sh so proxy stack is the single config surface. Rename TOP_K to LMCACHE_TOP_K in proxy stack (collision with engine sampling TOP_K). Remove dead defaults from _llama-engine.sh for params now owned by proxy stack. Clean _shared.sh — remove params already set by proxy stack.
