---
id: lcl-k2ju
status: closed
deps: [lcl-49d7]
links: []
created: 2026-07-05T05:02:40Z
type: feature
priority: 2
assignee: memgrafter
tags: [proxy, slot-routing]
---
# LRU eviction: evict oldest slot when all slots are full and a new unmatched request arrives

When all k slots (e.g. --parallel=4) have loaded KV states and a new request arrives whose prompt hash doesn't match any loaded slot, evict the LRU slot: save its KV, restore the new request's KV into that slot, then route the request with id_slot set to the evicted slot.
