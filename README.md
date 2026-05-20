# llama.cpp Launch Scripts

Scripts and notes for running Qwen3.6 28B REAP GGUF models with llama.cpp on Apple Silicon / Metal under tight unified-memory constraints.

## Quickstart: Qwen XXS TurboQuant server

From a fresh shell after reboot, raise the Metal wired-memory limit first:

```bash
sudo sysctl iogpu.wired_limit_mb=14336
```

Stop any existing server on the default port:

```bash
pid="$(lsof -tiTCP:8081 -sTCP:LISTEN || true)"
[ -n "$pid" ] && kill $pid
```

Start the bundled TurboQuant Metal build with the typical 32k settings:

```bash
cd ~/code/llama.cpp-launch-scripts

GGML_METAL_NO_RESIDENCY=1 \
CTX=32768 NGL=999 BATCH=64 UBATCH=16 \
CACHE_K=turbo3 CACHE_V=turbo3 MTP=0 \
ALIAS=qwen3.6-28b-reap-iq3xxs-turbo3-32k \
./run-qwen36-reap.sh --serve
```

OpenAI-compatible endpoint:

```text
http://127.0.0.1:8081/v1
```

Model id / alias:

```text
qwen3.6-28b-reap-iq3xxs-turbo3-32k
```

Check readiness:

```bash
curl -sS http://127.0.0.1:8081/health
curl -sS http://127.0.0.1:8081/v1/models
```

During startup, `/health` can briefly return `503 Loading model`; wait until it returns:

```json
{"status":"ok"}
```

Stop the server:

```bash
kill "$(cat /tmp/qwen36-llamacpp-server.pid)"
```

## Files

- `run-qwen36-reap.sh` — launcher for `llama-cli` or `llama-server`.

Current defaults:

- Model: `~/Downloads/Qwen3.6-28B-REAP.i1-IQ3_XXS.gguf`
- Server binary: `~/clones/llama-cpp-turboquant/build-metal/bin/llama-server`
- Host/port: `127.0.0.1:8081`
- Alias: `qwen3.6-28b-reap-iq3xxs-llamacpp`
- Context: `4096` unless overridden with `CTX=...`
- Output budget: `8192` unless overridden with `NPRED=...`
- KV cache: `q4_0/q4_0` unless overridden with `CACHE_K=... CACHE_V=...`
- MTP: disabled by default with `MTP=0`
- Metrics endpoint: enabled by default with `--metrics`
- Slot KV save/restore: enabled by default at `~/.cache/llama.cpp-launch-scripts/slot-kv`
- Prompt cache reuse: enabled by default with `--cache-reuse 256`

## Critical macOS Metal memory setting

On this 16 GB Apple Silicon Mac, full Metal offload failed until the IOGPU wired-memory limit was raised.

Run this after reboot before launching the model:

```bash
sudo sysctl iogpu.wired_limit_mb=14336
```

Verify:

```bash
sysctl iogpu.wired_limit_mb
```

Expected:

```text
iogpu.wired_limit_mb: 14336
```

Without this, llama.cpp can fail with Metal errors like:

```text
Insufficient Memory (00000008:kIOGPUCommandBufferCallbackErrorOutOfMemory)
```

## Serve 32k with TurboQuant build

```bash
cd ~/code/llama.cpp-launch-scripts

GGML_METAL_NO_RESIDENCY=1 \
CTX=32768 NGL=999 BATCH=64 UBATCH=16 \
CACHE_K=turbo3 CACHE_V=turbo3 MTP=0 \
ALIAS=qwen3.6-28b-reap-iq3xxs-turbo3-32k \
./run-qwen36-reap.sh --serve
```

OpenAI-compatible endpoint:

```text
http://127.0.0.1:8081/v1
```

Model alias:

```text
qwen3.6-28b-reap-iq3xxs-turbo3-32k
```

## TurboQuant K/V behavior

The TurboQuant branch accepts:

