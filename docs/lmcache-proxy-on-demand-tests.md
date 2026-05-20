# LMCache Proxy (On-Demand) — Happy Path Test Plan

## Goal

Verify the on-demand proxy correctly loads KV states from disk, checks metadata compatibility, restores into idle slots, and forwards requests to llama.cpp.

---

## Test Structure

All tests use `lmcache-proxy-on-demand.py` as the target. No integration with a real llama.cpp server — mock everything that would normally contact it.

---

## Test 1: KVCache metadata loading

**What it tests:** `.meta.json` sidecar is loaded correctly from disk.

```python
def test_cache_loads_metadata():
    # Setup: create cache dir, write .bin and .meta.json
    cache_dir = tempfile.mkdtemp()
    prefix_hash = hashlib.sha256(b"test prompt").hexdigest()[:32]
    kv_path = pathlib.Path(cache_dir) / prefix_hash / "slot_0_1715000000.bin"
    meta_path = kv_path.parent / "slot_0_1715000000.meta.json"
    kv_path.parent.mkdir(parents=True, exist_ok=True)

    # Write metadata sidecar
    meta = {
        "model_hash": "abc123",
        "context_size": 4096,
        "layer_count": 80,
        "num_kv_heads": 32,
        "head_dim": 128,
        "kv_format": "f16",
        "saved_at": "2025-05-20T12:00:00Z",
        "slot_id": 0,
    }
    meta_path.write_text(json.dumps(meta))
    kv_path.touch()

    # Act
    cache = KVCache(cache_dir)
    loaded_meta = cache.load_metadata(str(kv_path))

    # Assert
    assert loaded_meta is not None
    assert loaded_meta["model_hash"] == "abc123"
    assert loaded_meta["context_size"] == 4096
```

**Also test:** missing `.meta.json` returns `None` without raising.

---

## Test 2: Compatibility check — matching model

**What it tests:** `_is_compatible()` returns True when metadata matches server info.

```python
def test_compatibility_match():
    cache = KVCache(tempfile.mkdtemp())

    meta = {"model_hash": "abc123", "context_size": 4096}
    server_info = {"model_hash": "abc123", "context_size": 4096}

    assert cache.is_compatible(meta, server_info) is True
```

---

## Test 3: Compatibility check — mismatched model hash

**What it tests:** A KV from a different model is rejected.

```python
def test_compatibility_mismatch_model():
    cache = KVCache(tempfile.mkdtemp())

    meta = {"model_hash": "abc123", "context_size": 4096}
    server_info = {"model_hash": "def456", "context_size": 4096}

    assert cache.is_compatible(meta, server_info) is False
```

---

## Test 4: Compatibility check — mismatched context size

**What it tests:** A KV with different context size is rejected even if model hash matches.

```python
def test_compatibility_mismatch_context():
    cache = KVCache(tempfile.mkdtemp())

    meta = {"model_hash": "abc123", "context_size": 4096}
    server_info = {"model_hash": "abc123", "context_size": 2048}

    assert cache.is_compatible(meta, server_info) is False
```

---

## Test 5: Compatibility check — missing metadata keys

**What it tests:** `_is_compatible()` returns False when required keys are absent.

```python
def test_compatibility_missing_keys():
    cache = KVCache(tempfile.mkdtemp())

    meta = {"model_hash": "abc123"}  # missing context_size
    server_info = {"model_hash": "abc123", "context_size": 4096}

    assert cache.is_compatible(meta, server_info) is False
```

---

## Test 6: KV lookup by prompt prefix hash

**What it tests:** `find_match()` returns cached files whose prompt prefix matches.

```python
def test_find_match_by_prefix():
    cache_dir = tempfile.mkdtemp()
    prefix_hash = hashlib.sha256(b"hello world").hexdigest()[:32]
    kv_path = pathlib.Path(cache_dir) / prefix_hash / "slot_0_1715000000.bin"
    kv_path.parent.mkdir(parents=True, exist_ok=True)
    kv_path.touch()

    cache = KVCache(cache_dir)
    results = cache.find_match("hello world")

    assert len(results) == 1
    assert results[0].endswith(".bin")
```

---

## Test 7: On-demand KV restore via handler — happy path

**What it tests:** `_handle_request()` finds a cached KV, loads metadata, checks compatibility, restores into an idle slot, and forwards the request.

