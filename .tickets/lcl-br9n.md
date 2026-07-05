---
id: lcl-br9n
status: closed
deps: []
links: []
created: 2026-07-05T17:34:14Z
type: bug
priority: 1
assignee: memgrafter
tags: [proxy, prefix-cache, prune]
---
# Remove post-autosave prune that deletes newly created nodes

Every autosave cycle creates 2 new nodes, then the post-autosave prune immediately deletes them because they have hits=0 and last_used=null. This means lookup on the next request can't find the newer nodes and falls back to an old ancestor, causing redundant restores every turn.

Fix: remove the cache.prune_global() call at the end of _auto_save_prefix_cache. Budget enforcement is already handled proactively by _ensure_storage_room before each autosave.
