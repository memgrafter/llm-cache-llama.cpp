# Prefix Cache Matching Investigation

**Date**: 2026-07-05
**Session**: lmcache-proxy-20260705-011611.log (multi-slot, 2 slots)
**Status**: Working but inefficient — every request triggers a restore instead of slot reuse

## Observation

Every single request (32 POST requests) triggers a KV cache restore into slot 0. Zero restores into slot 1. No slot reuse via the ancestor walk.

```
32 POST /v1/chat/completions → 32 "restored node ... into slot 0"
0 "routed to slot" messages (log.debug level, invisible at INFO)
0 errors, warnings, or threshold failures
```

## Expected Behavior

After request 1 restores an anchor (1841 tok) into slot 0:
- Request 2's lookup finds node 12525 (parent = anchor-43a5fd59-1841)
- Ancestor walk should find that slot 0 holds anchor-43a5fd59-1841, an ancestor of 12525
- Slot 0 should be reused WITHOUT restore — llama.cpp computes the delta

After request 2 restores node 12596 into slot 0:
- Request 3's lookup finds node 13027 (parent = 12596)
- Ancestor walk should find that slot 0 holds 12596, an ancestor of 13027
- Slot 0 should be reused WITHOUT restore

## Trie Structure (Verified in SQLite DB)

The trie is correct — proper parent-child chains exist:

```
anchor-43a5fd59-61 (61 tok, no parent)          ← root anchor
anchor-43a5fd59-1841 (1841 tok, no parent)      ← child anchor
  └─ 11775-1cdfa470... (11775 tok)
  └─ 12525-3d2de1f5... (12525 tok)
       └─ 12596-f539f7d0... (12596 tok)
            └─ 13027-f48da4aa... (13027 tok)
                 └─ 13050-af854325... (13050 tok)
```

Node IDs and parent_ids are consistent — full IDs with hash suffixes used throughout.

## Blind Alleys Explored

### 1. Node ID mismatch between slot_state and trie
**Hypothesis**: `slot_state.record()` stores a different node ID format than what the ancestor chain walks.
**Result**: Incorrect. DB shows consistent full IDs (e.g., `anchor-43a5fd59-1841-a25765b857a38005d743c3326adf230d`) in both `id` and `parent_id` columns. The code stores `node["id"]` from the lookup result, which comes directly from the DB.

### 2. cache.lookup() returns None on first request
**Hypothesis**: If lookup returns None, `_pick_slot_for_restore(node=None)` is called, skips ancestor walk entirely, falls through to empty slots.
**Result**: This IS what happens on request 1 — lookup finds nothing, materializes anchor, restores into slot 0. But subsequent requests DO find nodes (autosave created them), so this doesn't explain why restores continue.

### 3. strictly_less=True filters out the right node
**Hypothesis**: With `strictly_less=True`, `cache.lookup()` only returns nodes with token_count < request length. If the request has exactly N tokens, a node with N tokens is excluded.
**Result**: Unlikely to be the root cause. The request typically has more tokens than the matched node (e.g., request ~12600 tok, matched node 12525 tok). Even if strictly_less excludes one node, it should find another ancestor.

### 4. Threshold check rejects shallow matches
**Hypothesis**: After the first restore (anchor at 1841 tok), the shared prefix is only 1841 tok (< 5000 threshold). The 80% ratio rule requires req_tokens >= 1473. If request has fewer tokens, slot is rejected.
**Result**: Plausible for request 2 specifically — if the request had < 1473 tokens, slot 0 would be rejected and we'd fall through to eviction (still restoring into slot 0). But by request 3+, slot 0 holds a much larger node (12596 tok), so threshold should pass.

### 5. autosave invalidates slot_state
**Hypothesis**: After autosave creates new nodes in the trie, `slot_state` still points to the old node (e.g., anchor-43a5fd59-1841). The ancestor walk looks for this old node in the new trie structure.
**Result**: Partially plausible. After request 1, slot_state records anchor-43a5fd59-1841. After autosave creates node 12525 (parent = anchor), the ancestor walk should still find anchor as an ancestor of 12525. The trie structure is correct, so this shouldn't matter.

