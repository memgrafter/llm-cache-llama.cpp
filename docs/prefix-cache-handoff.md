# Prefix Cache Handoff

## Current state

The repo now has three related paths:

1. **Static slot path**
   - File: `slot_0_current.bin`
   - Used by `run-lmcache-proxy-stack.sh`, `save-slot.sh`, and `restore-slot.sh`.
   - This is intentionally simple and separate from trie/cache pruning.

2. **Supervised proxy stack**
   - Entry point: `./run-lmcache-proxy-stack.sh`
   - Public endpoint: `127.0.0.1:8081`
   - Private llama.cpp backend: `127.0.0.1:8082`
   - If proxy stops, supervisor stops llama.cpp. If llama.cpp stops, supervisor stops proxy.
   - Startup restore defaults to `slot_0_current.bin`.

3. **Experimental prefix trie metadata tool**
   - File: `prefix_cache.py`
   - SQLite metadata DB:
     `~/.cache/llama.cpp-launch-scripts/slot-kv/trie/prefix-cache.sqlite`
   - KV bins remain as normal flat files in the llama.cpp slot-save directory:
     `~/.cache/llama.cpp-launch-scripts/slot-kv/prefix_node_<node-id>.bin`
   - This is intentionally flat because the llama.cpp slot save/restore API may reject filenames containing path separators.
   - Hashing: `blake2b-128-le-u32-v1`, streamed over little-endian uint32 token IDs with O(1) extra memory.

## Important verified behavior

### Save/restore works

Static slot roundtrip works:

```text
save slot -> restore slot -> save slot -> cmp
```

Validated with `slot_0_current.bin` and `cmp 0`.

### llama.cpp does prefix reuse after restore

Verified using a 3601-token prefix bin:

```text
restore prefix bin
send prefix + 23-token suffix
```

llama.cpp reported:

```text
cache_n = 3601
prompt_n = 23
```

So the proxy does not need to slice prompts. It only needs to choose and restore the best prefix bin; llama.cpp handles suffix evaluation.

### Exact-prefix edge case

A request whose prompt exactly matches a restored 3601-token slot crashed this TurboQuant llama.cpp build:

```text
need to evaluate at least 1 token
n_past was set to 3600
failed to remove sequence 0 with p0=3600, p1=-1
```

Tracked in `todo.txt`. Treat as a llama.cpp/test nuance, not a blocker for suffix-prefix reuse.

## Tests

Unit + integration tests exist.

Default run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Current default result:

```text
31 tests OK, 3 live integration tests skipped
```

Live integration tests are gated:

```bash
RUN_LIVE_PREFIX_CACHE_TESTS=1 python3 -m unittest tests/test_prefix_cache_integration.py -v
```

They intentionally touch the real service and should be run deliberately.

## `prefix_cache.py` commands

```bash
./prefix_cache.py init
./prefix_cache.py add --label NAME --prompt-file prompt.txt
./prefix_cache.py lookup --prompt-file prompt.txt
./prefix_cache.py list
./prefix_cache.py prune --max-bytes N
```

The tool currently expects already-rendered prompt text for `add` and `lookup`.

## Next step

Yes: the next implementation step is to integrate `prefix_cache.py` into the **on-demand proxy path**.

The target file should be:

```text
lmcache-proxy-on-demand.py
```

Not the older background-thread proxy unless you intentionally revive that design.

## Minimal proxy integration plan

At request time in `LMCacheHandler._handle_request()`:

1. Parse body as it does today.
2. Build the exact prompt text for lookup:
   - For `/completion`, use `body["prompt"]`.
   - For `/v1/chat/completions`, call backend `/apply-template` with `messages` to get the same rendered prompt llama.cpp will tokenize.
3. Tokenize rendered prompt through backend `/tokenize`.
4. Use `PrefixCache.lookup(tokens, touch=True)`.
5. If a node is found:
   - Restore `node["bin_file"]` into slot 0.
   - Forward the original request unchanged.
6. If no node is found:
   - Forward unchanged.

Important: do not modify the original request. llama.cpp should see the full prompt and use its own LCP/prefix reuse after the restore.

## What not to do yet

- Do not remove the static `slot_0_current.bin` path.
- Do not put KV blobs inside SQLite.
- Do not implement automatic cache creation after every request yet.
- Do not implement aggressive pruning beyond leaf-only pruning.
- Do not rely on raw OpenAI JSON for hashes; use rendered prompt text or token IDs.

## Open design questions

1. **Rendered prompt source**
   - Preferred: backend `/apply-template` then `/tokenize`.
   - For `/completion`, direct prompt string is already rendered/plain.

2. **Automatic node creation**
   - Later: after a long request finishes, save useful prefix boundaries into trie.
   - For now: manual `prefix_cache.py add` is safer.

3. **Boundary selection**
   - Semantic boundaries are better than fixed intervals: system prompt, developer prompt, tool schema, AGENTS/project context, etc.
   - Fixed ladder nodes may still be useful for long conversations.

4. **Pruning**
   - Keep trunk/shared nodes.
   - Prune leaves first by old `last_used`, low `hits`, and high `size_bytes`.

## Suggested first proxy integration test

Mock-backed test:

1. Create trie node for `prefix`.
2. Send request with `prefix + suffix` through proxy.
3. Assert proxy calls backend restore for the trie node before forwarding.
4. Assert original request body is forwarded unchanged.

Live-backed test can come after that and should remain opt-in.
