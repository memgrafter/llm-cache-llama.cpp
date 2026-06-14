---
id: lcl-6rg9
status: in_progress
deps: []
links: []
created: 2026-06-14T07:37:04Z
type: chore
priority: 2
assignee: memgrafter
---
# Normalize sh wrappers — dry shared parameters into shared script

The model-specific wrapper scripts (run-gemma4-12b.sh, run-reap.sh, run-gemma4-e4b.sh, run-qwen36-35b-a3b-ud-iq2m.sh) all repeat the same boilerplate: CTX=60000, CACHE_K=turbo3, CACHE_V=turbo3, SPEC_TYPE=ngram-mod, MTP=0, EXTRA_FLAGS=--no-mmproj, ALIAS=local-model. Extract these into a shared script (e.g. _shared.sh or shared-env.sh) that each wrapper sources. Each wrapper should only declare model-specific values (MODEL, CACHE_DIR). Also: ngram-mod should always be on — make SPEC_TYPE=ngram-mod the default in the shared script, not something each wrapper sets individually.

## Notes

**2026-06-14T07:37:08Z**

ngram-mod must always be on — SPEC_TYPE=ngram-mod is the default in the shared script, not an optional override. This is a hard requirement, not a preference.
