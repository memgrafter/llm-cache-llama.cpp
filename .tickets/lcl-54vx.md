---
id: lcl-54vx
status: open
deps: []
links: [a111cb5]
created: 2026-07-05T17:16:13Z
type: feature
priority: 3
assignee: memgrafter
tags: [proxy, slot-routing, prefix-cache]
---
# Track slot state in single-slot mode to skip redundant restores

In single-slot mode (no --parallel), the proxy creates no SlotState and always
restores on every request — even when the slot already has the right prefix loaded.

After lcl-2ocq was fixed for multi-slot mode, this is the same optimization
applied to single-slot mode.

## Current behavior

When `--parallel` is not set (or == 1), `slot_state` is None and
`_pick_slot_for_restore` returns just `self.slot_id` (int). The caller in
`_lookup_and_restore_prefix` checks `isinstance(slot_result, tuple)` — it's not,
so it always calls `_restore_slot`. Every request triggers a disk restore.

## Proposed fix

Create `slot_state = SlotState()` even in single-slot mode. Then
`_pick_slot_for_restore` returns `(slot_id, needs_restore)` tuples as it already
does in multi-slot mode, and the same ancestor-match optimization applies.

### Implementation steps

1. In `main()`, change the condition that creates `slot_state`:
   - Currently: only when `args.parallel is not None and args.parallel > 1`
   - Change to: always create `SlotState()` regardless of parallel mode
   - Keep `n_parallel = None` for single-slot mode (so `_discover_slots` still
     returns None and the proxy doesn't try to discover slots from llama.cpp)

2. In `_pick_slot_for_restore`, change the single-slot early return:
   - Currently: `return self.slot_id` (int)
   - Change to: `return (self.slot_id, True)` — always restore since we may
     have stale state after autosave overwrites the slot

3. In `_lookup_and_restore_prefix`, the caller already handles tuples via
   `isinstance(slot_result, tuple)` check — no change needed there.

4. Update tests in `test_on_demand_slot_routing.py`:
   - `test_single_slot_mode_returns_hardcoded_slot` expects `0` (int), should
     expect `(0, True)` (tuple)

### Why always restore in single-slot mode?

In single-slot mode, multiple conversations share slot 0. After autosave saves
a new node, the slot's actual content may differ from what `slot_state` tracks.
The safest approach is to always restore in single-slot mode — same as current
behavior. The optimization (ancestor match → skip restore) only kicks in when
the ancestor walk finds a match AND the slot is reused, which requires
multi-slot tracking where each slot serves a distinct conversation.

Alternatively: if we want the optimization even in single-slot mode, we could
check if `slot_state.node_for(self.slot_id)` matches an ancestor of the looked-up
node. But this is riskier because autosave may have already overwritten the slot
with a different node since the last request. The conservative approach (always
restore) keeps the behavior identical to today.

### Performance impact

No change in single-slot mode — behavior is identical (always restore).
The benefit comes when someone later wants to add the optimization for
single-slot mode too — the infrastructure (slot_state, tuple returns) would
already be in place.

### Related

- lcl-2ocq: same optimization implemented for multi-slot mode
- The investigation doc at docs/investigation-lcl-2ocq.md has full context

