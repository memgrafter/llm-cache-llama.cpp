# llama.cpp Prefix Slot Cache Feature — 1pager

Prepared: 2026-05-23T12:47:28

Note: This approach is not valid, llm probably hallucinated a llama.cpp extension system. Check later.

## Goal

Move the current proxy's core value into `llama-server`: orchestrate saved KV slot `.bin` files by matching rendered prompt-token prefixes, restoring the best matching file before prefill, saving the completed slot after response, and pruning low-value files. This is not a general plugin/API project; it is a guarded server feature that directly calls the same internal functions currently exposed through `/slots/{id}?action=save|restore|erase`.

## Existing Python behavior to port

Current live path: `lmcache-proxy-on-demand.py` + `prefix_cache.py`.

Per request today:

1. Render cache prompt.
   - `/completion`: use `body["prompt"]`.
   - `/v1/chat/completions`: call `/apply-template` with the full request body so tools/template-affecting fields are included.
2. Tokenize rendered prompt with `/tokenize`.
3. Lookup longest cached token prefix in SQLite metadata.
4. Restore matched bin into slot `0` through `/slots/0?action=restore`.
5. Forward the original request unchanged, except the current exact-prefix newline workaround when enabled.
6. After successful response, save slot `0` through `/slots/0?action=save` to a temp bin.
7. Read the slot bin token table, verify saved tokens start with rendered prompt tokens, insert metadata nodes, optionally insert a generated-response node sharing the same physical bin, then prune.

Legacy `lmcache-proxy.py` is not the target; it is whole-prompt hash/polling fallback behavior.

## Direct llama.cpp mapping

The REST API already calls the internals we need in `tools/server/server-context.cpp`:

```cpp
// save
llama_state_seq_save_file(ctx_tgt, filepath.c_str(), slot->id, tokens.data(), token_count);

// restore
llama_state_seq_load_file(ctx_tgt, filepath.c_str(), slot->id, tokens.data(), tokens.size(), &token_count);
slot->prompt.tokens.clear();
slot->prompt.tokens.insert(tokens);
```

The new feature should reuse/refactor these paths instead of making HTTP calls back into the server.

Add flags near existing slot flags:

```text
--slot-prefix-cache PATH
--slot-prefix-cache-max-bytes BYTES
--slot-prefix-cache-min-free-bytes BYTES
--slot-prefix-cache-min-save-tokens N
--slot-prefix-cache-allow-exact-prefix-restore
--slot-prefix-cache-no-generated
```

No behavior changes when `--slot-prefix-cache` is omitted.

## Cache model

Each logical cache node represents a token prefix:

```text
node_id   = "<token_count>-<blake2b128-le-u32-prefix-hash>"
bin_file  = prefix_node_<node_id>.bin
parent_id = longest existing shorter matching prefix
boundary  = auto-response | auto-generated-response | anchor | manual
stats     = hits, created_at, last_used, pinned, size_bytes, n_saved
```

Matching is deterministic/token-based:

1. collect cached lengths `<= incoming_token_count` (`<` while exact-prefix restore remains unsafe),
2. stream BLAKE2b-128 over little-endian `uint32` token IDs,
3. choose the longest `(token_count, prefix_hash)` hit,
4. update `hits` / `last_used`,
5. restore that bin into the selected slot before normal prompt processing.

## Server hook shape

Best integration point is inside `server_context` after a slot is selected and before prompt prefill, because the proxy currently assumes slot `0` but llama-server chooses slots internally. MVP may require `--parallel 1`; multi-slot support should restore/save against the actual assigned slot.

Lifecycle in-server:

```text
request parsed/rendered/tokenized
  -> slot selected
  -> prefix-cache lookup
  -> internal restore into selected slot
  -> normal llama.cpp request execution
  -> final response completed
  -> internal save selected slot to temp bin
  -> parse saved token table / verify prompt prefix
  -> atomic rename + metadata insert/update
  -> prune
```

## Pruning and safety

Pruning removes only unpinned leaf nodes so parent prefixes remain valid. Priority is plain LRU: oldest `COALESCE(last_used, created_at)` first. Shared physical bins are unlinked only after the last logical node reference is removed.

Failures must degrade to normal inference: lookup/restore/save/prune errors log and continue unless the user explicitly calls management endpoints.

## MVP boundary

Single process, single model, existing slot-save binary format, no general plugin ABI, no distributed cache, no semantic matching. Port prefix matching, direct restore/save, autosave, verification, generated-response shared-bin nodes, and pruning. Anchors can follow once the base lifecycle is correct.
