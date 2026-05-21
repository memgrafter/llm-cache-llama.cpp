import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

import prefix_cache


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

    def test_prefix_cache_restores_strict_prefix_and_autosaves_stream_result(self):
        with tempfile.TemporaryDirectory() as cache_dir_str:
            cache_dir = pathlib.Path(cache_dir_str)
            cache = prefix_cache.PrefixCache(cache_dir)
            cache.init()

            prefix_text = "hello world"
            request_prompt = "hello world suffix"
            generated = " generated"
            prefix_tokens = list(prefix_text.encode("utf-8"))
            parent_id, digest = prefix_cache.node_id_for(prefix_tokens)
            parent_bin = cache.relative_node_bin(parent_id)
            cache.absolute_bin_path(parent_bin).write_bytes(b"parent")
            cache.insert_node({
                "id": parent_id,
                "parent_id": None,
                "label": "parent",
                "boundary": "manual",
                "token_count": len(prefix_tokens),
                "prefix_hash": digest,
                "hash_algo": prefix_cache.HASH_ALGO,
                "bin_file": parent_bin,
                "size_bytes": 6,
                "n_saved": len(prefix_tokens),
                "created_at": prefix_cache.utc_now(),
            })

            handler, body_bytes = self._make_handler({"prompt": request_prompt, "stream": True})
            handler._forward = mock.Mock(return_value=lmcache.ForwardResult(
                200,
                "text/event-stream",
                b'data: {"choices":[{"delta":{"content":" generated"}}]}\n\ndata: [DONE]\n\n',
            ))
            handler.prefix_cache_obj = cache
            handler.cache_dir_obj = None
            handler.auto_save_enabled = True
            handler.prefix_cache_enabled = True
            handler.min_save_tokens = 1
            handler.max_cache_bytes = 2 * lmcache.GIB
            handler.min_free_bytes = 1
            handler.strict_prefix_restore = True
            handler.slot_id = 0

            def fake_call(method, path, body=None, server="localhost", port=8081, timeout=30):
                if path == "/tokenize":
                    return {"tokens": list(body["content"].encode("utf-8"))}
                if path == "/props":
                    return {"model_alias": "mock", "model_path": "/tmp/mock.gguf", "default_generation_settings": {"n_ctx": 4096}}
                raise AssertionError((method, path, body))

            def fake_save(slot_id, bin_file, server="localhost", port=8081):
                saved_tokens = list((request_prompt + generated).encode("utf-8"))
                cache.absolute_bin_path(bin_file).write_bytes(b"saved")
                return {"n_saved": len(saved_tokens), "filename": bin_file}

            with mock.patch.object(lmcache, "_call_llama", side_effect=fake_call), \
                 mock.patch.object(lmcache, "_restore_slot", return_value={"n_restored": len(prefix_tokens)}) as restore, \
                 mock.patch.object(lmcache, "_save_slot", side_effect=fake_save) as save:
                handler._handle_request("POST")

            restore.assert_called_once_with(0, parent_bin, "localhost", 8081)
            handler._forward.assert_called_once_with("POST", "/completion", body_bytes)
            save.assert_called_once()

            saved_tokens = list(request_prompt.encode("utf-8"))
            saved_id, _ = prefix_cache.node_id_for(saved_tokens)
            saved_node = cache.get_node(saved_id)
            self.assertIsNotNone(saved_node)
            self.assertEqual(saved_node["parent_id"], parent_id)
            self.assertEqual(saved_node["token_count"], len(saved_tokens))
            self.assertGreater(saved_node["n_saved"], saved_node["token_count"])

    def test_chat_completions_autosave_keys_known_rendered_prompt(self):
        with tempfile.TemporaryDirectory() as cache_dir_str:
            cache = prefix_cache.PrefixCache(pathlib.Path(cache_dir_str))
            cache.init()
            body = {
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"type": "function", "function": {"name": "noop", "parameters": {"type": "object"}}}],
                "tool_choice": "auto",
                "stream": True,
            }
            rendered = "<|im_start|>system\nshared instructions<|im_end|>\n<|im_start|>user\nhello<|im_end|>\n<|im_start|>assistant\n<think>\n"
            rendered_tokens = list(rendered.encode("utf-8"))
            handler, body_bytes = self._make_handler(body, path="/v1/chat/completions")
            handler._forward = mock.Mock(return_value=lmcache.ForwardResult(
                200,
                "text/event-stream",
                b'data: {"choices":[{"delta":{"reasoning_content":"hidden"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"shown"}}]}\n\n'
                b'data: [DONE]\n\n',
            ))
            handler.prefix_cache_obj = cache
            handler.cache_dir_obj = None
            handler.auto_save_enabled = True
            handler.prefix_cache_enabled = True
            handler.min_save_tokens = 1
            handler.max_cache_bytes = 2 * lmcache.GIB
            handler.min_free_bytes = 1
            handler.strict_prefix_restore = True
            handler.slot_id = 0
            handler.anchor_configs = [lmcache.AnchorConfig("end-of-system-message", "<|im_end|>", 1, "after", True)]

            anchor_tokens = list("<|im_start|>system\nshared instructions<|im_end|>".encode("utf-8"))

            def fake_call(method, path, body=None, server="localhost", port=8081, timeout=30):
                if path == "/apply-template":
                    self.assertIn("tools", body)
                    self.assertIn("tool_choice", body)
                    return {"prompt": rendered}
                if path == "/tokenize":
                    return {"tokens": list(body["content"].encode("utf-8"))}
                if path == "/completion":
                    self.assertEqual(body["prompt"], "<|im_start|>system\nshared instructions<|im_end|>")
                    self.assertEqual(body["n_predict"], 0)
                    return {"content": ""}
                if path == "/slots/0?action=erase":
                    return {"n_erased": 0}
                if path == "/props":
                    return {"model_alias": "mock", "model_path": "/tmp/mock.gguf", "default_generation_settings": {"n_ctx": 4096}}
                raise AssertionError((method, path, body))

            def fake_save(slot_id, bin_file, server="localhost", port=8081):
                cache.absolute_bin_path(bin_file).write_bytes(b"saved-chat")
                if bin_file.startswith("prefix_anchor_tmp_"):
                    return {"n_saved": len(anchor_tokens), "filename": bin_file}
                return {"n_saved": len(rendered_tokens) + 99, "filename": bin_file}

            with mock.patch.object(lmcache, "_call_llama", side_effect=fake_call), \
                 mock.patch.object(lmcache, "_restore_slot", return_value=None), \
                 mock.patch.object(lmcache, "_save_slot", side_effect=fake_save):
                handler._handle_request("POST")

            handler._forward.assert_called_once_with("POST", "/v1/chat/completions", body_bytes)
            node_id, _ = prefix_cache.node_id_for(rendered_tokens)
            node = cache.get_node(node_id)
            self.assertIsNotNone(node)
            self.assertEqual(node["token_count"], len(rendered_tokens))
            self.assertEqual(node["n_saved"], len(rendered_tokens) + 99)

            anchor_match = cache.lookup_materialized_anchor(label="end-of-system-message", tokens=anchor_tokens)
            self.assertIsNotNone(anchor_match)
            self.assertEqual(anchor_match["boundary"], "anchor")
            self.assertEqual(anchor_match["token_count"], len(anchor_tokens))

    def test_prefix_cache_anchor_materializes_once_after_full_prefix_miss(self):
        with tempfile.TemporaryDirectory() as cache_dir_str:
            cache = prefix_cache.PrefixCache(pathlib.Path(cache_dir_str))
            cache.init()
            saved_prompt = "<|im_start|>system\nshared<|im_end|>\n<|im_start|>user\nold<|im_end|>\n"
            incoming_prompt = "<|im_start|>system\nshared<|im_end|>\n<|im_start|>user\nnew<|im_end|>\n"
            saved_tokens = list(saved_prompt.encode("utf-8"))
            node_id, digest = prefix_cache.node_id_for(saved_tokens)
            bin_file = cache.relative_node_bin(node_id)
            cache.absolute_bin_path(bin_file).write_bytes(b"full")
            cache.insert_node({
                "id": node_id,
                "parent_id": None,
                "label": "full-node",
                "boundary": "auto-response",
                "token_count": len(saved_tokens),
                "prefix_hash": digest,
                "hash_algo": prefix_cache.HASH_ALGO,
                "bin_file": bin_file,
                "size_bytes": 4,
                "n_saved": len(saved_tokens),
                "created_at": prefix_cache.utc_now(),
            })
            anchor_text = "<|im_start|>system\nshared<|im_end|>"
            anchor_tokens = list(anchor_text.encode("utf-8"))
            cache.insert_anchor({
                "node_id": node_id,
                "label": "end-of-system-message",
                "token_count": len(anchor_tokens),
                "prefix_hash": prefix_cache.hash_tokens(anchor_tokens),
                "marker": "<|im_end|>",
                "occurrence": 1,
                "side": "after",
                "pinned": True,
                "created_at": prefix_cache.utc_now(),
            })

            handler, body_bytes = self._make_handler({"messages": [{"role": "user", "content": "new"}]}, path="/v1/chat/completions")
            handler._forward = mock.Mock(return_value=lmcache.ForwardResult(200, "text/event-stream", b""))
            handler.prefix_cache_obj = cache
            handler.cache_dir_obj = None
            handler.auto_save_enabled = False
            handler.prefix_cache_enabled = True
            handler.strict_prefix_restore = True
            handler.slot_id = 0
            handler.anchor_configs = [lmcache.AnchorConfig("end-of-system-message", "<|im_end|>", 1, "after", True)]

            def fake_call(method, path, body=None, server="localhost", port=8081, timeout=30):
                if path == "/apply-template":
                    return {"prompt": incoming_prompt}
                if path == "/tokenize":
                    return {"tokens": list(body["content"].encode("utf-8"))}
                if path == "/completion":
                    self.assertEqual(body["prompt"], anchor_text)
                    return {"content": ""}
                if path == "/slots/0?action=erase":
                    return {"n_erased": len(saved_tokens)}
                if path == "/props":
                    return {"model_alias": "mock", "model_path": "/tmp/mock.gguf", "default_generation_settings": {"n_ctx": 4096}}
                raise AssertionError((method, path, body))

            def fake_save(slot_id, save_file, server="localhost", port=8081):
                cache.absolute_bin_path(save_file).write_bytes(b"anchor")
                return {"n_saved": len(anchor_tokens), "filename": save_file}

            with mock.patch.object(lmcache, "_call_llama", side_effect=fake_call), \
                 mock.patch.object(lmcache, "_restore_slot", return_value={"n_restored": len(saved_tokens)}) as restore, \
                 mock.patch.object(lmcache, "_save_slot", side_effect=fake_save) as save:
                handler._handle_request("POST")

            restore.assert_not_called()
            save.assert_called_once()
            materialized = cache.lookup_materialized_anchor(label="end-of-system-message", tokens=anchor_tokens)
            self.assertIsNotNone(materialized)
            self.assertEqual(materialized["boundary"], "anchor")
            self.assertEqual(materialized["token_count"], len(anchor_tokens))
            handler._forward.assert_called_once_with("POST", "/v1/chat/completions", body_bytes)

    def test_prefix_cache_restores_existing_materialized_anchor(self):
        with tempfile.TemporaryDirectory() as cache_dir_str:
            cache = prefix_cache.PrefixCache(pathlib.Path(cache_dir_str))
            cache.init()
            anchor_text = "<|im_start|>system\nshared<|im_end|>"
            incoming_prompt = anchor_text + "\n<|im_start|>user\nnew<|im_end|>\n"
            anchor_tokens = list(anchor_text.encode("utf-8"))
            node_id, digest = prefix_cache.anchor_node_id_for("end-of-system-message", anchor_tokens)
            bin_file = cache.relative_node_bin(node_id)
            cache.absolute_bin_path(bin_file).write_bytes(b"anchor")
            cache.insert_node({
                "id": node_id,
                "parent_id": None,
                "label": "end-of-system-message",
                "boundary": "anchor",
                "token_count": len(anchor_tokens),
                "prefix_hash": digest,
                "hash_algo": prefix_cache.HASH_ALGO,
                "bin_file": bin_file,
                "size_bytes": 6,
                "n_saved": len(anchor_tokens),
                "created_at": prefix_cache.utc_now(),
                "pinned": True,
            })
            cache.insert_anchor({
                "node_id": node_id,
                "label": "end-of-system-message",
                "token_count": len(anchor_tokens),
                "prefix_hash": digest,
                "marker": "<|im_end|>",
                "occurrence": 1,
                "side": "after",
                "pinned": True,
                "created_at": prefix_cache.utc_now(),
            })

            handler, body_bytes = self._make_handler({"messages": [{"role": "user", "content": "new"}]}, path="/v1/chat/completions")
            handler._forward = mock.Mock(return_value=lmcache.ForwardResult(200, "text/event-stream", b""))
            handler.prefix_cache_obj = cache
            handler.cache_dir_obj = None
            handler.auto_save_enabled = False
            handler.prefix_cache_enabled = True
            handler.strict_prefix_restore = True
            handler.slot_id = 0
            handler.anchor_configs = [lmcache.AnchorConfig("end-of-system-message", "<|im_end|>", 1, "after", True)]

            def fake_call(method, path, body=None, server="localhost", port=8081, timeout=30):
                if path == "/apply-template":
                    return {"prompt": incoming_prompt}
                if path == "/tokenize":
                    return {"tokens": list(body["content"].encode("utf-8"))}
                raise AssertionError((method, path, body))

            with mock.patch.object(lmcache, "_call_llama", side_effect=fake_call), \
                 mock.patch.object(lmcache, "_restore_slot", return_value={"n_restored": len(anchor_tokens)}) as restore, \
                 mock.patch.object(lmcache, "_save_slot") as save:
                handler._handle_request("POST")

            restore.assert_called_once_with(0, bin_file, "localhost", 8081)
            save.assert_not_called()
            handler._forward.assert_called_once_with("POST", "/v1/chat/completions", body_bytes)

    def test_prefix_cache_does_not_restore_exact_match_by_default(self):
        with tempfile.TemporaryDirectory() as cache_dir_str:
            cache = prefix_cache.PrefixCache(pathlib.Path(cache_dir_str))
            cache.init()
            prompt = "exact prompt"
            tokens = list(prompt.encode("utf-8"))
            node_id, digest = prefix_cache.node_id_for(tokens)
            bin_file = cache.relative_node_bin(node_id)
            cache.absolute_bin_path(bin_file).write_bytes(b"exact")
            cache.insert_node({
                "id": node_id,
                "parent_id": None,
                "label": "exact",
                "boundary": "manual",
                "token_count": len(tokens),
                "prefix_hash": digest,
                "hash_algo": prefix_cache.HASH_ALGO,
                "bin_file": bin_file,
                "size_bytes": 5,
                "n_saved": len(tokens),
                "created_at": prefix_cache.utc_now(),
            })

            handler, body_bytes = self._make_handler({"prompt": prompt})
            handler._forward = mock.Mock(return_value=lmcache.ForwardResult(200, "application/json", b'{"content":""}'))
            handler.prefix_cache_obj = cache
            handler.cache_dir_obj = None
            handler.auto_save_enabled = False
            handler.prefix_cache_enabled = True
            handler.strict_prefix_restore = True

            def fake_call(method, path, body=None, server="localhost", port=8081, timeout=30):
                if path == "/tokenize":
                    return {"tokens": list(body["content"].encode("utf-8"))}
                raise AssertionError((method, path, body))

            with mock.patch.object(lmcache, "_call_llama", side_effect=fake_call), \
                 mock.patch.object(lmcache, "_restore_slot", return_value={"n_restored": len(tokens)}) as restore:
                handler._handle_request("POST")

            restore.assert_not_called()
            handler._forward.assert_called_once_with("POST", "/completion", body_bytes)

    def test_prefix_cache_low_storage_skips_autosave_gracefully(self):
        with tempfile.TemporaryDirectory() as cache_dir_str:
            cache = prefix_cache.PrefixCache(pathlib.Path(cache_dir_str))
            cache.init()
            handler, _ = self._make_handler({"prompt": "short", "stream": True})
            handler.prefix_cache_obj = cache
            handler.auto_save_enabled = True
            handler.min_save_tokens = 1
            handler.min_free_bytes = 999999999999
            handler.max_cache_bytes = 2 * lmcache.GIB
            ctx = lmcache.RequestCacheContext("short", list(b"short"), [])
            result = lmcache.ForwardResult(
                200,
                "text/event-stream",
                b'data: {"choices":[{"delta":{"content":" out"}}]}\n\ndata: [DONE]\n\n',
            )

            def fake_call(method, path, body=None, server="localhost", port=8081, timeout=30):
                if path == "/tokenize":
                    return {"tokens": list(body["content"].encode("utf-8"))}
                raise AssertionError((method, path, body))

            usage = shutil._ntuple_diskusage(total=1000, used=999, free=1)
            with mock.patch.object(lmcache, "_call_llama", side_effect=fake_call), \
                 mock.patch.object(lmcache.shutil, "disk_usage", return_value=usage), \
                 mock.patch.object(lmcache, "_save_slot") as save:
                handler._auto_save_prefix_cache(ctx, {"prompt": "short", "stream": True}, result)

            save.assert_not_called()
            self.assertEqual(cache.list_nodes(), [])


