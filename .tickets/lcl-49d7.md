---
id: lcl-49d7
status: closed
deps: []
links: []
created: 2026-07-05T05:02:07Z
type: feature
priority: 1
assignee: memgrafter
tags: [proxy, slot-routing]
---
# Track slot state in lmcache-proxy-on-demand: which slots have loaded KV, their hash and token count

The on-demand proxy currently uses a single slot_id = 0. Need to track per-slot state: which slots have loaded KV states, the prompt hash of their loaded KV, estimated token count, and last-used timestamp. This maps from lmcache-proxy.py's _slot_hash, _slot_tokens, and _slot_time dicts onto the on-demand proxy's architecture.