### 6. _lookup_and_restore_prefix returns None despite restoring
**Hypothesis**: The function restores a node but returns None, so `id_slot` is never injected into the request body.
**Result**: Code analysis shows `return slot if self.slot_state is not None else None`. Since `slot_state` IS set (multi-slot mode), this returns the slot ID. The "routed to slot" message is `log.debug` level, invisible at INFO — so routing IS happening, just not visible in logs.

### 7. Every restore overwrites slot 0, so ancestor walk always starts fresh
**Hypothesis**: Each restore into slot 0 replaces the KV cache entirely. Even though slot_state tracks the new node, the next request's lookup finds an even newer node, and the cycle repeats.
**Result**: This IS what's happening — every request restores a progressively larger node into slot 0 (1841 → 12596 → 13050 → ... → 36946). But the question remains: WHY isn't the ancestor walk finding that slot 0 holds a valid prefix?

### 8. Materialized anchor has different ID than trie anchor
**Hypothesis**: When `cache.lookup()` returns None and we materialize an anchor, the materialized node has a different ID than what's in the trie.
**Result**: Log shows the restored node is `anchor-43a5fd59-1841-a25765b857a38005d743c3326adf230d`, which matches the DB exactly. No mismatch.

### 9. Autosave creates sibling nodes, breaking ancestor chain
**Hypothesis**: Autosave creates nodes whose parent_for couldn't find the restored node, making them siblings instead of descendants.
**Result**: This IS a known issue (ticket lcl-3m5o). The sibling/grandparent checks were added as workarounds. But in this session's trie, the chains are correct — 12525 → anchor-43a5fd59-1841 is a proper parent-child relationship.

### 10. Global prune removes nodes that slot_state references
**Hypothesis**: Prune operations delete nodes from the trie that slot_state still references, breaking ancestor walks.
**Result**: Prune only deletes bin files and DB entries for LRU nodes. The anchor nodes and recent autosave nodes are too new to be pruned. Also, prune doesn't affect slot_state — it's in-memory tracking.

## Most Likely Root Cause

The ancestor walk in `_pick_slot_for_restore` should find matches but apparently doesn't. The most likely explanations:

1. **slot_state is stale after autosave**: After request 1 restores anchor into slot 0, autosave creates new nodes (12525, 12596). But `slot_state.record()` is NOT called during autosave — it only records the node that was RESTORED, not the node that was SAVED. So after autosave, slot_state still thinks slot 0 holds anchor-43a5fd59-1841, but the actual KV cache in slot 0 contains the full 12596-token prefix.

2. **The restore happens BEFORE slot_state is updated**: On request 2, lookup finds node 12525. The ancestor walk checks if slot 0 holds an ancestor of 12525. Slot 0 should hold anchor-43a5fd59-1841 (from request 1's record). But if the threshold check fails (1841 < 5000, need 80% of 1841 = 1473 tok), slot 0 is rejected. We fall through to empty/eviction, restore into slot 0, and update slot_state to node 12525.

3. **llama.cpp's internal prefix matching compensates**: Even though the proxy restores every time, llama.cpp detects that the restored prefix overlaps with what's already loaded and skips redundant computation. This makes the system "work" despite the proxy's inefficiency.

## Key Insight: Why It "Works"

The system works because:
- Each restore loads a KV cache that includes the previous conversation's prefix
- llama.cpp detects prefix overlap and only computes the delta
- The proxy's slot routing (`id_slot` injection) routes to the right slot
- But the proxy does unnecessary work: restoring when it could just route

## Remaining Questions

1. Does the ancestor walk actually execute? (No debug logging at INFO level)
2. Is the threshold check rejecting valid matches on request 2?
3. Would concurrent conversations expose the routing bug? (Slot 1 is never used because there's only one conversation)
4. Does `slot_state` need to be updated during autosave to reflect the current slot contents?

