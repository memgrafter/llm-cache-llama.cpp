---
id: lcl-vkx4
status: open
deps: []
links: []
created: 2026-07-05T09:50:09Z
type: bug
priority: 1
assignee: memgrafter
tags: [proxy, slot-routing, kv-cache, llama-cpp]
---
# llama.cpp 'failed to find space in KV cache' on restore into slot


llama.cpp returns HTTP 400 when trying to restore a KV bin file into a slot because there aren't enough available cells in the shared KV cache.

**Error observed** (from qwen36-backend-20260705-023814.log):


**Root cause**: llama.cpp's KV cache is shared across all slots. When slot 0 holds ~145k tokens and we try to restore ~144k tokens into slot 1, the total exceeds the available cell pool.

**Current mitigation**: The proxy's _try_make_room_for erases another slot before retrying. But this is reactive — we should be proactive about capacity planning.

**Need to investigate**:
- Can we query llama.cpp for available KV cells before attempting a restore?
- Should we track total token usage across slots and refuse to restore if capacity would be exceeded?
- Is there a way to pre-allocate per-slot capacity to avoid these failures?

