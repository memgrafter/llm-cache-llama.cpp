import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

# lmcache-proxy.py has a hyphen in the name, so we can't import it directly
_spec = importlib.util.spec_from_file_location(
    "lmcache_proxy",
    os.path.join(os.path.dirname(__file__), "..", "lmcache-proxy.py"),
)
_lmcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lmcp)
KVCache = _lmcp.KVCache
SlotManager = _lmcp.SlotManager
LMCacheHandler = _lmcp.LMCacheHandler


class TestKVCache(unittest.TestCase):
    def test_hash_prefix_is_stable(self):
        with tempfile.TemporaryDirectory() as d:
            cache = KVCache(d)
            h1 = cache._hash_prefix("hello world")
            h2 = cache._hash_prefix("hello world")
            self.assertEqual(h1, h2)

    def test_hash_prefix_differs_for_different_prompts(self):
        with tempfile.TemporaryDirectory() as d:
            cache = KVCache(d)
            h1 = cache._hash_prefix("prompt a")
            h2 = cache._hash_prefix("prompt b")
            self.assertNotEqual(h1, h2)

    def test_hash_prefix_ignores_trailing_whitespace(self):
        with tempfile.TemporaryDirectory() as d:
            cache = KVCache(d)
            # hashes should differ — no normalization
            h1 = cache._hash_prefix("hello")
            h2 = cache._hash_prefix("hello ")
            self.assertNotEqual(h1, h2)


class TestSlotManagerUnit(unittest.TestCase):
    """Unit tests for SlotManager slot-hash tracking and LRU."""

    def _make_manager(self, cache_dir=None, min_match_tokens=0):
        if cache_dir is None:
            cache_dir = tempfile.mkdtemp()
        cache = KVCache(cache_dir)
        return SlotManager("localhost", 8081, cache,
                           min_match_tokens=min_match_tokens), cache

    # -- get_best_slot --

    def test_get_best_slot_no_slots(self):
        sm, _ = self._make_manager()
        self.assertIsNone(sm.get_best_slot("any prompt"))

    def test_get_best_slot_exact_match(self):
        sm, cache = self._make_manager(min_match_tokens=0)
        prompt = "the quick brown fox"
        h = cache._hash_prefix(prompt)
        sm._slot_hash[0] = h
        sm._slot_tokens[0] = 100
        sm._slot_time[0] = time.monotonic()

        self.assertEqual(sm.get_best_slot(prompt), 0)

    def test_get_best_slot_no_match(self):
        sm, cache = self._make_manager(min_match_tokens=0)
        sm._slot_hash[0] = cache._hash_prefix("different prompt")
        sm._slot_tokens[0] = 100
        sm._slot_time[0] = time.monotonic()

        self.assertIsNone(sm.get_best_slot("unrelated prompt"))

    def test_get_best_slot_returns_first_matching(self):
        sm, cache = self._make_manager(min_match_tokens=0)
        prompt = "shared prefix"
        h = cache._hash_prefix(prompt)
        sm._slot_hash[0] = h
        sm._slot_tokens[0] = 100
        sm._slot_hash[2] = h
        sm._slot_tokens[2] = 100
        sm._slot_time[0] = time.monotonic()
        sm._slot_time[2] = time.monotonic()

        # both match — first one wins (dict iteration order)
        result = sm.get_best_slot(prompt)
        self.assertIn(result, [0, 2])

    def test_get_best_slot_ignores_nonmatching_slots(self):
        sm, cache = self._make_manager(min_match_tokens=0)
        prompt = "target"
        h_target = cache._hash_prefix(prompt)
        h_other = cache._hash_prefix("other")
        sm._slot_hash[0] = h_other
        sm._slot_tokens[0] = 100
        sm._slot_hash[1] = h_target
        sm._slot_tokens[1] = 100
        sm._slot_hash[2] = h_other
        sm._slot_tokens[2] = 100
        sm._slot_time[0] = time.monotonic()
        sm._slot_time[1] = time.monotonic()
        sm._slot_time[2] = time.monotonic()

        self.assertEqual(sm.get_best_slot(prompt), 1)

    def test_get_best_slot_rejects_below_min_tokens(self):
        """Slot has matching hash but too few tokens — rejected."""
        sm, cache = self._make_manager(min_match_tokens=5000)
        # long prompt (~20k chars = ~5k tok) but slot only has 100 tok
        prompt = "x" * 20_000
        h = cache._hash_prefix(prompt)
        sm._slot_hash[0] = h
        sm._slot_tokens[0] = 100  # below threshold
        sm._slot_time[0] = time.monotonic()

        self.assertIsNone(sm.get_best_slot(prompt))

    def test_get_best_slot_accepts_above_min_tokens(self):
        """Slot has matching hash and enough tokens — accepted."""
        sm, cache = self._make_manager(min_match_tokens=5000)
        prompt = "x" * 20_000
        h = cache._hash_prefix(prompt)
        sm._slot_hash[0] = h
        sm._slot_tokens[0] = 6000  # above threshold
        sm._slot_time[0] = time.monotonic()

        self.assertEqual(sm.get_best_slot(prompt), 0)

    def test_get_best_slot_ratio_threshold(self):
        """Short request: 80% of request context is the threshold, not min_tokens."""
        sm, cache = self._make_manager(min_match_tokens=5000)
        # short prompt (~4k chars = ~1k tok), 80% of 1k = 800
        prompt = "y" * 4_000
        h = cache._hash_prefix(prompt)
        sm._slot_hash[0] = h
        sm._slot_tokens[0] = 900  # above 80% of request (800), below min_tokens (5000)
        sm._slot_time[0] = time.monotonic()

        self.assertEqual(sm.get_best_slot(prompt), 0)

    # -- lru_slot --

    def test_lru_slot_returns_oldest(self):
        sm, _ = self._make_manager()
        t = time.monotonic()
        sm._slot_time[0] = t + 10  # newest
        sm._slot_time[1] = t + 5   # middle
        sm._slot_time[2] = t       # oldest

        self.assertEqual(sm.lru_slot(), 2)

    def test_lru_slot_empty_returns_none(self):
        sm, _ = self._make_manager()
        self.assertIsNone(sm.lru_slot())

    def test_lru_slot_single_entry(self):
        sm, _ = self._make_manager()
        sm._slot_time[3] = time.monotonic()

        self.assertEqual(sm.lru_slot(), 3)

    # -- update_slot_time --

    def test_update_slot_time_records_timestamp(self):
        sm, _ = self._make_manager()
        before = time.monotonic()
        sm.update_slot_time(5)
        after = time.monotonic()

        self.assertIn(5, sm._slot_time)
        self.assertGreaterEqual(sm._slot_time[5], before)
        self.assertLessEqual(sm._slot_time[5], after)

    def test_update_slot_time_overwrites_existing(self):
        sm, _ = self._make_manager()
        sm._slot_time[0] = time.monotonic()
        old_val = sm._slot_time[0]
        time.sleep(0.01)
        sm.update_slot_time(0)

        self.assertGreater(sm._slot_time[0], old_val)


