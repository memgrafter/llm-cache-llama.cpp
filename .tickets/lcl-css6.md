---
id: lcl-css6
status: open
deps: []
links: []
created: 2026-07-05T09:45:02Z
type: bug
priority: 2
assignee: memgrafter
tags: [proxy, slot-routing, prefix-cache, autosave]
---
# Proxy consistently matches ~90% context instead of full cache hit


When a conversation grows over multiple turns, the proxy's prefix cache lookup finds a match that covers only ~90% of the loaded context rather than the full amount. This means llama.cpp has to recompute ~10% of tokens that were already cached.

**Observed**: In the latest 240k ctx session, slot 0 reached 143003 tokens but each request's lookup matched a node ~90% of that size, forcing recomputation of the gap.

**Theories**:
1. Autosave doesn't save a new KV bin file every round — it may be reusing an older bin, so the trie's newest node points to stale data
2. The lookup finds a node whose prefix hash matches but whose bin file is from an earlier autosave cycle, missing the most recent tokens
3. The generated-prefix node (larger) isn't being saved/restored correctly — maybe only the prompt node is persisted
4. Token boundary mismatch: the BLAKE2b hash at the exact request length doesn't match any node in the trie, so lookup falls back to a shorter ancestor

**Need to investigate**:
- Does autosave create a new bin file each round? Check if bin files accumulate or get reused.
- Does cache.lookup() return the largest matching node or just any matching node?
- Is the generated-prefix node (with response tokens included) being saved and matched correctly?

