---
id: lcl-5phf
status: closed
deps: []
links: []
created: 2026-07-05T17:50:54Z
type: bug
priority: 1
assignee: memgrafter
tags: [proxy, prefix-cache, prune]
---
# Set last_used when autosave creates nodes so LRU prune doesn't delete them

Newly autosaved nodes have last_used=null and hits=0, making them LRU prune candidates. _ensure_storage_room prunes them before the next lookup can touch them.

Fix: set last_used=utc_now() when inserting nodes in _auto_save_prefix_cache. Every node that exists has been used (someone saved it), so null is incorrect.

In _auto_save_prefix_cache, both the prompt node and generated node are created with 'last_used': None. Change to 'last_used': prefix_cache.utc_now().

Same applies to _evict_slot and _materialize_anchor_once — any place that inserts a node should set last_used.