```bash
CACHE_K=turbo3 CACHE_V=turbo3
```

But for this model it logs:

```text
GQA ratio 8:1 ... upgrading K from turbo3 to q8_0 to prevent quality degradation
```

So by default the effective cache is:

```text
K = q8_0
V = turbo3
```

To force TurboQuant for both K and V:

```bash
TURBO_AUTO_ASYMMETRIC=0 \
CACHE_K=turbo3 CACHE_V=turbo3 \
./run-qwen36-reap.sh --serve
```

Use forced K+V TurboQuant for memory/context experiments; quality may degrade.

## High-context experiment: ~131k

```bash
kill "$(cat /tmp/qwen36-llamacpp-server.pid)" 2>/dev/null || true

TURBO_AUTO_ASYMMETRIC=0 \
GGML_METAL_NO_RESIDENCY=1 \
CTX=131000 NGL=999 BATCH=64 UBATCH=16 \
CACHE_K=turbo3 CACHE_V=turbo3 MTP=0 \
EXTRA_FLAGS="--cache-ram 0" \
ALIAS=qwen3.6-28b-reap-iq3xxs-turbo3-131k \
./run-qwen36-reap.sh --serve
```

`--cache-ram 0` disables llama-server prompt cache so context-capacity tests do not waste memory on saved prompt states.

## Save and restore slot KV cache to disk

The script enables disk slot save/restore by default:

```text
--slot-save-path ~/.cache/llama.cpp-launch-scripts/slot-kv
```

After prefill finishes and the slot is idle, save slot 0:

```bash
curl -sS -X POST 'http://127.0.0.1:8081/slots/0?action=save' \
  -H 'Content-Type: application/json' \
  -d '{"filename":"my-prefill.bin"}'
```

After a server restart with the same model/context/cache settings, restore it:

```bash
curl -sS -X POST 'http://127.0.0.1:8081/slots/0?action=restore' \
  -H 'Content-Type: application/json' \
  -d '{"filename":"my-prefill.bin"}'
```

Prompt cache reuse is also on by default:

```text
--cache-reuse 256
```

## Stop server

```bash
kill "$(cat /tmp/qwen36-llamacpp-server.pid)"
```

## Bundled TurboQuant build notes / troubleshooting

### Existing listener on port 8081

If launch fails because the port is already in use, stop the old listener:

```bash
lsof -nP -iTCP:8081 -sTCP:LISTEN
pid="$(lsof -tiTCP:8081 -sTCP:LISTEN || true)"
[ -n "$pid" ] && kill $pid
```

The launcher writes the current server PID here:

```text
/tmp/qwen36-llamacpp-server.pid
```

### macOS `@rpath` for the bundled TurboQuant build

The bundled TurboQuant binary may reference its original build path:

```text
~/clones/llama-cpp-turboquant/build-metal/bin
```

If startup aborts with a missing library such as `@rpath/libllama-common.0.dylib`, point that expected path at the bundled build:

```bash
ln -s \
  ~/code/llama.cpp-launch-scripts/builds/llama-cpp-turboquant-build-metal \
  ~/clones/llama-cpp-turboquant/build-metal
```

Verify:

```bash
ls -l ~/clones/llama-cpp-turboquant/build-metal/bin/libllama-common.0.dylib
```

### Startup health status

A healthy process can still report loading while the model initializes:

```json
{"error":{"message":"Loading model","type":"unavailable_error","code":503}}
```

Wait and retry until `/health` returns:

```json
{"status":"ok"}
```

## Pi model config

The local Pi model entry should point at:

```text
baseUrl: http://127.0.0.1:8081/v1
provider: llama.cpp
```

For the 32k TurboQuant server, use model id:

```text
qwen3.6-28b-reap-iq3xxs-turbo3-32k
```

For the 131k experiment, use model id:

```text
qwen3.6-28b-reap-iq3xxs-turbo3-131k
```
