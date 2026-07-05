---
id: lcl-15mz
status: open
deps: []
links: []
created: 2026-07-05T04:29:49Z
type: task
priority: 3
assignee: memgrafter
tags: [proxy, slot-routing]
---
# Investigate min_match_tokens threshold tuning for early short tool-call rounds

Early agent rounds (tool calls before reading files) may be under 5000 tok, causing the threshold check to reject matches unnecessarily. 5000 tok prefill is ~2.5s when dedicated and then cached, so it may never matter in practice. But early short rounds could be painful. Burn-in coding first, then investigate if tuning min_match_tokens or adding a dynamic threshold helps.
