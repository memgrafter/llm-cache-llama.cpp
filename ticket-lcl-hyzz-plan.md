# Ticket lcl-hyzz: Update slot_state after ancestor-match routing

## Problem Statement

When `_pick_slot_for_restore` returns an ancestor match (`needs_restore=False`), it only calls `slot_state.touch()` — the `node_id` and `token_count` are never updated. If the backend grows beyond the tracked node via delta processing, `slot_state` holds stale data. Subsequent ancestor-match checks use wrong baselines, causing incorrect routing decisions and checkpoint invalidation from MTP truncation.

### Root Cause Flow

1. **Session A** routes to slot 0 with node 1841 (via restore). `slot_state.record(0, "1841", N)` is called.
2. **Session A** sends a follow-up. `_pick_slot_for_restore` finds ancestor match on slot 0 (node 1841 is an ancestor of the matched node). Returns `(0, False)`. Only `slot_state.touch(0)` is called — `node_id` stays "1841", `token_count` stays N.
3. **Backend** processes Session A's prompt via delta: it extends KV cache from position N to some larger M (e.g., N + 500 tokens). The slot now has M tokens loaded, but the proxy still thinks it has N.
4. **Session B** arrives. `_pick_slot_for_restore` checks slot 0: `node_for(0)` = "1841", `tokens_for(0)` = N. It finds that node 1841 is an ancestor of Session B's matched node too, so it routes to slot 0 again (ancestor match).
5. **Backend** processes Session B. The common prefix between Session B's prompt and the slot's actual KV is only K tokens (K < M, because Session B diverged earlier). `pos_next` = K + 1.
6. **MTP truncation**: All checkpoints with `pos_max > pos_next` are erased. This destroys checkpoints that Session A was relying on.
7. **Proxy blindness**: The proxy never knew the slot had grown from N to M, so it couldn't have detected the mismatch.

### Why the proxy can't know the exact new state

The proxy sends the original request to llama.cpp unchanged. The backend computes `n_past` internally (the common prefix length), then processes only the delta. The proxy has no visibility into:
- What `n_past` was computed as
- How many tokens were actually processed in the delta
- Whether checkpoints were created or invalidated during processing

The backend's `/slots` endpoint reports `is_busy`, `is_processing`, and `current_token`, but does **not** report the current loaded node or token count for a slot's KV cache.

## Design Decision

Record the matched node after ancestor match. The matched node is the deepest ancestor in the trie that the slot already holds — it's provably correct because the slot has at least this much loaded.

## Implementation Plan

### Changes to `_pick_slot_for_restore` (lines ~695-745)

In the ancestor match path (around line 740):

```python
if best_slot is not None:
    slot_tok = self.slot_state.tokens_for(best_slot)
    if slot_tok is not None and req_tokens >= slot_tok:
        self.slot_state.touch(best_slot)
        # Ancestor match: slot already has a prefix of the matched node.
        return (best_slot, False)
```

Change to:

```python
if best_slot is not None:
    slot_tok = self.slot_state.tokens_for(best_slot)
    if slot_tok is not None and req_tokens >= slot_tok:
        self.slot_state.record(best_slot, best_shared_node_id, best_shared_tok)
        return (best_slot, False)
```

Where `best_shared_node_id` and `best_shared_tok` are the deepest ancestor node id and token count found during the ancestor walk.

### Additional tracking: `best_shared_node_id`

Currently the ancestor walk tracks `best_shared_tok` but not the corresponding node id. We need to add:

```python
best_slot = None
best_shared_tok = 0
best_shared_node_id = None  # NEW
```

And update inside the loop:

```python
if anc_tok > best_shared_tok:
    best_shared_tok = anc_tok
    best_slot = sid
    best_shared_node_id = anc_id  # NEW
```

Then use `best_shared_node_id` in the `record()` call.

### Edge Cases

1. **autosave after ancestor match**: If autosave runs after an ancestor-match request, it will save the slot and potentially create a new node. The next request's `_lookup_and_restore_prefix` will find this new node, and `slot_state.record()` will be called normally via the restore path. No conflict.

2. **Multiple ancestor matches in sequence**: Each ancestor match updates `slot_state` to the matched node. If Session A sends progressively longer prompts, each time the matched node moves deeper in the trie, and `slot_state` is updated accordingly. Correct behavior.

3. **Session B routes to slot 0 after Session A's ancestor match**: Now `slot_state` has the correct matched node from Session A's last request. If Session B's prompt shares this ancestor, it will correctly route to slot 0 (ancestor match). If not, it will check empty slots or evict. This is the desired behavior — the stale data problem is mitigated.

4. **What if `best_shared_node_id` is None?**: This shouldn't happen because we only enter the ancestor match path when `best_slot is not None`, which requires finding a matching ancestor. But add a guard: `if best_shared_node_id is not None: self.slot_state.record(...)`. Otherwise fall back to `touch()`.

## Impact Assessment

- **Risk**: Low. We're recording data we already know is loaded (the matched ancestor node). The only risk is if the trie lookup returns stale data, but that's a separate bug.
- **Testing**: Verify that after an ancestor match, subsequent routing decisions use the updated `slot_state`. Check that empty slots are still preferred when available.
- **Performance**: No measurable impact — one extra dict write in `slot_state.record()`.

## Acceptance Criteria

1. After an ancestor-match routing, `slot_state.node_for(slot)` returns the matched node id (not the previously tracked node).
2. After an ancestor-match routing, `slot_state.tokens_for(slot)` returns the matched node's token count.
3. Subsequent routing decisions use the updated state to correctly identify ancestor matches or route to empty slots.
4. No regression in sibling match or restore paths.

