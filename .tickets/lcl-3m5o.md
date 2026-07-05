---
id: lcl-3m5o
status: open
deps: []
links: []
created: 2026-07-05T07:59:11Z
type: bug
priority: 2
assignee: memgrafter
tags: [proxy, slot-routing, prefix-cache]
---
# Investigate why autosave creates sibling nodes after restore instead of ancestor chain

After restoring a node (e.g. 122387) into a slot, autosave creates a new node (e.g. 122470) whose parent is NOT the restored node but an earlier ancestor (52297). This makes them siblings, breaking the ancestor walk in _pick_slot_for_restore.

Theories:
1. parent_for(122470) can't find node 122387 because the BLAKE2b hash at length 122387 differs between the restored bin and the autosave bin
2. The restored node isn't in the trie yet when autosave runs (unlikely — it was saved by a previous autosave)
3. Token ordering or padding differences between restore and autosave reads
4. lengths_leq doesn't include 122387 for some reason

Workaround: sibling check (same parent + <1% token diff) added in 24d9d4d.

Need to add debug logging to parent_for to trace the exact failure path.
