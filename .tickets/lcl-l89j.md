---
id: lcl-l89j
status: closed
deps: []
links: []
created: 2026-07-06T04:11:52Z
type: bug
priority: 1
assignee: memgrafter
tags: [proxy, caching, race]
---
# Per-slot busy tracking to prevent concurrent routing

ThreadingHTTPServer handles requests concurrently. _cache_lock serializes _lookup_and_restore_prefix and _auto_save_prefix_cache, but NOT the backend processing window. Two requests can be routed to the same slot back-to-back before either finishes — when MTP truncates on one, checkpoints for the other are invalidated with no way to detect it. Need per-slot busy flag: mark slot in-use when routed, clear when response completes. Busy slots should be treated as unavailable during routing.
