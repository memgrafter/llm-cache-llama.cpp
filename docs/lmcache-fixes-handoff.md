# LMCache Proxy — One-Pager Handoff

## Goal
Intercept requests, find matching KV states on disk, restore them into any available slot, then forward to llama.cpp. `--slot-prompt-similarity` handles the rest.

---

## Metadata Sidecar: `<name>.meta.json`

**Problem:** KV states are model-specific (model architecture, context length, layer count). A KV from one model is incompatible with another — and there's no guard against cross-model restore.

**Solution:** Store a tiny JSON sidecar next to each KV binary:

```
<cache_dir>/
  <sha32_prefix>/
    slot_0_1715000000.bin          # KV state (~500 MB for Qwen3.6-28B)
    slot_0_1715000000.meta.json    # metadata (few hundred bytes)
```

**`slot_0_1715000000.meta.json`:**
```json
{
  "model_file": "$HOME/Downloads/Qwen3.6-28B-REAP.i1-IQ3_XXS.gguf",
  "model_hash": "sha256:<first 16 hex chars>",
  "context_size": 4096,
  "layer_count": 80,
  "num_kv_heads": 32,
  "head_dim": 128,
  "kv_format": "f16",
  "saved_at": "2025-05-20T12:00:00Z",
  "slot_id": 0
}
```

**Usage:** Before restoring any KV file, load its `.meta.json` and check compatibility with the running server's model. If `model_hash` or `context_size` doesn't match, skip it.

---

## Minimal Changes to `lmcache-proxy.py`

### 1. Add metadata-aware KV lookup on request interception

In `LMCacheHandler._handle_request()`, before forwarding:

```python
# Extract prompts from request body
prompts = self._extract_prompts(body)

# Find matching KV states in cache
for prompt_text in prompts:
    kv_files = self.cache_dir_obj.find_match(prompt_text)
    if not kv_files:
        continue

    # Load metadata and check compatibility before restoring
    meta_path = kv_files[0].replace('.bin', '.meta.json')
    meta = self._load_metadata(meta_path)
    if not meta or not self._is_compatible(meta):
        log.debug("KV incompatible, skipping: %s", kv_files[0])
        continue

    # Find any idle slot (don't care which one)
    slot = self._get_available_slot()
    if slot is None:
        break

    # Restore KV into that slot
    result = _restore_slot(slot, kv_files[0], ...)
    if result:
        log.info("restored KV into slot %d", slot)
        break  # only need one match per request
```

### 2. `_get_available_slot()` — find first idle slot

```python
def _get_available_slot(self) -> int | None:
    """Find an idle slot via llama.cpp's /slots endpoint."""
    try:
        url = f"http://{self.llama_server}:{self.llama_port}/slots"
        with urllib.request.urlopen(url, timeout=10) as resp:
            slots = json.loads(resp.read().decode())
            for s in slots:
                if not s.get("is_busy", False):
                    return s["id"]  # first non-busy slot
    except Exception:
        pass
    return None
```

### 3. `_load_metadata()` and `_is_compatible()` helpers

```python
def _load_metadata(self, meta_path: str) -> dict | None:
    """Load metadata sidecar if it exists."""
    try:
        with open(meta_path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def _is_compatible(self, meta: dict) -> bool:
    """Check if cached KV is compatible with current server config."""
    # Compare model hash and context size against running server's model
    server_model = self._get_server_model_info()  # from llama.cpp /health or similar
    return (meta.get("model_hash") == server_model.get("hash") and
            meta.get("context_size") == server_model.get("ctx_size"))
```

### 4. Remove the SlotManager background thread

The old design tried to pre-load KV states into idle slots on a timer. With the new approach, we load KV on-demand when a request arrives — no background thread needed. Delete `SlotManager` class entirely.

---

## Server Flags Required

```bash
llama-server \
  --slot-save-path ~/.cache/llm-kv \
  --cache-idle-slots \
  --slot-prompt-similarity 0.15 \
  ...
```

- `--slot-save-path` — where KV states are saved to disk when slots go idle
- `--cache-idle-slots` — automatically save KV when slot becomes idle
- `--slot-prompt-similarity 0.15` — threshold for prompt matching (0.1 = 10% match)

---

## Flow

```
Client → Proxy → llama.cpp
         ↓
    Extract prompt from request body
    Hash prefix → lookup KV on disk
    If found:
        Load .meta.json sidecar
        Check model/context compatibility
        Find idle slot via /slots API
        Restore KV into that slot
    Forward request to llama.cpp
    ↓
    --slot-prompt-similarity matches prompt to restored slot
    Reuse KV cache → skip prefill ✓
```

---

## Key Assumptions

- Single user / no concurrent conflicting slots
- KV states on disk have a `.meta.json` sidecar for model compatibility
- `--slot-prompt-similarity` is enabled and set to a reasonable threshold (0.1–0.2)

---

## Future: Slot Prefix Match Test Endpoint (Hypothetical)

**What we want:** A llama.cpp endpoint that tests whether the current slot's KV prefix matches a given prompt, *without* actually processing the prompt.

```bash
# Proposed API:
curl -X POST http://localhost:8081/slots/test-match \
  -d '{"slot": 0, "prompt": "hello world", "threshold": 0.15}'
# Response:
{
  "matched": true,
  "prefix_match_pct": 87.3
}
```

**Why this is useful:**
- Lets the proxy *test* a slot's prefix match before restoring KV from disk
- Avoids the I/O cost of loading a 500 MB KV file only to discover it doesn't match
- Would be a no-cost check against the slot's in-memory KV state

**Current status:** This endpoint does not exist in llama.cpp. It would require adding a new REST handler that compares a prompt against the slot's KV prefix without running inference.

**Workaround today:** The proxy relies on prompt-prefix hashing to find candidate KV files, then restores and lets `--slot-prompt-similarity` reject mismatches. This is suboptimal because it pays the I/O cost of loading the KV before discovering incompatibility.

---

## KV Cache Size Reference (Qwen3.6-28B)

| Metric | Value |
|---|---|
| GGUF model size | 11.2 GB |
| KV per idle slot | ~500 MB (ctx=4096, 80 layers, 32 KV heads) |
| `.meta.json` sidecar | ~300 bytes |
| With multiple slots | Several GB easily |

**Implication:** Metadata sidecars are negligible overhead vs the KV binaries. They're worth doing because they prevent loading incompatible 500 MB files.
