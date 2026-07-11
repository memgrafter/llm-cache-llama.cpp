---
id: lcl-axnv
status: open
deps: []
links: [lcl-9itx]
created: 2026-07-07T12:48:13Z
type: task
priority: 2
assignee: memgrafter
tags: [benchmark, llm, pcie]
---
# Measure prefill TPS curve with ubatch=4096

Test whether increasing ubatch from 1024 to 4096 improves prefill throughput by amortizing PCIe transfer overhead across more tokens per batch.

## Notes

**2026-07-07T12:48:36Z**

## Background

Pipeline parallel (32/32 split) on two RTX 3090 Ti with llama.cpp. GPU 0 on CPU lanes (PCIe 4.0 x8), GPU 1 on chipset lanes (PCIe 3.0 x4). Current ubatch=1024.

Observed prefill TPS curve at ubatch=1024:
- Start: ~2400 TPS
- Drops instantly to ~2000
- At 100k context: drops below 1500

Single GPU baseline (same model, same context):
- Start: ~1200 → 1000 → ~750 at 100k

The 2x ratio is consistent throughout, suggesting the bottleneck scales linearly.

## Hypothesis

At ubatch=1024, each prefill batch transfers ~20 MB across PCIe per stage boundary (1024 × 5120 × 4 bytes). At PCIe 3.0 x4 (~1 GB/s), that's ~40ms per batch. The compute per batch is so fast that the fixed PCIe transfer overhead dominates throughput.

Raising ubatch to 4096 does 4× the work for the same ~40ms PCIe transfer, amortizing the overhead and improving effective TPS.

## Experiment

1. Set llama.cpp ubatch=4096 (keep all other params identical)
2. Run prefill at increasing context lengths: 10k, 50k, 75k, 100k
3. Record TPS at each point
4. Compare curve to ubatch=1024 baseline

## Expected outcomes

**If PCIe overhead is the bottleneck:**
- Starting TPS increases (more tokens per batch = better amortization)
- The curve degrades less severely — gap between start and 100k narrows
- Ratio vs single GPU may exceed 2x at shorter contexts

**If memory bandwidth is the dominant bottleneck:**
- Curve shape stays roughly the same (same degradation pattern)
- Absolute TPS may increase slightly but proportionally
- 2x ratio persists throughout

## Inferences

- Large improvement → PCIe transfer overhead per batch was significant, ubatch tuning matters
- Small/no improvement → memory bandwidth for KV cache reads is the true bottleneck regardless of batch size
