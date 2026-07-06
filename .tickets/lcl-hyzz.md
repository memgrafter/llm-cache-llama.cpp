---
id: lcl-hyzz
status: closed
deps: []
links: []
created: 2026-07-06T04:11:45Z
type: bug
priority: 1
assignee: memgrafter
tags: [proxy, caching]
---
# Update slot_state after ancestor-match routing

When _pick_slot_for_restore returns an ancestor match (no restore needed), it only calls touch() on slot_state — the node_id and token_count are never updated. If the backend grows beyond the tracked node via delta processing, slot_state holds stale data. Subsequent ancestor-match checks use wrong baselines, causing incorrect routing decisions and checkpoint invalidation from MTP truncation.
