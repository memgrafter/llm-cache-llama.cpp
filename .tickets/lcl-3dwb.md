---
id: lcl-3dwb
status: open
deps: []
links: []
created: 2026-07-05T22:53:11Z
type: task
priority: 2
assignee: memgrafter
tags: [prefix-cache, anchor, prune]
---
# Investigate anchor tree growth and pruning strategy

The anchor tree (rooted at anchor node for end-of-system-message) has grown to 117 nodes / 59 unique bin files. It behaves differently from the main tree:

- Each conversation restoring from the anchor adds one pair (auto-response + auto-generated), creating a straight chain rather than branching
- No lcl-9opm ancestor sharing — each pair has its own file
- Can't be pruned: deleting any pair orphans everything below it
- The chain grows indefinitely with each new conversation that matches deeper into it

## Questions to answer

1. Is the anchor tree actually useful? We moved all 59 files to .bak and the system ran fine without them
2. Should the anchor tree use lcl-9opm ancestor sharing like the main tree?
3. Should the cascade prune apply to the anchor tree too? If so, how to avoid orphaning valid nodes
4. Is the anchor config (end-of-system-message) the right boundary, or should anchors be prunable independently
5. Can we limit anchor tree depth or prune stale branches that haven't been restored from recently

