# Prefix Cache Handoff

## Current state

The prefix cache is now integrated into the supervised llama.cpp proxy stack.

There are two separate slot-KV paths:

1. **Static/manual slot**
   - File: `slot_0_current.bin`
   - Used by `run-lmcache-proxy-stack.sh`, `save-slot.sh`, and `restore-slot.sh`.
   - This remains separate from trie pruning and automatic prefix-cache nodes.

2. **Automatic trie-backed prefix cache**
   - Metadata: `~/.cache/llama.cpp-launch-scripts/slot-kv/trie/prefix-cache.sqlite`
   - KV bins: `~/.cache/llama.cpp-launch-scripts/slot-kv/prefix_node_<node-id>.bin`
   - Temp save files: `prefix_tmp_<time-ns>.bin`, renamed after the saved token count is known.
   - Hashing: `blake2b-128-le-u32-v1` over little-endian uint32 token IDs.

## Implemented proxy behavior

`lmcache-proxy-on-demand.py` now does the full automatic loop:

1. Render/tokenize incoming prompt.
   - `/completion`: uses `body["prompt"]`.
   - `/v1/chat/completions`: calls backend `/apply-template`, then `/tokenize`.
2. Lookup the best trie node in `PrefixCache`.
3. Restore the matching node into slot `0`.
4. Forward the original request unchanged.
5. Stream the backend response through to the client.
6. After the response completes, save slot `0` and create a trie node keyed by the incoming rendered prompt tokens.
7. If enabled and verified, create a second generated-response trie node sharing the same bin.
8. Prune to the configured size limit.

Important: the proxy forwards the original request unchanged. llama.cpp still owns prompt reuse semantics after restore.

## Defaults / knobs

`run-lmcache-proxy-stack.sh` now passes these proxy settings:

```bash
MIN_SAVE_TOKENS=256
PREFIX_CACHE_MAX_BYTES=2GiB
PREFIX_CACHE_MIN_FREE_BYTES=512MiB
NO_AUTO_SAVE=0
NO_PREFIX_CACHE=0
NO_GENERATED_PREFIX_CACHE=0
ALLOW_EXACT_PREFIX_RESTORE=0
```

Proxy flags:

```bash
--min-save-tokens N
--prefix-cache-max-bytes BYTES
--prefix-cache-min-free-bytes BYTES
--no-auto-save
--no-prefix-cache
--no-generated-prefix-cache
--allow-exact-prefix-restore
```

## Exact-prefix safety

Exact-length restores are still not sent to llama.cpp by default:

```text
node.token_count < incoming.token_count
```

If the proxy finds an exact node and can append a newline without changing the existing token prefix, it forwards the newline-extended request and restores the exact node as a strict prefix. This is annotated in node metadata as `exact_prefix_newline_workaround` and exists only as a workaround for the current llama.cpp/TurboQuant exact-prefix restore crash. If the newline would not preserve the token prefix, the proxy falls back to ordinary strict-prefix lookup.

Once fixed upstream, remove this guard/workaround or start the proxy with:

```bash
ALLOW_EXACT_PREFIX_RESTORE=1 ./run-lmcache-proxy-stack.sh
```

## Streaming autosave detail

Streaming is supported and is the main autosave path.

Automatic prompt nodes are keyed by the exact incoming rendered prompt tokens. After stream completion, the proxy also attempts an optimistic generated-response node: it parses the saved slot bin token table, verifies the saved slot starts with the rendered prompt tokens, reconstructs text from streamed `reasoning_content`/`content`/text deltas, tokenizes `prompt + captured_response`, and inserts the verified LCP beyond the prompt as an `auto-generated-response` node. The prompt node and generated node can point at the same physical bin; pruning is shared-bin safe and unlinks a bin only after the last referencing node is removed. If verification fails, the proxy keeps the prompt-only node.

## Storage behavior

Before autosave, the proxy:

1. prunes trie cache to `PREFIX_CACHE_MAX_BYTES`, default `2GiB`,
2. checks filesystem free space,
3. if free space is below `PREFIX_CACHE_MIN_FREE_BYTES`, default `512MiB`, prunes additional leaf nodes,
4. if nothing is prunable and storage is still low, skips autosave gracefully.

Static `slot_0_current.bin` is not pruned by this logic.

## Management bypass

`prefix_cache.py` sends:

```text
X-LMCache-Bypass: 1
```

This prevents management operations like `prefix_cache.py add` from recursively triggering proxy autosave when pointed at the public proxy URL.

## Current live note

The first automatic implementation validated `/completion`, but real usage is `/v1/chat/completions`. Chat autosave keys a safe prompt node by incoming rendered prompt tokens and, when stream reconstruction verifies against the slot bin token table, adds an optimistic generated-response node for rewind/fork/tree-split clients.

A live `/v1/chat/completions` streaming autosave was verified with `MIN_SAVE_TOKENS=1`: the proxy created a DB node keyed to the rendered prompt and saved a slot bin where `n_saved >= token_count`. The temporary live-test nodes were deleted afterward. A gated live regression test exists in `tests/test_lmcache_proxy_on_demand.py` behind `RUN_LIVE_PROXY_CHAT_CACHE_TESTS=1`.

## Tests

Default suite:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Current default result:

```text
43 tests OK, 5 live tests skipped
```

Live prefix-cache contract:

```bash
RUN_LIVE_PREFIX_CACHE_TESTS=1 python3 -m unittest tests/test_prefix_cache_integration.py -v
```

Current live result:

```text
6 tests OK
```

Generated-response cache coverage now includes unit tests for full verified generated nodes, partial verified LCP when stream text diverges from slot tokens, tool-call-only streams not producing bogus generated nodes, shared-bin pruning/accounting, generated-node restore as longest prefix, and the exact-prefix newline workaround. A gated live `/completion` test asserts creation of an `auto-generated-response` node sharing the prompt node's bin.

## Remaining work

- Upstream/fix exact-prefix restore crash, then remove `strict_prefix_restore` guard/newline workaround.
- Add richer tool-call serialization support if we decide to cache assistant tool-call JSON before the client sends tool results.
- Consider a threaded HTTP server if concurrent streaming clients become necessary.
