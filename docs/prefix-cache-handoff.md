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
6. After the response completes, save slot `0` into a new trie node.
7. Prune to the configured size limit.

Important: the proxy forwards the original request unchanged. llama.cpp still owns prompt reuse semantics after restore.

## Defaults / knobs

`run-lmcache-proxy-stack.sh` now passes these proxy settings:

```bash
MIN_SAVE_TOKENS=256
PREFIX_CACHE_MAX_BYTES=2GiB
PREFIX_CACHE_MIN_FREE_BYTES=512MiB
NO_AUTO_SAVE=0
NO_PREFIX_CACHE=0
ALLOW_EXACT_PREFIX_RESTORE=0
```

Proxy flags:

```bash
--min-save-tokens N
--prefix-cache-max-bytes BYTES
--prefix-cache-min-free-bytes BYTES
--no-auto-save
--no-prefix-cache
--allow-exact-prefix-restore
```

## Exact-prefix safety

Exact-length restores are disabled by default:

```text
node.token_count < incoming.token_count
```

Reason: this llama.cpp/TurboQuant build can crash on exact-prefix restore. Once fixed upstream, remove this guard or start the proxy with:

```bash
ALLOW_EXACT_PREFIX_RESTORE=1 ./run-lmcache-proxy-stack.sh
```

## Streaming autosave detail

Streaming is supported and is the main autosave path.

For llama.cpp `/completion` streaming responses, the final saved slot usually contains:

```text
prompt tokens + generated tokens except the last generated token
```

That is expected: the last sampled token has been emitted, but its KV may not be present until it is evaluated as input for the next token. The proxy therefore saves to a temp filename first, reads `n_saved`, reconstructs the exact saved token prefix from streamed token IDs, then renames the bin to the final node filename.

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

## Verified live behavior

Live stack was restarted with:

```bash
MIN_SAVE_TOKENS=1 ./run-lmcache-proxy-stack.sh
```

A streaming `/completion` request autosaved a node:

```text
prefix-cache autosaved node 41-... (41 tokens, 63.1 MiB)
```

A later longer streaming request restored that node before forwarding:

```text
prefix-cache restored node 41-... (41 tokens)
```

With normal `cache_prompt` behavior, llama.cpp reported reuse on a subsequent request:

```text
cache_n = 53
prompt_n = 10
```

If the client sends `cache_prompt: false`, llama.cpp can still accept the restore but reports `cache_n = 0`; the proxy intentionally does not rewrite this client setting.

## Tests

Default suite:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Current default result:

```text
34 tests OK, 3 live tests skipped
```

Live prefix-cache contract:

```bash
RUN_LIVE_PREFIX_CACHE_TESTS=1 python3 -m unittest tests/test_prefix_cache_integration.py -v
```

Current live result:

```text
6 tests OK
```

## Remaining work

- Upstream/fix exact-prefix restore crash, then remove `strict_prefix_restore` guard.
- Add broader contract tests beyond the current 3 behavior groups.
- Add chat-specific live automatic-loop test using `/v1/chat/completions` and `/apply-template`.
- Consider a threaded HTTP server if concurrent streaming clients become necessary.
