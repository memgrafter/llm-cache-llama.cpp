---
id: lcl-9itx
status: open
deps: []
links: [lcl-axnv]
created: 2026-07-07T12:48:13Z
type: task
priority: 2
assignee: memgrafter
tags: [benchmark, llm, pcie]
---
# Measure generation (decode) TPS curve with ubatch=4096

Test whether decode throughput follows the same degradation pattern as prefill at long contexts, to determine if the bottleneck is memory-bandwidth (same curve on both) or PCIe-specific (prefill-only).

## Notes

**2026-07-07T12:48:36Z**

## Background

Same setup as lcl-axnv. We've observed prefill TPS degradation at long contexts and need to determine if the same pattern holds for decode (generation) throughput.

Key distinction:
- **Prefill**: processes all tokens in context simultaneously → massive KV cache reads + PCIe transfers per batch
- **Decode**: processes one token at a time → minimal PCIe transfer, but still reads growing KV cache

## Hypothesis

If memory bandwidth is the root bottleneck, decode TPS should show the same degradation pattern (starts high, drops as context grows) because each decode step must read the full KV cache for that GPU's layers.

If PCIe is the dominant bottleneck, decode should be largely unaffected since only 1 token crosses the boundary per step (negligible transfer).

## Experiment

1. Set ubatch=4096 (consistent with prefill experiment)
2. Run generation (not prefill) at increasing context lengths: 10k, 50k, 75k, 100k
3. Record decode TPS at each point
4. Compare curve shape to prefill curve and single-GPU decode baseline

## Expected outcomes

**If memory bandwidth is the bottleneck:**
- Decode TPS drops similarly: high at short context → low at 100k
- Ratio vs single GPU stays roughly constant (2x)
- Confirms KV cache read bandwidth is the limiting factor for both phases

**If PCIe is the dominant bottleneck:**
- Decode TPS stays relatively flat across context lengths
- Little to no degradation from 10k → 100k
- Confirms prefill-specific issue, not general memory-bandwidth problem

## Inferences

- Similar degradation curve → memory bandwidth is the root cause, PCIe is secondary. Focus on KV cache optimization (e.g., fp8 helps, but access patterns matter more)
- Flat decode curve → prefill-specific bottleneck. Likely PCIe transfer overhead per batch or activation memory staging. ubatch tuning would help more than hardware changes
