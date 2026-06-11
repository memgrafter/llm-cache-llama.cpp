---
id: lcl-jyun
status: open
deps: []
links: []
created: 2026-06-11T06:31:45Z
type: task
priority: 2
assignee: memgrafter
tags: [performance, llamacpp, koboldcpp, prefill, cuda, qwen]
---
# Investigate llama.cpp prefill TPS gap vs KoboldCPP on Unsloth 35B 4-bit

Compare why the Unsloth 35B 4-bit model reportedly reached about 4000 prompt-prefill tok/s in KoboldCPP, while this llama.cpp/proxy stack is closer to about 3000 prompt-prefill tok/s under comparable local CUDA testing. Generation speed is not the focus: KoboldCPP was about 90 gen tok/s and this stack is higher, partly because MTP is available here and not there. Focus on prompt processing/prefill throughput.

## Design

Capture apples-to-apples prefill benchmarks: same model or closest GGUF quant, same context length/prompt shape, same GPU, same CUDA/FA settings if possible, no cache-restore contamination, and no generated tokens or minimal generation. Compare llama.cpp flags (batch/ubatch, flash-attn, KV types, mmap/mlock, offload, threads, cache reuse, MTP/spec disabled for prefill-only), KoboldCPP launch settings, and backend logs/timing fields. Check whether proxy overhead, chat template rendering, cache save/restore, MTP draft context, or different GGUF/quant kernels explain the gap.

## Acceptance Criteria

1. Record exact KoboldCPP command/settings and measured prefill TPS source. 2. Run at least one clean llama.cpp prefill-only or near-prefill-only benchmark with matching prompt/model settings. 3. Produce a table comparing model file, quant, context, batch/ubatch, flash attention, KV cache type, CUDA backend, prompt tokens, prompt eval time, and prefill TPS. 4. Identify likely cause(s) of the ~4000 vs ~3000 tok/s gap or list controlled follow-up experiments.
