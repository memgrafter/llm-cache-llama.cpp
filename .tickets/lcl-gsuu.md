---
id: lcl-gsuu
status: open
deps: []
links: []
created: 2026-07-05T18:34:46Z
type: feature
priority: 2
assignee: memgrafter
tags: [prefix-cache, architecture, database]
---
# Unify prefix cache database across all model runs

Currently each model run gets its own cache directory under ~/.cache/llama.cpp-launch-scripts/<run-name>/trie/prefix-cache.sqlite. There are 27 separate SQLite databases — only 1 has data (103 nodes, ~197 GB), the rest are empty.

This causes several problems:
- When switching models/runs, the cache is lost — no shared prefix reuse
- Prune operates across all discovered caches but can't share nodes between them
- Empty databases waste file descriptors during discover() scans
- No way to leverage a large existing cache when starting a new run with similar data

## Proposed design

Single shared database at ~/.cache/llama.cpp-launch-scripts/prefix-cache.sqlite (or similar top-level location). Each node record includes a model_alias or cache_dir tag so:
- Lookups can be scoped to the current model/run
- Prune can operate globally across all models
- New runs immediately benefit from existing cached prefixes
- Empty per-run DB files can be cleaned up

## Implementation steps

1. Migrate schema: add model_id / cache_dir column to nodes table if not present
2. Change PrefixCache init to use shared DB path instead of per-run path
3. Update insert_node, lookup, prune_global to scope by model_id where needed
4. Migrate existing data from per-run DBs into unified DB
5. Clean up empty per-run trie directories
6. Add migration script for existing deployments

