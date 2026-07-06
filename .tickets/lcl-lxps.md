---
id: lcl-lxps
status: open
deps: []
links: []
created: 2026-07-05T23:40:32Z
type: task
priority: 3
assignee: memgrafter
tags: [naming, refactor]
---
# Rename _evict_slot to reflect that it saves state, not evicts

## Problem

 doesn't actually evict anything. It saves the current KV cache state of a slot to the DB as a new node (label "eviction", boundary "auto-eviction"), updates ancestors' bin_file references, and returns. The actual eviction (clearing the slot from memory) is done by  which is called right after.

So  is really an emergency autosave / checkpoint — it captures whatever's in the slot before it's about to be overwritten.

## Fix

Rename  →  (or ) across all callers and tests. Update docstrings to clarify that this saves state before eviction, not performs the eviction itself.
