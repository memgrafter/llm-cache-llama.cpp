---
id: lcl-7s7u
status: closed
deps: [lcl-0svq]
links: []
created: 2026-07-05T05:02:31Z
type: feature
priority: 2
assignee: memgrafter
tags: [proxy, slot-routing]
---
# Disk cache fallback: restore into idle slot when no in-memory slot matches

When get_best_slot returns None, check the on-disk trie cache (prefix_cache.PrefixCache) for a matching KV file. If found and an idle slot is available, restore the KV into that slot before routing. This maps from lmcache-proxy.py's try_disk_cache() method.
