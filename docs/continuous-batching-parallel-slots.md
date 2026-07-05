# Continuous Batching & Parallel Slots

## TL;DR

With `--parallel N` (e.g. `PARALLEL=2`) and continuous batching enabled (the default), llama-server can process a **prefill** on one slot alongside a **generating** slot in the same `llama_decode()` call. The generating slot contributes 1 token to the batch; the prefill slot fills the rest of the batch capacity. This is how you overlap prefill and decoding across GPUs.

## How it works

The core loop lives in `update_slots()` ([server-context.cpp:2386](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp#L2386)).

### Step 1 — Collect generating slots

All slots in `SLOT_STATE_GENERATING` are gathered, and `slot.update_batch(batch)` is called for each, adding **1 token** (the sampled next token) per slot to the batch.

- Collecting: [server-context.cpp:2488](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp#L2488)
- `update_batch()`: [server-context.cpp:351](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp#L351) — adds the sampled token via `common_batch_add()`

### Step 2 — Continuous batching (prefill alongside generation)

If `cont_batching` is true (default: **true**), or if the batch is empty, the engine processes prompt slots (`SLOT_STATE_PROCESSING_PROMPT` / `SLOT_STATE_STARTED`), adding as many tokens as fit within `n_batch`.

- Gate check: [server-context.cpp:2616](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp#L2616)
  ```cpp
  if (params_base.cont_batching || batch.n_tokens == 0) {
  ```
- Prompt batching loop: [server-context.cpp:2618](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp#L2618)

### Compatibility check — `can_batch_with`

Slots can be batched together if they have the same task type and matching LoRA adapters. This check does **not** prevent mixing generating + prefill states.

- Definition: [server-context.cpp:289](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp#L289)
  ```cpp
  bool can_batch_with(server_slot & other_slot) const {
      return task->type == other_slot.task->type && are_lora_equal(lora, other_slot.lora);
  }
  ```
- Applied in generating loop: [server-context.cpp:2497](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp#L2497)
- Applied in prompt loop: [server-context.cpp:2623](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp#L2623)

### Step 3 — Decode the combined batch

The entire batch (1 token from generating + up to `n_batch - 1` tokens from prefill) is decoded in a single `llama_decode()` call.

- Decode loop: [server-context.cpp:3192](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp#L3192)

## Example: 2-GPU, PARALLEL=2

With `PARALLEL=2` and `BATCH=4096`:

| Slot | State | Tokens in batch |
|------|-------|-----------------|
| 1    | Generating (decoding) | 1 |
| 2    | Prefilling (new prompt) | up to 4095 |

Both are decoded together. The prefill gets full GPU throughput; the generating slot's single token rides along for virtually free.

## Key variables

| Variable | Default | Source | Description |
|----------|---------|--------|-------------|
| `PARALLEL` | 1 | [run-lmcache-proxy-stack.sh](../run-lmcache-proxy-stack.sh) | Number of parallel slots (`--parallel N`) |
| `cont_batching` | true | [common.h:544](https://github.com/ggml-org/llama.cpp/blob/master/common/common.h#L544) | Enable continuous (dynamic) batching |

## References

- `update_slots()` main loop — [server-context.cpp:2386](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp#L2386)
- Continuous batching gate — [server-context.cpp:2616](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp#L2616)
- `can_batch_with()` — [server-context.cpp:289](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp#L289)
- `update_batch()` (generating slot → 1 token) — [server-context.cpp:351](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp#L351)
- `cont_batching` flag definition — [common.h:544](https://github.com/ggml-org/llama.cpp/blob/master/common/common.h#L544)
- CLI arg for `--cont-batching` — [arg.cpp:2187](https://github.com/ggml-org/llama.cpp/blob/master/common/arg.cpp#L2187)

