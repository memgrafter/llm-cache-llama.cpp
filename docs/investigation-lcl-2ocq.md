---
ticket: lcl-2ocq
status: investigation
date: 2026-07-05
---

# Investigation: Cache constantly reloads instead of reusing matched KV cache in VRAM slot

## Root Cause

In `_lookup_and_restore_prefix`, when a prefix node is found and `_pick_slot_for_restore` returns a slot that **already has an ancestor of the matched node loaded**, the code **always calls `_restore_slot`** — even though llama.cpp can compute the delta from its existing prefix without needing a restore.

### The Flow (concrete example)

1. Request 1: 100k tokens → lookup finds node A (100k), restores into slot 0
2. Autosave after request 1 creates node B (120k) in the trie, parent = A
3. **Request 2**: 120k tokens → lookup finds node B (120k)
4. `_pick_slot_for_restore` walks B's ancestor chain: [B(120k), A(100k)]
5. Slot 0 has node A → ancestor match found, `best_shared_tok = 100k`
6. `req_tokens (120k) >= slot_tok (100k)` → reuse slot 0 ✓
7. **`_restore_slot(slot 0, node B's bin)` is called** ← wasteful!

The restore reloads ~120k tokens from disk into VRAM that already contains 100k of them.
llama.cpp could have just computed the 20k delta from its existing prefix.

### Why this happens every turn

Autosave creates a new node in the trie after each turn (e.g., B at 120k, C at 140k, D at 160k).
On the next request, lookup finds the newer node. The ancestor walk finds a match
(because slot 0 still tracks the older node A), so the slot is reused.
But the restore is always called because the code doesn't distinguish between:

- **Ancestor match** — slot already has a prefix of the matched node; llama.cpp can compute delta
- **Empty/evicted slot** — slot has nothing or unrelated data; restore is required

## The Fix

Skip the restore when there's an ancestor match. The slot already holds a prefix of the
matched node, and llama.cpp will detect the shared prefix and compute only the new tokens.

### How llama.cpp handles this

When a request is sent with `id_slot` pointing to a slot that has a loaded prefix:
- llama.cpp checks if the incoming prompt starts with the same tokens as the slot's prefix
- If yes, it reuses those tokens and only processes the delta (new tokens)
- The KV cache cells for the shared prefix are reused, no recomputation needed

So if slot 0 has 100k tokens loaded and we send a 120k-token request that shares those 100k,
llama.cpp will compute only the 20k delta — **no restore needed**.

### Implementation approach

Modify `_pick_slot_for_restore` to return `(slot_id, needs_restore)` instead of just `slot_id`:

```python
def _pick_slot_for_restore(self, node: dict | None, req_tokens: int) -> tuple[int, bool] | None:
    # ... existing logic ...
    
    # Ancestor match — slot has a prefix of the matched node.
    # llama.cpp can compute the delta — no restore needed.
    if best_slot is not None and ancestor_match:
        return (best_slot, False)  # needs_restore = False
    
    # Empty or evicted slot — must restore.
    if empty:
        return (empty[0], True)
    if target:
        return (target, True)
```

Then in `_lookup_and_restore_prefix`:

```python
slot_result = self._pick_slot_for_restore(node, req_tokens)
if slot_result is None:
    return None

slot, needs_restore = slot_result

if needs_restore:
    result = _restore_slot(slot, node["bin_file"], ...)
    # ... existing restore success/failure handling ...
else:
    # Slot already has ancestor of matched node — skip restore.
    ctx.restored_node_id = node["id"]
    ctx.restored_via = via
    log.info("prefix-cache routing to slot %d (no restore needed, ancestor match)", slot)
    return slot if self.slot_state is not None else None
```

### Impact on slot_state

When we skip the restore, `slot_state` is **not updated** — it still points to the older
ancestor node (e.g., node A at 100k). This means slot_state becomes slightly stale.

**Is this a problem?** No. The ancestor walk in `_pick_slot_for_restore` will still find
the match on subsequent requests because:
- For the same conversation: node A is an ancestor of all future nodes (B, C, D...),
  so the ancestor walk always finds a match → slot reused → restore skipped again.
- For a different conversation: no ancestor match → empty/evicted slot used → restore called.

The only cost is **one unnecessary restore** on the first request of a new conversation
(because slot_state thinks the slot has fewer tokens than it actually does, so we don't
realize the slot already has enough for the new request). This is wasteful but not broken.

**Alternative**: Update `slot_state.record()` with the matched node even when skipping restore.
This would make future ancestor walks find deeper matches sooner. However, this risks
making slot_state inaccurate about what's actually in the slot (it records a node that
was never restored), which could cause routing bugs if the conversation changes mid-flight.
The safer approach is to leave slot_state as-is.

### Impact on autosave

Autosave runs after each request completes. It saves the slot's actual state and creates
a new trie node based on the real token count. This works correctly regardless of whether
a restore was called, because autosave reads from the slot directly (not from slot_state).

### Performance impact

For a conversation with N turns, this fix eliminates **N-1 unnecessary restores**.
Each restore involves:
- Reading a ~200MB+ .bin file from disk
- POST /slots/N?action=restore to llama.cpp (which loads KV tensors into VRAM)
- Waiting for the restore to complete (can be seconds)

With this fix, only the **first** request in a conversation triggers a restore.
All subsequent requests route directly to the slot and let llama.cpp compute deltas.

### Edge cases

1. **Same conversation, slot has MORE tokens than matched node**: This shouldn't happen
   in practice because lookup finds the longest match. But if it did (e.g., due to a
   pruning gap), the ancestor walk would still find the match and skip the restore.
   llama.cpp would just use its existing larger prefix.

2. **Sibling node match** (autosave creates siblings): The sibling check in
   `_pick_slot_for_restore` is a special case for when parent_for couldn't find the
   restored slot's node. If we skip the restore here too, it could be risky — the sibling
   might have a slightly different prefix. **Keep the restore for sibling matches.**

3. **Single-slot mode** (no `slot_state`): The fix applies only to multi-slot mode.
   In single-slot mode, every request already triggers a restore because there's no
   slot tracking. A separate optimization could add basic tracking even in single-slot
   mode, but that's out of scope for this ticket.

4. **KV cache capacity**: Skipping restores actually helps with KV cache pressure because
   we're not loading the same data twice into the shared cell pool. This is related to
   lcl-vkx4 ("failed to find space in KV cache").

## Summary

The fix is straightforward: when `_pick_slot_for_restore` finds an ancestor match
(slot already has a prefix of the matched node), skip the restore and let llama.cpp
compute the delta. This eliminates redundant disk I/O and VRAM reloads on every turn.

</content>}npx tk update lcl-2ocq --status investigating --add-link investigation-lcl-2ocq.md