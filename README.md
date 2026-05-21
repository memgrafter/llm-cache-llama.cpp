# llama.cpp Launch Scripts

Run a local Qwen3.6 llama.cpp server behind a supervised KV-cache proxy so the public OpenAI endpoint stays stable, the backend is cleaned up with the proxy, hidden prompt-cache RAM stays off, and slot KV can be explicitly saved/restored on disk.

## Quickstart

From this repo:

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

## Interface guide

### `run-lmcache-proxy-stack.sh` — recommended entrypoint

This is the clean story for humans: run one script, get one public endpoint, stop one PID, and both proxy plus llama.cpp terminate together.

```bash
./run-lmcache-proxy-stack.sh
```

Useful environment overrides:

| Variable | Default | Meaning |
|---|---:|---|
| `PUBLIC_HOST` | `127.0.0.1` | Proxy bind host. |
| `PUBLIC_PORT` | `8081` | Public proxy port used by clients/pi. |
| `BACKEND_HOST` | `127.0.0.1` | llama.cpp backend host. |
| `BACKEND_PORT` | `8082` | Private llama.cpp backend port. |
| `ALIAS` | `qwen3.6-28b-reap-iq3xxs-turbo3-35k` | Model id exposed by `/v1/models`. |
| `CACHE_DIR` | `~/.cache/llama.cpp-launch-scripts/slot-kv` | Slot KV save/restore directory. |
| `CACHE_RAM` | `0` | Disables llama.cpp's separate multi-prompt RAM cache. |
| `RESTORE_SLOT_ON_START` | `slot_0_current.bin` | Static slot KV file to restore into llama.cpp before the proxy starts; set empty to skip. |
| `RESTORE_SLOT_ID` | `0` | Slot id restored at startup. |
| `TOP_K` | `3` | KV candidates the proxy may try per prompt. |
| `STOP_EXISTING` | `1` | Clear existing listeners on the public/backend ports before launch. |

Example with logs and custom ports:

```bash
PUBLIC_PORT=8090 BACKEND_PORT=8091 ./run-lmcache-proxy-stack.sh
```

Start without restoring a saved slot:

```bash
RESTORE_SLOT_ON_START= ./run-lmcache-proxy-stack.sh
```

PID files:

```text
/tmp/lmcache-proxy-stack.pid
/tmp/lmcache-proxy.pid
/tmp/qwen36-llamacpp-backend.pid
```

Logs are written under `logs/` by default:

```text
logs/qwen36-backend-*.log
logs/lmcache-proxy-*.log
```

### `lmcache-proxy-on-demand.py` — proxy only

Runs only the Python proxy. Use this directly only if you are managing llama.cpp yourself.

```bash
python3 lmcache-proxy-on-demand.py \
  --host 127.0.0.1 \
  --port 8081 \
  --server 127.0.0.1 \
  --llama-port 8082 \
  --cache-dir ~/.cache/llama.cpp-launch-scripts/slot-kv \
  --top-k 3
```

Flags:

| Flag | Meaning |
|---|---|
| `--host` | Proxy bind host. |
| `--port` | Proxy public port. |
| `--server` | llama.cpp backend host. |
| `--llama-port` | llama.cpp backend port. |
| `--cache-dir` | Disk KV cache directory. |
| `--top-k` | Maximum cached KV candidates to try per prompt. |

On each request, the proxy extracts prompt text, checks for matching KV files in `--cache-dir`, restores a compatible match into an idle slot, then forwards the request to llama.cpp.

### `run-qwen36-reap.sh` — backend only

Launches llama.cpp directly. In the supervised proxy stack, this is called by `run-lmcache-proxy-stack.sh` with `PORT=8082`.

Important defaults:

```text
ALIAS=qwen3.6-28b-reap-iq3xxs-turbo3-35k
SLOT_SAVE_PATH=~/.cache/llama.cpp-launch-scripts/slot-kv
CACHE_RAM=0
CACHE_REUSE=256
```

`CACHE_RAM=0` disables llama.cpp's separate multi-prompt RAM cache. Explicit slot save/restore still works.

## Slot KV operations

Save slot 0 through the public proxy to the static restore file:

```bash
./save-slot.sh
```

Restore slot 0 through the public proxy from the static restore file:

```bash
./restore-slot.sh
```

Both scripts default to:

```text
slot_0_current.bin
```

Verify a restore/export roundtrip is byte-identical:

```bash
./restore-slot.sh
HOST=127.0.0.1 PORT=8081 SLOT=0 ./save-slot.sh slot_0_cmp.bin

cmp ~/.cache/llama.cpp-launch-scripts/slot-kv/slot_0_current.bin \
    ~/.cache/llama.cpp-launch-scripts/slot-kv/slot_0_cmp.bin
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
