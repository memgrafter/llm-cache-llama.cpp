---
id: lcl-j4qp
status: closed
deps: []
links: []
created: 2026-07-05T03:18:29Z
type: feature
priority: 2
assignee: memgrafter
tags: [proxy, slot-routing]
---
# Evict oldest slot when all slots are full and a new unmatched request arrives

When all k slots (e.g. --parallel=4) have loaded KV states and a new request arrives whose prompt hash doesn't match any loaded slot, the proxy should evict the LRU slot: save its KV, restore the new request's KV into that slot, then route the request with id_slot set to the evicted slot. Uses lru_slot() and update_slot_time() already in SlotManager.

## Notes

**2026-07-05T03:50:41Z**

Edge case 1: minimum match threshold — shared system prompts give small matches but we don't want to yoink a long-prompt slot. Need at least 5000 tok or 80% of request context size (whichever is lower). Edge case 2: if no slot matches, still check on-disk trie cache for forks.

**2026-07-05T03:51:25Z**

Edge case 1: minimum match threshold — track token count per slot, reject match if <5000 tok or <80% of request context (whichever lower). Prevents routing short system-prompt-only requests to slots loaded with long conversations. Edge case 2: disk cache fallback — if no slot matches, check on-disk trie for cached KV, restore into idle slot, then route.

**2026-07-05T04:29:56Z**

Split: slot lock race condition -> lcl-notu, update _slot_tokens from response -> lcl-nqwf, threshold tuning investigation -> lcl-15mz

**2026-07-05T04:29:59Z**

Still TODO: actual eviction logic (save LRU slot, restore new KV, route). lru_slot() exists but is unused in handler.
