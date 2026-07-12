---
id: lcl-ftl2
status: open
deps: []
links: []
created: 2026-07-12T17:16:43Z
type: feature
priority: 2
assignee: memgrafter
---
# Log diverged chars on cache miss over X%

When a prefix cache lookup misses by more than a threshold (TBD, maybe 10%), log the characters that diverged — up to ~50 chars of context showing where the match broke. This helps diagnose why caches aren't hitting and what prompts are causing divergence.

## Blocker: Data storage

We **don't want to store this data** (persistently or in any durable form). Logging diverged characters could capture prompt content, which is sensitive. Any implementation must ensure zero persistence — the data can only be used for immediate diagnostic output and must not be written to disk, logs, or any storage medium.
