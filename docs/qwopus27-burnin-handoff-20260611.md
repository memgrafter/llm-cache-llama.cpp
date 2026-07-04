# Qwopus 27B v2 MTP Q4_K_M burn-in handoff

Date: 2026-06-11
Repo: `/home/robbintt/code/llm-cache-llama.cpp`
Benchmark repo: `/home/robbintt/code/vast-ai-provisioning`

## Current good config

Wrapper:

```text
run-qwopus36-27b-v2-mtp-q4km-nonthinking.sh
```

Model:

```text
models/Qwopus3.6-27B-v2-MTP-Q4_K_M.gguf
```

Runtime defaults:

```text
CTX=128000
MTP=3
REASONING=off
BATCH=4096
UBATCH=1024
CACHE_K=q8_0
CACHE_V=q8_0
```

Pi/benchmark model alias:

```text
qwopus36-27b-v2-mtp-q4km-128k-mtp3-b4096-u1024-nonthinking
```

Provider endpoint:

```text
http://192.168.1.21:8081/v1
```

## Important context findings

- `128k + MTP=3` works and is the current preferred 27B non-thinking setup.
- `160k + MTP=0` loads and passes a hello-world smoke.
- `160k + MTP=1` loads but OOMs on first tiny request; do not use without reducing memory elsewhere.
- Thinking mode works technically but tends to spend very large reasoning budgets and can miss benchmark format.
- Non-thinking mode is much more benchmark-stable.

## Recent successful burn-ins

### Burn-in 1

Run:

```text
benchmark/runs/qwopus27-v2-mtp-q4km-mtp3-nonthinking-burnin20-20260611-104136/report.md
```

Result:

```text
HTTP 200: 20 / 20
Valid: 20 / 20
Max context: 73,459
Truncated: 0
CUDA crash: no
Proxy 502: no
Weighted prefill TPS: 485.64 tok/s
Weighted generation TPS: 32.14 tok/s
```

### Burn-in repeat2

Run:

```text
benchmark/runs/qwopus27-v2-mtp-q4km-mtp3-nonthinking-burnin20-repeat2-20260611-114702/report.md
```

Result:

```text
HTTP 200: 20 / 20
Valid: 20 / 20
Max context: 68,780
Truncated: 0
CUDA crash: no
Proxy 502: no
Weighted prefill TPS: 548.79 tok/s
Weighted generation TPS: 39.04 tok/s
```

Cache behavior in repeat2:

```text
POST: 20
full-prefix restores: 19
anchor restores: 0
```

No obvious kernel/NVRM/Xid/AER/reset errors were seen in the quick tail checks of:

```text
kernel-gpu-burnin.log
nvidia-smi-burnin.csv
```

## Script used to properly run the 20-round burn-in

This is the cleaned-up script pattern that stops the existing stack first, launches the correct wrapper with fresh logs/cache, waits for health, then runs 20 benchmark rounds against the LAN IP.

```bash
#!/usr/bin/env bash
set -euo pipefail

LLM_REPO=/home/robbintt/code/llm-cache-llama.cpp
BENCH_REPO=/home/robbintt/code/vast-ai-provisioning
MODEL_ALIAS=qwopus36-27b-v2-mtp-q4km-128k-mtp3-b4096-u1024-nonthinking

cd "$LLM_REPO"

# Stop current benchmark processes if any.
pids="$(pgrep -f 'run_deterministic_agentic_benchmark.py' || true)"
if [ -n "$pids" ]; then
  echo "Stopping benchmark process(es): $pids"
  kill $pids || true
  sleep 2
fi

# Stop current stack/listeners.
if [ -f /tmp/lmcache-proxy-stack.pid ] && kill -0 "$(cat /tmp/lmcache-proxy-stack.pid)" 2>/dev/null; then
  echo "Stopping stack PID $(cat /tmp/lmcache-proxy-stack.pid)"
  kill "$(cat /tmp/lmcache-proxy-stack.pid)" || true
fi

for pf in /tmp/lmcache-proxy.pid /tmp/qwen36-llamacpp-backend.pid; do
  if [ -f "$pf" ]; then
    pid="$(cat "$pf" 2>/dev/null || true)"
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  fi
done

for i in $(seq 1 45); do
  if ! lsof -tiTCP:8081 -sTCP:LISTEN >/dev/null 2>&1 && \
     ! lsof -tiTCP:8082 -sTCP:LISTEN >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

for port in 8081 8082; do
  pids="$(lsof -tiTCP:$port -sTCP:LISTEN 2>/dev/null || true)"
  [ -n "$pids" ] && kill -9 $pids || true
done

sleep 1

# Launch fresh 128k/MTP3/non-thinking stack.
run_stamp="$(date +%Y%m%d-%H%M%S)"
echo "$run_stamp" >/tmp/qwopus27-burnin20-stamp
cache_dir="$HOME/.cache/llama.cpp-launch-scripts/qwopus36-27b-v2-mtp-q4km-128k-mtp3-nonthinking-burnin20-$run_stamp"

STAMP=qwopus27-v2-mtp-q4km-128k-mtp3-nonthinking-burnin20-$run_stamp \
PUBLIC_HOST=0.0.0.0 \
ALIAS="$MODEL_ALIAS" \
CACHE_DIR="$cache_dir" \
PREFIX_CACHE_MAX_BYTES=16GiB \
LLAMA_FORWARD_TIMEOUT=2400 \
./run-qwopus36-27b-v2-mtp-q4km-nonthinking.sh --background

for i in $(seq 1 180); do
  if curl -sS --max-time 2 http://127.0.0.1:8081/health 2>/dev/null | grep -q 'ok'; then
    echo "ready-after=${i}s"
    break
  fi
  sleep 1
  if [ "$i" = 180 ]; then
    echo timeout
    tail -120 "logs/qwen36-backend-qwopus27-v2-mtp-q4km-128k-mtp3-nonthinking-burnin20-$run_stamp.log"
    exit 1
  fi
done

backend_log="$LLM_REPO/logs/qwen36-backend-qwopus27-v2-mtp-q4km-128k-mtp3-nonthinking-burnin20-$run_stamp.log"
proxy_log="$LLM_REPO/logs/lmcache-proxy-qwopus27-v2-mtp-q4km-128k-mtp3-nonthinking-burnin20-$run_stamp.log"

printf 'stamp=%s\nendpoint=http://192.168.1.21:8081/v1\nmodel=%s\nbackend_log=%s\nproxy_log=%s\n' \
  "$run_stamp" "$MODEL_ALIAS" "$backend_log" "$proxy_log"

# Confirm runtime settings.
rg -n 'CTX=|MTP=|REASONING=|draft-mtp|thinking =|new slot|n_ctx' \
  "logs/qwen36-backend-qwopus27-v2-mtp-q4km-128k-mtp3-nonthinking-burnin20-$run_stamp.log" | head -50

nvidia-smi --query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,power.draw,temperature.gpu,clocks.sm,clocks.mem \
  --format=csv,noheader,nounits || true

# Run 20 deterministic accumulated-context rounds.
cd "$BENCH_REPO"
run_id="qwopus27-v2-mtp-q4km-mtp3-nonthinking-burnin20-$(date +%Y%m%d-%H%M%S)"
echo "run_id=$run_id"

python3 benchmark/run_deterministic_agentic_benchmark.py \
  --backend llama.cpp \
  --manifest benchmark/problem_manifest.example.json \
  --base-url http://192.168.1.21:8081/v1 \
  --model "$MODEL_ALIAS" \
  --pi-models-config /home/robbintt/.pi/agent/models.json \
  --thinking off \
  --backend-log "$backend_log" 