@unittest.skipUnless(
    os.environ.get("RUN_LIVE_PROXY_CHAT_CACHE_TESTS") == "1",
    "set RUN_LIVE_PROXY_CHAT_CACHE_TESTS=1 to run live proxy chat-cache test",
)
class LMCacheProxyLiveChatTests(unittest.TestCase):
    def setUp(self):
        self.base_url = os.environ.get("PREFIX_CACHE_LIVE_BASE_URL", "http://127.0.0.1:8081")
        self.cache_dir = pathlib.Path(
            os.environ.get("PREFIX_CACHE_LIVE_CACHE_DIR", str(prefix_cache.DEFAULT_CACHE_DIR))
        ).expanduser()
        try:
            with urllib.request.urlopen(self.base_url + "/health", timeout=5) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            raise unittest.SkipTest(f"live proxy unavailable: {e}")
        if data.get("status") != "ok":
            raise unittest.SkipTest(f"live proxy unhealthy: {data!r}")
        self.created_node_ids = []

    def tearDown(self):
        cache = prefix_cache.PrefixCache(self.cache_dir)
        for node_id in reversed(self.created_node_ids):
            node = cache.get_node(node_id)
            if not node:
                continue
            try:
                cache.absolute_bin_path(node["bin_file"]).unlink()
            except FileNotFoundError:
                pass
            db = cache.connect()
            try:
                db.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
                db.commit()
            finally:
                db.close()

    def post_json(self, path, body, *, timeout=120):
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-LMCache-Bypass": "1"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def test_live_chat_completions_autosaves_rendered_prompt_node(self):
        marker = f"LIVE-CHAT-AUTOCACHE-{time.time_ns()}"
        body = {
            "messages": [{"role": "user", "content": marker + " " + ("cacheword " * 320)}],
            "stream": True,
            "max_tokens": 2,
            "temperature": 0.0,
        }
        rendered = self.post_json("/apply-template", {"messages": body["messages"]})["prompt"]
        tokens = self.post_json("/tokenize", {"content": rendered})["tokens"]
        node_id, _ = prefix_cache.node_id_for([int(t) for t in tokens])
        self.created_node_ids.append(node_id)

        req = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            for _ in resp:
                pass

        # Autosave runs after stream completion in the same handler; allow filesystem settle.
        time.sleep(0.5)
        node = prefix_cache.PrefixCache(self.cache_dir).get_node(node_id)
        self.assertIsNotNone(node)
        self.assertEqual(node["token_count"], len(tokens))
        self.assertGreaterEqual(node["n_saved"], node["token_count"])
        self.assertTrue((self.cache_dir / node["bin_file"]).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
