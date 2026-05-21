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

Automatic nodes are keyed by the exact incoming rendered prompt tokens, not by trying to reconstruct generated output. The saved slot file may contain additional generated tokens. That is safe: on a later longer chat request, llama.cpp computes the true LCP against the restored slot and reuses any matching generated tokens too. This avoids corrupt metadata when `/v1/chat/completions` streams omit or transform reasoning/content text.

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

The first automatic implementation validated `/completion`, but real usage is `/v1/chat/completions`. Chat autosave was adjusted to key nodes by incoming rendered prompt tokens because reconstructing saved slot tokens from OpenAI chat stream deltas is not reliable.

A live `/v1/chat/completions` streaming autosave was verified with `MIN_SAVE_TOKENS=1`: the proxy created a DB node keyed to the rendered prompt and saved a slot bin where `n_saved >= token_count`. The temporary live-test nodes were deleted afterward. A gated live regression test exists in `tests/test_lmcache_proxy_on_demand.py` behind `RUN_LIVE_PROXY_CHAT_CACHE_TESTS=1`.

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
