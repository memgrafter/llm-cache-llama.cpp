---
id: lcl-c3ik
status: closed
deps: [lcl-0svq]
links: []
created: 2026-07-05T05:02:22Z
type: feature
priority: 1
assignee: memgrafter
tags: [proxy, slot-routing]
---
# Inject id_slot into request body when routing to a specific slot

When get_best_slot returns a target slot, inject id_slot into the request body before forwarding to llama.cpp. This maps from lmcache-proxy.py's handler logic that sets body['id_slot'] = target. Depends on routing being implemented.
