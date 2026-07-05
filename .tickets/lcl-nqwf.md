---
id: lcl-nqwf
status: open
deps: []
links: []
created: 2026-07-05T04:25:15Z
type: feature
priority: 3
assignee: memgrafter
tags: [proxy, slot-routing]
---
# Update _slot_tokens from response usage data after request completes

_slot_tokens is only set at restore time (estimated from prompt char count). After a request completes, the response includes usage.prompt_tokens_details.cached_tokens and prompt_tokens — use these to update _slot_tokens for the routed slot. Non-blocking: update in background after response is read. Proxy already knows the slot_id since it injected id_slot.
