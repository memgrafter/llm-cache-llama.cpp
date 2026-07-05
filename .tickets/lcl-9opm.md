---
id: lcl-9opm
status: closed
deps: []
links: []
created: 2026-07-05T18:41:33Z
type: feature
priority: 2
assignee: memgrafter
tags: [prefix-cache, prune, optimization]
---
# Allow ancestor nodes to reference descendant bin files for aggressive pruning

## Problem

The cache is a deep chain of parent→child nodes where each node holds its own ~5GB .bin file. Only leaf nodes are prunable — internal nodes can't be pruned because they have children. This means ~190 GB of cache is locked in unprunable internal nodes, with only ~8 GB across 4 leaf candidates.

## Insight

llama.cpp loads KV cache by prefix — if you restore a 100k-token .bin file into a slot that only needs the first 50k tokens, it works perfectly. The extra tokens sit idle in VRAM but don't cause problems.

This means ancestor nodes can safely reference their descendant's bin file, because the descendant's file contains all the ancestor's data as a prefix.

## Proposal

When creating a new descendant node C (child of B, grandchild of A), update A's bin_file to point to C's file instead of A's own file. Then:

1. Prune B — delete B's .bin file. A still works because it now points to C's file.
2. More intermediate nodes become prunable since their files aren't needed.
3. Dramatically increases prunable surface area — no longer limited to just leaves.

### Multiple descendants from same ancestor

If ancestor A has two children B and C, both point to whichever is the latest created (C). Both share the same prefix data up to A's token count, so this is correct. When B is pruned, A still works via C's file.

### Implementation

1. In insert_node / autosave: after creating descendant node, walk ancestor chain and update each ancestor's bin_file to point to the new descendant's file
2. In prune_global: when pruning a leaf, check if any non-child nodes reference the same bin_file — if not, safe to unlink
3. Add migration: for existing cache, ancestors already share files with descendants via the bin_refs mechanism, but make it explicit

### RAM cache bonus

Since ancestors always point to the latest descendant's file, a hot RAM layer around the currently-loaded file serves all ancestor hits without additional disk I/O. When we continuously rewrite ancestor references on each new descendant creation, the file in use is always the most recent one.

### Trade-offs

- **VRAM**: loading a 100k-token file for a 50k-token node wastes ~2GB VRAM. Acceptable in single-slot mode. Minor concern in multi-slot — but only when restoring from an ancestor, which is rare after lcl-2ocq fix.
- **Complexity**: need to track that ancestors reference descendant files, not their own
- **Safety**: if the latest descendant is pruned before its ancestor, the ancestor's file reference becomes stale. Mitigation: ancestors should always have a fallback — either keep their own file OR ensure prune order guarantees descendants are pruned before ancestors (which is already the case since only leaves are prunable)

