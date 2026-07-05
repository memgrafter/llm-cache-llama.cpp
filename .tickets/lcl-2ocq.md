---
id: lcl-2ocq
status: open
deps: []
links: []
created: 2026-07-05T09:49:43Z
type: feature
priority: 2
assignee: memgrafter
tags: [proxy, slot-routing, prefix-cache, eviction]
---
# Cache constantly reloads instead of reusing matched KV cache in VRAM slot


The proxy restores a KV bin file into a slot on every request, even when the slot already has a matching prefix loaded in VRAM. This wastes time reloading data that's already in memory.

**Current behavior**: Every request triggers a restore (POST /slots/N?action=restore) even when the prefix match is already in the slot.

**Proposed fix**: Consider unloading slots after response completion or after a short expiry (~1 minute). This would:
- Free VRAM for other conversations
- Avoid redundant restores of already-loaded prefixes
- Reduce slot count pressure on the shared KV cache

Alternatively: when a prefix match is found and the slot already holds that prefix (ancestor walk matches), skip the restore entirely and just route to the slot. llama.cpp can compute the delta from the existing prefix.

**Related**: The shared KV cache means more slots = less room per slot. Unloading unused slots would help with the 'failed to find space' issue too.