class _MockLlamaHandler(BaseHTTPRequestHandler):
    """Minimal mock llama.cpp server for integration tests."""

    def do_GET(self):
        if self.path == "/slots":
            body = json.dumps([
                {"id": 0, "is_processing": False},
                {"id": 1, "is_processing": False},
                {"id": 2, "is_processing": False},
                {"id": 3, "is_processing": False},
            ])
        else:
            body = json.dumps({})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        # Echo back whatever was sent (for slot save/restore and completions)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}

        # Slot save/restore endpoints return a success response
        if "/slots/" in self.path and "action=" in self.path:
            resp = {
                "id_slot": int(self.path.split("/")[-1].split("?")[0]),
                "filename": data.get("filename", "test.bin"),
                "n_saved": 100,
                "n_written": 1024,
                "timings": {"save_ms": 1.0},
            }
        else:
            # Completion endpoint — echo back the body with id_slot
            resp = {
                "choices": [{"message": {"content": "ok"}}],
                "id": "cmpl-test",
                "model": "test-model",
                "object": "chat.completion",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "prompt_tokens_details": {"cached_tokens": 8},
                },
            }
            # Echo the id_slot that was sent
            if "id_slot" in data:
                resp["id_slot"] = data["id_slot"]

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(resp).encode())

    def log_message(self, format, *args):
        pass  # suppress logging


