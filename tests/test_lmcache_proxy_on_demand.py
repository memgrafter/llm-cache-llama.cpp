import hashlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "lmcache-proxy-on-demand.py"

spec = importlib.util.spec_from_file_location("lmcache_proxy_on_demand", MODULE_PATH)
lmcache = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lmcache
spec.loader.exec_module(lmcache)


class LMCacheProxyOnDemandTests(unittest.TestCase):
    def _create_cache_entry(self, cache_dir, prompt="hello world", meta=None, slot_id=0, timestamp=1715000000):
        prefix_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:32]
        kv_path = pathlib.Path(cache_dir) / prefix_hash / f"slot_{slot_id}_{timestamp}.bin"
        meta_path = kv_path.parent / f"slot_{slot_id}_{timestamp}.meta.json"
        kv_path.parent.mkdir(parents=True, exist_ok=True)
        if meta is not None:
            meta_path.write_text(json.dumps(meta))
        kv_path.touch()
        return kv_path, meta_path

    def _make_handler(self, body, path="/completion"):
        body_bytes = json.dumps(body).encode("utf-8")
        handler = object.__new__(lmcache.LMCacheHandler)
        handler.headers = {"Content-Length": str(len(body_bytes))}
        handler.rfile = io.BytesIO(body_bytes)
        handler.wfile = io.BytesIO()
        handler.path = path
        handler.llama_server = "localhost"
        handler.llama_port = 8081
        return handler, body_bytes

    def test_cache_loads_metadata(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            prefix_hash = hashlib.sha256(b"test prompt").hexdigest()[:32]
            kv_path = pathlib.Path(cache_dir) / prefix_hash / "slot_0_1715000000.bin"
            meta_path = kv_path.parent / "slot_0_1715000000.meta.json"
            kv_path.parent.mkdir(parents=True, exist_ok=True)

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

            cache = lmcache.KVCache(cache_dir)
            loaded_meta = cache.load_metadata(str(kv_path))

            self.assertIsNotNone(loaded_meta)
            self.assertEqual(loaded_meta["model_hash"], "abc123")
            self.assertEqual(loaded_meta["context_size"], 4096)

    def test_missing_metadata_returns_none(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            prefix_hash = hashlib.sha256(b"test prompt").hexdigest()[:32]
            kv_path = pathlib.Path(cache_dir) / prefix_hash / "slot_0_1715000000.bin"
            kv_path.parent.mkdir(parents=True, exist_ok=True)
            kv_path.touch()

            cache = lmcache.KVCache(cache_dir)

            self.assertIsNone(cache.load_metadata(str(kv_path)))

    def test_compatibility_match(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            cache = lmcache.KVCache(cache_dir)
            meta = {"model_hash": "abc123", "context_size": 4096}
            server_info = {"model_hash": "abc123", "context_size": 4096}

            self.assertTrue(cache.is_compatible(meta, server_info))

    def test_compatibility_mismatch_model(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            cache = lmcache.KVCache(cache_dir)
            meta = {"model_hash": "abc123", "context_size": 4096}
            server_info = {"model_hash": "def456", "context_size": 4096}

            self.assertFalse(cache.is_compatible(meta, server_info))

    def test_compatibility_mismatch_context(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            cache = lmcache.KVCache(cache_dir)
            meta = {"model_hash": "abc123", "context_size": 4096}
            server_info = {"model_hash": "abc123", "context_size": 2048}

            self.assertFalse(cache.is_compatible(meta, server_info))

    def test_compatibility_missing_keys(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            cache = lmcache.KVCache(cache_dir)
            meta = {"model_hash": "abc123"}
            server_info = {"model_hash": "abc123", "context_size": 4096}

            self.assertFalse(cache.is_compatible(meta, server_info))

    def test_find_match_by_prefix(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            prefix_hash = hashlib.sha256(b"hello world").hexdigest()[:32]
            kv_path = pathlib.Path(cache_dir) / prefix_hash / "slot_0_1715000000.bin"
            kv_path.parent.mkdir(parents=True, exist_ok=True)
            kv_path.touch()

            cache = lmcache.KVCache(cache_dir)
            results = cache.find_match("hello world")

            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].endswith(".bin"))

    def test_find_match_excludes_meta_json(self):
        """find_match should only return .bin files, not .meta.json sidecars."""
        with tempfile.TemporaryDirectory() as cache_dir:
            prefix_hash = hashlib.sha256(b"hello world").hexdigest()[:32]
            kv_path = pathlib.Path(cache_dir) / prefix_hash / "slot_0_1715000000.bin"
            meta_path = pathlib.Path(cache_dir) / prefix_hash / "slot_0_1715000000.meta.json"
            kv_path.parent.mkdir(parents=True, exist_ok=True)
            kv_path.touch()
            meta_path.touch()

            cache = lmcache.KVCache(cache_dir)
            results = cache.find_match("hello world")

            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].endswith(".bin"))

    def test_handler_restores_kv_on_demand(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            meta = {"model_hash": "abc123", "context_size": 4096}
            kv_path, _ = self._create_cache_entry(cache_dir, meta=meta)
            cache = lmcache.KVCache(cache_dir)
            handler, body_bytes = self._make_handler({"prompt": "hello world"})
            handler._get_available_slot = mock.Mock(return_value=0)
            handler._forward = mock.Mock(return_value=None)

            with mock.patch.object(lmcache.LMCacheHandler, "server_model_info", {"model_hash": "abc123", "context_size": 4096}), \
                 mock.patch.object(lmcache.LMCacheHandler, "cache_dir_obj", cache), \
                 mock.patch.object(lmcache, "_restore_slot", return_value=True) as restore:
                handler._handle_request("POST")

            restore.assert_called_once_with(0, str(kv_path), "localhost", 8081)
            handler._forward.assert_called_once_with("POST", "/completion", body_bytes)

    def test_handler_skips_incompatible_kv(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            meta = {"model_hash": "abc123", "context_size": 4096}
            self._create_cache_entry(cache_dir, meta=meta)
            cache = lmcache.KVCache(cache_dir)
            handler, body_bytes = self._make_handler({"prompt": "hello world"})
            handler._get_available_slot = mock.Mock(return_value=0)
            handler._forward = mock.Mock(return_value=None)

            with mock.patch.object(lmcache.LMCacheHandler, "server_model_info", {"model_hash": "def456", "context_size": 4096}), \
                 mock.patch.object(lmcache.LMCacheHandler, "cache_dir_obj", cache), \
                 mock.patch.object(lmcache, "_restore_slot", return_value=True) as restore:
                handler._handle_request("POST")

            restore.assert_not_called()
            handler._get_available_slot.assert_not_called()
            handler._forward.assert_called_once_with("POST", "/completion", body_bytes)

    def test_handler_tries_second_candidate_when_first_is_incompatible(self):
        """When the newest KV is incompatible but second-newest is compatible, restore should use the second."""
        with tempfile.TemporaryDirectory() as cache_dir:
            # Create two entries — first (newer) has incompatible model_hash
            kv1_path, _ = self._create_cache_entry(cache_dir,
                prompt="hello world", meta={"model_hash": "wrong", "context_size": 4096}, slot_id=0, timestamp=1715000002)
            # Second (older) has compatible model_hash
            kv2_path, _ = self._create_cache_entry(cache_dir,
                prompt="hello world", meta={"model_hash": "abc123", "context_size": 4096}, slot_id=0, timestamp=1715000001)
            cache = lmcache.KVCache(cache_dir)
            handler, body_bytes = self._make_handler({"prompt": "hello world"})
            handler._get_available_slot = mock.Mock(return_value=0)
            handler._forward = mock.Mock(return_value=None)

            with mock.patch.object(lmcache.LMCacheHandler, "server_model_info", {"model_hash": "abc123", "context_size": 4096}), \
                 mock.patch.object(lmcache.LMCacheHandler, "cache_dir_obj", cache), \
                 mock.patch.object(lmcache, "_restore_slot", return_value=True) as restore:
                handler._handle_request("POST")

            # First candidate is incompatible (model_hash mismatch), second is compatible.
            # _restore_slot is only called when compatibility check passes, so it should be called once.
            self.assertEqual(restore.call_count, 1)
            restore.assert_called_with(0, str(kv2_path), "localhost", 8081)
            handler._forward.assert_called_once_with("POST", "/completion", body_bytes)

    def test_get_server_model_info(self):
        class MockResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({
                    "model": {
                        "path": "/path/to/model.gguf",
                        "ctx_size": 4096,
                    }
                }).encode()

        with mock.patch.object(lmcache.urllib.request, "urlopen", return_value=MockResponse()):
            result = lmcache._get_server_model_info("localhost", 8081)

        self.assertIsNotNone(result)
        self.assertEqual(result["model_hash"], hashlib.sha256(b"/path/to/model.gguf").hexdigest()[:16])
        self.assertEqual(result["context_size"], 4096)

    def test_full_flow_restores_kv_and_forwards(self):
        """Full flow: find match, restore KV into slot, forward to llama.cpp."""
        with tempfile.TemporaryDirectory() as cache_dir:
            meta = {"model_hash": "abc123", "context_size": 4096}
            kv_path, _ = self._create_cache_entry(cache_dir, meta=meta)
            cache = lmcache.KVCache(cache_dir)
            handler, body_bytes = self._make_handler({"prompt": "hello world"})
            handler._get_available_slot = mock.Mock(return_value=0)
            handler._forward = mock.Mock(return_value=None)

            with mock.patch.object(lmcache.LMCacheHandler, "server_model_info", {"model_hash": "abc123", "context_size": 4096}), \
                 mock.patch.object(lmcache.LMCacheHandler, "cache_dir_obj", cache), \
                 mock.patch.object(lmcache, "_restore_slot", return_value=True) as restore:
                handler._handle_request("POST")

            restore.assert_called_once_with(0, str(kv_path), "localhost", 8081)
            handler._forward.assert_called_once_with("POST", "/completion", body_bytes)

    def test_get_server_model_info_missing_fields(self):
        """When health endpoint returns model info without path/ctx_size,
        _get_server_model_info should still return a valid dict (not None)."""
        class MockResponse:
            status = 200

            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False

            def read(self):
                return json.dumps({
                    "model": {},
                }).encode()

        with mock.patch.object(lmcache.urllib.request, "urlopen", return_value=MockResponse()):
            result = lmcache._get_server_model_info("localhost", 8081)

        self.assertIsNotNone(result)
        self.assertEqual(result["context_size"], 0)

    def test_get_server_model_info_missing_endpoint(self):
        """When health endpoint raises, _get_server_model_info returns None."""
        with mock.patch.object(lmcache.urllib.request, "urlopen", side_effect=Exception("timeout")):
            result = lmcache._get_server_model_info("localhost", 8081)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
