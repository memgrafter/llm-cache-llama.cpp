Read-only findings:

- Current backend is **not using speculative decoding**:
  - `MTP=0`
  - log says: `common_speculative_init: no implementations specified for speculative decoding`
- Model is **Qwen3.6 28B REAP / Qwen3.6 35B A3B MoE-derived**
  - MoE-ish model, tight Apple Metal memory setup.
- Current observed generation baseline from logs:
  - prompt eval: ~95–106 tok/s
  - decode: ~25–29 tok/s
- MTP tensors are not present in the GGUF, so agreed: ignore `draft-mtp`.

## Best options, ranked

### 1. First choice: `ngram-mod`

Best immediate option.

Why:
- No draft model needed.
- Very low memory overhead.
- Shared hash pool across server slots.
- Specifically recommended for:
  - repeated code/text
  - reasoning models repeating thinking/final answer
  - summarization
  - MoE models needing long drafts
- Quality risk should be minimal because main model verifies tokens.

Recommended first test:

```bash
EXTRA_FLAGS="--spec-type ngram-mod --spec-ngram-mod-n-match 24 --spec-ngram-mod-n-min 48 --spec-ngram-mod-n-max 64" \
./run-lmcache-proxy-stack.sh
```

Or through the backend only:

```bash
EXTRA_FLAGS="--spec-type ngram-mod --spec-ngram-mod-n-match 24 --spec-ngram-mod-n-min 48 --spec-ngram-mod-n-max 64" \
./run-qwen36-reap.sh --serve
```

This is probably the safest production candidate.

### 2. Second choice: `ngram-map-k4v` for repetitive coding/refactor sessions

Useful if requests contain repeated code blocks, long diffs, generated file rewrites, etc.

Suggested test config:

```bash
EXTRA_FLAGS="--spec-type ngram-map-k4v --spec-ngram-map-k4v-size-n 8 --spec-ngram-map-k4v-size-m 8 --spec-ngram-map-k4v-min-hits 2 --spec-draft-n-max 64" \
./run-lmcache-proxy-stack.sh
```

Could also test combined:

```bash
EXTRA_FLAGS="--spec-type ngram-mod,ngram-map-k4v --spec-ngram-mod-n-match 24 --spec-ngram-mod-n-min 48 --spec-ngram-mod-n-max 64 --spec-ngram-map-k4v-size-n 8 --spec-ngram-map-k4v-size-m 8 --spec-ngram-map-k4v-min-hits 2" \
./run-lmcache-proxy-stack.sh
```

But I’d benchmark `ngram-mod` alone first before combining.

### 3. Draft model speculative decoding: lower priority

Not impossible, but likely not the best first path.

Concerns:
- No local compatible draft GGUF found.
- Main model already uses tight memory: Apple M4, 14 GiB Metal wired limit.
- A draft model would need compatible tokenizer/vocab and ideally high prediction agreement.
- CPU draft may be too slow; GPU draft may not fit.
- For Qwen3.6 REAP/MoE, draft mismatch could mean poor acceptance and no speedup.

Only worth testing later if we find a very small compatible Qwen/Qwen3.6 draft model.

Example shape later:

```bash
EXTRA_FLAGS="--spec-type draft-simple --spec-draft-model /path/to/draft.gguf --spec-draft-n-max 8 --spec-draft-p-min 0.75" \
./run-lmcache-proxy-stack.sh
```

## Proposed benchmark plan

1. Baseline current config:
   - no speculative decoding
   - capture decode tok/s, latency, memory, crash behavior

2. Test `ngram-mod`:
   - default long MoE config:
     ```bash
     --spec-type ngram-mod \
     --spec-ngram-mod-n-match 24 \
     --spec-ngram-mod-n-min 48 \
     --spec-ngram-mod-n-max 64
     ```

3. Test `ngram-map-k4v`:
   - only on code/refactor/repetition-heavy prompts

4. Test combined `ngram-mod,ngram-map-k4v`:
   - only if both individually show benefit

5. Accept config if:
   - no meaningful slowdown on normal chat
   - measurable speedup on coding/reasoning/repetition workloads
   - no increase in backend instability
   - logs show useful accepted-token stats:
     ```text
     statistics ngram_mod: ...
     draft acceptance rate = ...
     ```

## Implementation direction later

Add explicit env knobs to `run-qwen36-reap.sh`, probably:

```bash
SPEC_TYPE="${SPEC_TYPE:-}"
SPEC_NGRAM_MOD_N_MATCH="${SPEC_NGRAM_MOD_N_MATCH:-24}"
SPEC_NGRAM_MOD_N_MIN="${SPEC_NGRAM_MOD_N_MIN:-48}"
SPEC_NGRAM_MOD_N_MAX="${SPEC_NGRAM_MOD_N_MAX:-64}"
```

Then wire them into `args`. For now, no code changes needed because `EXTRA_FLAGS` already supports testing.