```python
def test_handler_restores_kv_on_demand():
    # Setup: create mock KV cache with compatible metadata
    cache_dir = tempfile.mkdtemp()
    # ... (setup .bin and .meta.json as in Test 1)

    # Create a KVCache instance with the mock data
    cache = KVCache(cache_dir)

    # Mock server model info
    LMCacheHandler.server_model_info = {
        "model_hash": "abc123",
        "context_size": 4096,
    }
    LMCacheHandler.cache_dir_obj = cache

    # Mock _get_available_slot() to return slot 0
    def mock_get_slot():
        return 0

    # Mock _restore_slot() to succeed
    restore_called = []
    original_restore = globals().get('_restore_slot')
    def mock_restore(slot_id, kv_path, server, port):
        restore_called.append((slot_id, kv_path))
        return True
    globals()['_restore_slot'] = mock_restore

    # Act: call _handle_request with a POST containing a prompt that matches the cached KV
    handler = LMCacheHandler(...)  # set up with mock request data
    handler._extract_prompts = lambda body: ["hello world"]  # match cached prompt
    handler._handle_request("POST")

    # Assert
    assert len(restore_called) == 1
    assert restore_called[0][0] == 0  # slot 0
```

---

## Test 8: Compatibility check skips restore when metadata doesn't match

**What it tests:** When the cached KV's model hash doesn't match the server, no restore is attempted.

```python
def test_handler_skips_incompatible_kv():
    cache_dir = tempfile.mkdtemp()
    # ... (setup .bin with different model_hash)

    cache = KVCache(cache_dir)
    LMCacheHandler.server_model_info = {
        "model_hash": "def456",  # different from cached
        "context_size": 4096,
    }
    LMCacheHandler.cache_dir_obj = cache

    restore_called = []
    def mock_restore(slot_id, kv_path, server, port):
        restore_called.append((slot_id, kv_path))
        return True

    # ... set up handler with mock request ...

    handler._handle_request("POST")

    assert len(restore_called) == 0  # no restore attempted
```

---

## Test 9: Server model info fetched from /health

**What it tests:** `_get_server_model_info()` correctly parses the `/health` endpoint response.

```python
def test_get_server_model_info():
    # Mock the urllib request
    class MockResponse:
        status = 200
        def read(self):
            return json.dumps({
                "model": {
                    "path": "/path/to/model.gguf",
                    "ctx_size": 4096,
                }
            }).encode()

    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = lambda *args, **kwargs: MockResponse()

    try:
        result = _get_server_model_info("localhost", 8081)
        assert result is not None
        assert result["model_hash"] == hashlib.sha256(b"/path/to/model.gguf").hexdigest()[:16]
        assert result["context_size"] == 4096
    finally:
        urllib.request.urlopen = original_urlopen
```

---

## Test 10: Full flow — request → KV restore → forward

**What it tests:** The complete happy path from client request through proxy to llama.cpp.

This test is the integration test that ties all the pieces together:

1. Client sends a POST with a prompt matching a cached KV
2. Proxy's `KVCache.find_match()` finds the `.bin` file
3. Proxy loads `.meta.json` and checks compatibility against server info
4. Proxy calls `_restore_slot()` into an idle slot
5. Proxy forwards the request to llama.cpp
6. No errors are raised

```python
def test_full_flow():
    # Setup: cache dir with .bin and .meta.json
    # Mock: _get_available_slot returns slot 0
    # Mock: _restore_slot succeeds
    # Mock: _forward completes without error
    # Act: handler processes a POST request
    # Assert: restore was called once, forward was called once
    pass
```

---

## Test Setup Notes

- Use `unittest.mock` for all external dependencies (llama.cpp endpoints, KV restore)
- Create temp directories with `tempfile.mkdtemp()` for each test's cache data
- Mock `urllib.request.urlopen` for `/health` and `/slots` endpoints
- No need to run a real llama.cpp server — the proxy logic is self-contained

---

## Dependencies Noted in Tests

| Test | External Dependency | How to Mock |
|---|---|---|
| 1, 6 | Filesystem | `tempfile.mkdtemp()` + direct file writes |
| 2-5 | KVCache.is_compatible() | Direct method calls on KVCache instance |
| 7, 8 | _restore_slot(), _get_available_slot() | Mock functions |
| 9 | /health endpoint | Mock urllib.request.urlopen |
| 10 | Full proxy behavior | Mock all external calls |

---

## What These Tests Don't Cover

- **Integration with real llama.cpp** — that's a separate concern; these tests verify the proxy logic in isolation
- **Edge cases** (concurrent requests, corrupted metadata files, missing KV binaries) — handle in follow-up
- **Performance** — no benchmarks for KV restore latency or cache lookup speed
- **The hypothetical `/slots/test-match` endpoint** — not yet implemented in llama.cpp
