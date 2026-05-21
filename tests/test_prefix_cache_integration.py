import contextlib
import io
import json
import os
import pathlib
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import prefix_cache


class MockLlamaHandler(BaseHTTPRequestHandler):
    cache_dir = None
    slot_tokens = []
    model_alias = "mock-qwen"
    model_path = "/tmp/mock.gguf"
    ctx_size = 32768

    def log_message(self, fmt, *args):
        return

    @staticmethod
    def tokenize_text(text):
        return list(text.encode("utf-8"))

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if n == 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _write_json(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file_path(self, filename):
        p = pathlib.Path(filename)
        if p.is_absolute():
            return p
        return pathlib.Path(self.cache_dir) / p

    def do_GET(self):
        if self.path == "/health":
            self._write_json({"status": "ok"})
            return
        if self.path == "/props":
            self._write_json({
                "model_alias": self.model_alias,
                "model_path": self.model_path,
                "default_generation_settings": {"n_ctx": self.ctx_size},
            })
            return
        self._write_json({"error": "not found"}, status=404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        body = self._read_json()

        if parsed.path == "/tokenize":
            self._write_json({"tokens": self.tokenize_text(body.get("content", ""))})
            return

        if parsed.path == "/completion":
            type(self).slot_tokens = self.tokenize_text(body.get("prompt", ""))
            self._write_json({
                "content": "",
                "timings": {
                    "prompt_n": len(type(self).slot_tokens),
                    "cache_n": 0,
                    "predicted_n": 0,
                },
            })
            return

        if parsed.path.startswith("/slots/"):
            slot_id = int(parsed.path.split("/")[2])
            action = urllib.parse.parse_qs(parsed.query).get("action", [""])[0]
            if action == "erase":
                n = len(type(self).slot_tokens)
                type(self).slot_tokens = []
                self._write_json({"id_slot": slot_id, "n_erased": n})
                return
            if action == "save":
                filename = body["filename"]
                path = self._file_path(filename)
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = json.dumps({"tokens": type(self).slot_tokens}, separators=(",", ":")).encode("utf-8")
                path.write_bytes(b"MOCKKV\n" + payload)
                self._write_json({
                    "id_slot": slot_id,
                    "filename": filename,
                    "n_saved": len(type(self).slot_tokens),
                    "n_written": path.stat().st_size,
                })
                return
            if action == "restore":
                filename = body["filename"]
                path = self._file_path(filename)
                raw = path.read_bytes()
                payload = json.loads(raw.split(b"\n", 1)[1].decode("utf-8"))
                type(self).slot_tokens = payload["tokens"]
                self._write_json({
                    "id_slot": slot_id,
                    "filename": filename,
                    "n_restored": len(type(self).slot_tokens),
                    "n_read": path.stat().st_size,
                })
                return

        self._write_json({"error": "not found"}, status=404)


class MockLlamaServer:
    def __init__(self, cache_dir):
        handler = type("TestMockLlamaHandler", (MockLlamaHandler,), {})
        handler.cache_dir = cache_dir
        handler.slot_tokens = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class PrefixCacheIntegrationContract:
    """Shared integration behavior run against mock and live backends."""

    def run_cli(self, *args):
        buf = io.StringIO()
        argv = ["--cache-dir", str(self.cache_dir), "--base-url", self.base_url, *args]
        with contextlib.redirect_stdout(buf):
            prefix_cache.main(list(argv))
        out = buf.getvalue().strip()
        return json.loads(out) if out else None

    def add_node(self, label, prompt):
        result = self.run_cli("add", "--label", label, "--prompt", prompt)
        node = result["node"]
        self.created_node_ids.append(node["id"])
        return node

    def test_add_and_lookup_longer_prompt(self):
        self.run_cli("init")
        prompt = f"{self.prompt_prefix} shared stable prefix for integration lookup."

        node = self.add_node("integration-prefix", prompt)
        match = self.run_cli("lookup", "--prompt", prompt + " suffix tokens")

        self.assertEqual(match["match"]["id"], node["id"])
        self.assertEqual(match["match"]["token_count"], node["token_count"])
        self.assertTrue((self.cache_dir / node["bin_file"]).exists())

    def test_lookup_no_cache_missed_cache_and_cache_too_long(self):
        self.run_cli("init")
        self.assertIsNone(self.run_cli("lookup", "--prompt", "nothing cached yet")["match"])

        self.add_node("miss-target", f"{self.prompt_prefix} abcdef")

        self.assertIsNone(self.run_cli("lookup", "--prompt", f"{self.prompt_prefix} abcXYZ")["match"])
        self.assertIsNone(self.run_cli("lookup", "--prompt", f"{self.prompt_prefix} abc")["match"])

    def test_parent_relationship_uses_longest_existing_prefix(self):
        self.run_cli("init")
        parent_prompt = f"{self.prompt_prefix} parent-prefix"
        child_prompt = parent_prompt + " child-suffix"

        parent = self.add_node("parent", parent_prompt)
        child = self.add_node("child", child_prompt)

        self.assertEqual(child["parent_id"], parent["id"])


class TestPrefixCacheIntegrationMock(PrefixCacheIntegrationContract, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_dir = pathlib.Path(self.tmp.name)
        self.server = MockLlamaServer(self.cache_dir)
        self.server.start()
        self.base_url = self.server.base_url
        self.created_node_ids = []
        self.prompt_prefix = "mock-prefix-cache-contract"

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()


@unittest.skipUnless(
    os.environ.get("RUN_LIVE_PREFIX_CACHE_TESTS") == "1",
    "set RUN_LIVE_PREFIX_CACHE_TESTS=1 to run live prefix-cache integration tests",
)
class TestPrefixCacheIntegrationLive(PrefixCacheIntegrationContract, unittest.TestCase):
    def setUp(self):
        self.cache_dir = pathlib.Path(
            os.environ.get("PREFIX_CACHE_LIVE_CACHE_DIR", str(prefix_cache.DEFAULT_CACHE_DIR))
        ).expanduser()
        self.base_url = os.environ.get("PREFIX_CACHE_LIVE_BASE_URL", prefix_cache.DEFAULT_BASE_URL)
        try:
            with urllib.request.urlopen(self.base_url.rstrip("/") + "/health", timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise unittest.SkipTest(f"live llama.cpp service unavailable: {e}")
        if data.get("status") != "ok":
            raise unittest.SkipTest(f"live llama.cpp service not healthy: {data!r}")
        self.created_node_ids = []
        self.prompt_prefix = "live-prefix-cache-contract-20260520"

    def tearDown(self):
        # Remove nodes created by live tests. Delete children first.
        cache = prefix_cache.PrefixCache(self.cache_dir)
        for node_id in reversed(self.created_node_ids):
            node = cache.get_node(node_id)
            if node:
                try:
                    cache.absolute_bin_path(node["bin_file"]).unlink()
                except FileNotFoundError:
                    pass
                with contextlib.closing(cache.connect()) as db:
                    db.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
                    db.commit()
        # Put the normal static slot back if available.
        try:
            prefix_cache.LlamaClient(self.base_url).restore_slot(0, "slot_0_current.bin")
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
