# llama.cpp Launch Scripts

Run a local Qwen3.6 llama.cpp server behind a supervised KV-cache proxy so the public OpenAI endpoint stays stable, the backend is cleaned up with the proxy, hidden prompt-cache RAM stays off, and slot KV can be explicitly saved/restored on disk.

## Quickstart

```bash
cd ~/code/llama.cpp-launch-scripts
```

Raise the Apple Metal wired-memory limit after reboot:

```bash
sudo sysctl iogpu.wired_limit_mb=14336
```

Start the supervised proxy stack:

```bash
./run-lmcache-proxy-stack.sh
```

That starts:

```text
127.0.0.1:8081  LMCache proxy       public client/pi endpoint
127.0.0.1:8082  llama.cpp backend   private model server
```

Verify the public endpoint:

```bash
curl -sS http://127.0.0.1:8081/health
curl -sS http://127.0.0.1:8081/v1/models
```

Clients, including pi, should use:

```text
baseUrl: http://127.0.0.1:8081/v1
model:   qwen3.6-28b-reap-iq3xxs-turbo3-35k
```

Stop the full stack cleanly:

```bash
kill "$(cat /tmp/lmcache-proxy-stack.pid)"
```

When the supervisor/proxy stops, it also stops the llama.cpp backend.

## Environment variables

| Variable | Default | Meaning |
|---|---:|---|
| `PUBLIC_HOST` | `127.0.0.1` | Proxy bind host. |
| `PUBLIC_PORT` | `8081` | Public proxy port used by clients/pi. |
| `BACKEND_HOST` | `127.0.0.1` | llama.cpp backend host. |
| `BACKEND_PORT` | `8082` | Private llama.cpp backend port. |
| `ALIAS` | `qwen3.6-28b-reap-iq3xxs-turbo3-35k` | Model id exposed by `/v1/models`. |
| `MODELS_DIR` | `<repo>/models` | Directory used by model wrapper scripts for GGUF defaults. |
| `MODEL` | wrapper-specific GGUF in `MODELS_DIR` | Exact GGUF path passed to llama.cpp. |
| `CACHE_DIR` | `~/.cache/llama.cpp-launch-scripts/slot-kv` | Slot KV save/restore directory. |
| `CACHE_RAM` | `0` | Disables llama.cpp's separate multi-prompt RAM cache. |
| `SPEC_TYPE` | `ngram-mod` | Enables draft-model-free speculative decoding; set `none` to disable. |
| `SPEC_NGRAM_MOD_N_MATCH` | `24` | n-gram lookup length for `ngram-mod`. |
| `SPEC_NGRAM_MOD_N_MIN` | `48` | Minimum n-gram draft length for `ngram-mod`. |
| `SPEC_NGRAM_MOD_N_MAX` | `63` | Maximum n-gram draft length for `ngram-mod`; clamped to `BATCH - 1` because llama.cpp verifies one sampled token plus draft tokens in one logical batch. |
| `TOP_K` | `3` | Legacy KV candidates the proxy may try per prompt. |
| `MIN_SAVE_TOKENS` | `256` | Minimum rendered-prompt token count before automatic prefix-cache autosave. |
| `PREFIX_CACHE_MAX_BYTES` | `8GiB` | Global trie-backed prefix-cache size limit across cache subdirectories; unpinned leaf nodes are pruned by LRU to stay under it. |
| `PREFIX_CACHE_MIN_FREE_BYTES` | `512MiB` | Minimum filesystem free space required before autosave; the proxy prunes or skips gracefully below it. |
| `NO_AUTO_SAVE` | `0` | Set `1` to restore prefixes but skip automatic saves. |
| `NO_PREFIX_CACHE` | `0` | Set `1` to disable trie-backed prefix cache. |
| `NO_GENERATED_PREFIX_CACHE` | `0` | Set `1` to skip optimistic generated-response prefix nodes after stream completion. |
| `ALLOW_EXACT_PREFIX_RESTORE` | `0` | Set `1` only after the llama.cpp exact-prefix restore crash is fixed. |
| `STOP_EXISTING` | `1` | Clear existing listeners on the public/backend ports before launch. |

Speculative decoding defaults to `ngram-mod` because it does not require a draft model and uses only a small shared n-gram hash pool; llama.cpp still verifies all drafted tokens with the main model.

Example with logs and custom ports:

```bash
PUBLIC_PORT=8090 BACKEND_PORT=8091 ./run-lmcache-proxy-stack.sh
```

The backend starts with an empty slot; the proxy restores the best disk-cache prefix on the first request.

## PID files and logs

PID files:

```text
/tmp/lmcache-proxy-stack.pid
/tmp/lmcache-proxy.pid
/tmp/qwen36-llamacpp-backend.pid
```

Logs are written under `logs/` by default:

```text
logs/stack-*.log                # when started with --background, or if redirected manually
logs/qwen36-backend-*.log
logs/lmcache-proxy-*.log
```

## Manual Slot KV operations

The normal service path is the trie-backed disk cache. The backend starts with an empty slot and restores the best matching cache node per request.

For debugging llama.cpp's built-in slot API, save or restore an explicitly named slot file through the public proxy:

```bash
./save-slot.sh manual-debug.bin
./restore-slot.sh manual-debug.bin
```

Filenames are relative to the backend `--slot-save-path` / `SLOT_SAVE_PATH` directory.

Verify a manual restore/export roundtrip is byte-identical:

```bash
./restore-slot.sh manual-debug.bin
HOST=127.0.0.1 PORT=8081 SLOT=0 ./save-slot.sh manual-debug-cmp.bin

cmp ~/.cache/llama.cpp-launch-scripts/slot-kv/manual-debug.bin \
    ~/.cache/llama.cpp-launch-scripts/slot-kv/manual-debug-cmp.bin
```

`cmp` exits `0` when the files are bytewise identical.

## What it launches and what it stops

`run-lmcache-proxy-stack.sh` launches both processes:

```text
LMCache proxy       public endpoint on PUBLIC_PORT, default 8081
llama.cpp backend   private endpoint on BACKEND_PORT, default 8082
```

The supervisor treats the proxy as the public service. If the proxy exits, the supervisor stops llama.cpp. If llama.cpp exits, the supervisor stops the proxy. If you `kill $(cat /tmp/lmcache-proxy-stack.pid)`, both processes are stopped.

Manual cleanup if PID files are stale:

```bash
for port in 8081 8082; do
  pids="$(lsof -tiTCP:$port -sTCP:LISTEN || true)"
  [ -n "$pids" ] && kill $pids
done
```
