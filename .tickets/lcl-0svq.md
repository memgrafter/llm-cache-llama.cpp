---
id: lcl-0svq
status: closed
deps: [lcl-49d7]
links: []
created: 2026-07-05T05:02:13Z
type: feature
priority: 1
assignee: memgrafter
tags: [proxy, slot-routing]
---
# Route incoming requests to best-matching slot by prompt hash

Implement get_best_slot() in the on-demand proxy: route each request to the slot whose loaded KV matches the prompt hash. Includes minimum match threshold (5000 tok or 80% of request context, whichever lower) to prevent short system-prompt requests from yoinking slots loaded with long conversations. Depends on slot state tracking being in place.
