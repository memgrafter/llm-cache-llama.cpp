---
id: lcl-2ww5
status: closed
deps: []
links: []
created: 2026-07-05T19:52:54Z
type: bug
priority: 1
assignee: memgrafter
tags: [prefix-cache, prune, optimization]
---
# Move ancestor bin_file update from autosave to prune path

The lcl-9opm optimization updates ancestor nodes to point to descendant bin files during autosave. This creates a problem: pruning a leaf never frees disk space because ancestors still reference the same file.

## Current broken flow

After autosave with ancestor update:


Prune deletes L from DB. Checks refs for file_L.bin → G and P still reference it → file NOT unlinked. 0 bytes freed. P becomes the new leaf, same story repeats until the whole chain is gone — but each step only deletes a DB row, never the actual ~5GB file.

## Fix: move ancestor update to prune time

Instead of eagerly updating ancestors during autosave, update them only when their file is about to be unlinked:

1. Prune selects leaf L, deletes from DB
2. Check refs for L's bin_file
3. If refs == 0 (would unlink):
   a. Find all nodes pointing to this file
   b. Update them to point to the next available descendant's file
   c. Now unlink — space actually freed
4. If refs > 0, don't unlink (current behavior)

This way each node keeps its own file until prune time, and pruning a leaf frees space immediately. The ancestor optimization still works — ancestors get redirected at the last moment instead of eagerly during autosave.

## Implementation

1. Revert the autosave-time ancestor update in lmcache-proxy-on-demand.py
2. Add ancestor redirect logic inside prune_global, triggered when remaining_refs == 0
3. Need to find the next available descendant's file — walk children of the nodes being updated
4. Update tests accordingly