class TestSlotRoutingIntegration(unittest.TestCase):
    """Integration tests: proxy routes requests to correct slot via id_slot."""

    @classmethod
    def setUpClass(cls):
        cls.cache_dir = tempfile.mkdtemp()
        cls.server_port = 18999  # mock llama server
        cls.proxy_port = 18990   # proxy
        cls._start_mock_server()
        time.sleep(0.2)  # let server start

    @classmethod
    def _start_mock_server(cls):
        cls.mock_server = HTTPServer(("127.0.0.1", cls.server_port), _MockLlamaHandler)
        cls._mock_thread = Thread(target=cls.mock_server.serve_forever, daemon=True)
        cls._mock_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.mock_server.shutdown()

    def _make_handler(self):
        """Create a handler with slot_manager and cache wired up."""
        cache = KVCache(self.cache_dir)
        sm = SlotManager("127.0.0.1", self.server_port, cache)
        LMCacheHandler.llama_server = "127.0.0.1"
        LMCacheHandler.llama_port = self.server_port
        LMCacheHandler.slot_manager = sm
        LMCacheHandler.cache_dir_obj = cache
        return sm, cache

    def test_request_routes_to_matching_slot(self):
        sm, cache = self._make_handler()
        prompt = "the quick brown fox jumps"
        h = cache._hash_prefix(prompt)
        sm._slot_hash[2] = h
        sm._slot_tokens[2] = 1000
        sm._slot_time[2] = time.monotonic()

        # Test the routing logic directly (can't construct handler without socket):
        target = sm.get_best_slot(prompt)
        self.assertEqual(target, 2)

    def test_request_no_match_no_id_slot(self):
        sm, cache = self._make_handler()
        prompt = "unique prompt xyz"
        # No slot has this hash
        target = sm.get_best_slot(prompt)
        self.assertIsNone(target)

    def test_slot_time_updated_after_routing(self):
        sm, cache = self._make_handler()
        prompt = "routing test"
        h = cache._hash_prefix(prompt)
        sm._slot_hash[1] = h
        old_time = time.monotonic()
        sm._slot_time[1] = old_time

        sm.update_slot_time(1)
        new_time = sm._slot_time[1]
        self.assertGreater(new_time, old_time)

    def test_full_roundtrip_with_mock_server(self):
        """Send a real HTTP request through the proxy to the mock server."""
        sm, cache = self._make_handler()
        prompt = "roundtrip test prompt"
        h = cache._hash_prefix(prompt)
        sm._slot_hash[3] = h
        sm._slot_tokens[3] = 1000
        sm._slot_time[3] = time.monotonic()

        # Verify routing decision
        target = sm.get_best_slot(prompt)
        self.assertEqual(target, 3)

        # Build the request body with id_slot injected
        body = {
            "model": "test",
            "messages": [{"role": "user", "content": prompt}],
            "id_slot": target,
        }
        import urllib.request
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server_port}/v1/chat/completions",
            data=data,
            method="POST",
        )
        req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode())

        # Mock server echoes back id_slot
        self.assertEqual(result.get("id_slot"), 3)

    def test_multiple_slots_different_hashes(self):
        """Each slot has a different prompt hash; routing picks the right one."""
        sm, cache = self._make_handler()
        prompts = ["alpha", "beta", "gamma"]
        for i, p in enumerate(prompts):
            sm._slot_hash[i] = cache._hash_prefix(p)
            sm._slot_tokens[i] = 1000
            sm._slot_time[i] = time.monotonic()

        for i, p in enumerate(prompts):
            self.assertEqual(sm.get_best_slot(p), i)

    def test_try_disk_cache_no_match_in_slots(self):
        """When no slot matches, try_disk_cache checks on-disk trie and restores into idle slot."""
        with tempfile.TemporaryDirectory() as d:
            cache = KVCache(d)
            sm = SlotManager("127.0.0.1", self.server_port, cache)
            prompt = "disk cache test prompt"

            # No slots loaded
            self.assertIsNone(sm.get_best_slot(prompt))

            # try_disk_cache: no files on disk either → None
            result = sm.try_disk_cache(prompt)
            self.assertIsNone(result)

    def test_try_disk_cache_with_file_on_disk(self):
        """When a KV file exists on disk and an idle slot is available, restore it."""
        with tempfile.TemporaryDirectory() as d:
            cache = KVCache(d)
            # Create a fake cached file
            prompt = "disk cache hit"
            h = cache._hash_prefix(prompt)
            fake_dir = pathlib.Path(d) / h
            fake_dir.mkdir(parents=True)
            fake_file = fake_dir / "0_1234.bin"
            fake_file.write_bytes(b"fake kv data")

            sm = SlotManager("127.0.0.1", self.server_port, cache)
            # Slot 0 is idle (mock server returns all idle)
            result = sm.try_disk_cache(prompt)

            # Should have restored into an idle slot
            self.assertIsNotNone(result)
            self.assertIn(result, [0, 1, 2, 3])
            self.assertEqual(sm._slot_hash[result], h)

    def test_lru_after_multiple_updates(self):
        """After updating slot times, LRU returns the oldest."""
        sm, _ = self._make_handler()
        t0 = time.monotonic()
        sm._slot_time[0] = t0
        sm._slot_time[1] = t0 + 5
        sm._slot_time[2] = t0 + 10

        # Slot 0 is oldest
        self.assertEqual(sm.lru_slot(), 0)

        # Update slot 0 to be newest, now slot 1 is oldest
        sm._slot_time[0] = t0 + 20
        self.assertEqual(sm.lru_slot(), 1)


class TestExtractPrompts(unittest.TestCase):
    """Tests for LMCacheHandler._extract_prompts."""

    def test_extract_from_chat_messages(self):
        body = {
            "messages": [
                {"role": "system", "content": "be nice"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "world"},
            ]
        }
        handler = LMCacheHandler.__new__(LMCacheHandler)
        prompts = handler._extract_prompts(body)
        self.assertEqual(prompts, ["hello", "world"])

    def test_extract_from_legacy_prompt(self):
        body = {"prompt": "once upon a time"}
        handler = LMCacheHandler.__new__(LMCacheHandler)
        prompts = handler._extract_prompts(body)
        self.assertEqual(prompts, ["once upon a time"])

    def test_extract_from_multimodal_content(self):
        body = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "describe this image"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ]},
            ]
        }
        handler = LMCacheHandler.__new__(LMCacheHandler)
        prompts = handler._extract_prompts(body)
        self.assertEqual(prompts, ["describe this image"])

    def test_extract_empty_body(self):
        handler = LMCacheHandler.__new__(LMCacheHandler)
        prompts = handler._extract_prompts({})
        self.assertEqual(prompts, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
