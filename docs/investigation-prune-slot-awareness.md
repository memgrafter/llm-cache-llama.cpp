---
ticket: lcl-br9n (supplement)
date: 2026-07-05
---

# Investigation: _ensure_storage_room prunes nodes currently loaded in slots

## Problem

Removing the post-autosave prune (lcl-br9n) fixed half the problem. The other half:
`_ensure_storage_room` calls `prune_global` before each autosave to enforce the
cache budget. This prune deletes nodes that are **currently loaded in slots** because
they appear as LRU candidates (low hits, old last_used).

## Concrete flow from log (lmcache-proxy-20260705-103700.log)

1. Request 1: restore node 119009 into slot 0 → autosave creates 119087+119103
   → `_ensure_storage_room` prunes 119009+118981 (the just-restored nodes)
2. Request 2: restore node 119103 into slot 1 → `_ensure_storage_room` prunes
   119103+119087 (the just-restored nodes from slot 1!)
3. Request 3: lookup can't find newer nodes (pruned) → falls back to 115524 → restore
4. Request 4: ancestor match, no restore ✓ → autosave creates 119220+119258
   → `_ensure_storage_room` prunes 119258+119220 (again!)
5. Cycle repeats: create → prune → lookup fails → restore old node

## Root cause

The prune query in `prune_global` selects unpinned leaf nodes ordered by
COALESCE(last_used, created_at) ASC. It has **no awareness of slot_state** —
it doesn't know which nodes are currently loaded in VRAM slots.

A node that was just restored into a slot has:
- hits = 1 (touched once by lookup)
- last_used = recent timestamp
- But it's still the LRU candidate because all other nodes are even newer

## Why this matters

When a node is pruned from the DB, its .bin file is unlinked from disk.
The KV data in VRAM is fine (llama.cpp holds it), but on the next request:
- lookup can't find the node in the DB → falls back to an older ancestor
- Restore loads the older ancestor → prefill hit rate drops (e.g., 80% instead of 95%+)

## Proposed fix

Make `prune_global` aware of which nodes are currently tracked in slot_state.
Nodes that are loaded in a slot should be protected from pruning, even if
they're LRU candidates.

### Option A: Pass slot_state to prune_global

Add an optional parameter to `prune_global`:
```python
def prune_global(self, *, max_bytes, max_nodes, dry_run, protected_node_ids=None):
```

In the prune query, exclude protected nodes:
```sql
WHERE n.id NOT IN (protected_ids) AND ...
```

Call from `_ensure_storage_room`:
```python
protected = {self.slot_state.node_for(sid) for sid in self.slot_state.all_slot_ids()}
prune_global(..., protected_node_ids=protected)
```

### Option B: Pin tracked nodes temporarily

Before pruning, set `pinned=1` on all tracked nodes. After pruning, set them back.
This is simpler but involves extra DB writes.

### Option C: Increase cache budget

The cache is consistently ~2% over budget (102GB vs 100GB max). Increasing the
budget would reduce prune pressure. But this doesn't fix the fundamental issue
— under heavy load, pruning would still hit tracked nodes.

### Recommendation: Option A

It's the cleanest solution — explicit protection of in-use nodes without side
effects. The `protected_node_ids` parameter is optional so existing callers
(prune CLI, etc.) don't need changes.

## Note on VRAM vs disk

Pruning removes DB metadata and unlinks the .bin file from disk. The KV cache
in VRAM is unaffected — llama.cpp holds it independently. The problem only
arises when a new request needs to lookup the node (it's gone from the DB) or
when a slot needs to be restored from disk (the .bin is gone).

