#!/usr/bin/env python3
"""lmcache-proxy — HTTP proxy for llama.cpp with disk-based KV cache.

On-demand version: intercepts requests, finds matching KV states on disk,
checks metadata compatibility, restores into an idle slot, then forwards.

No background thread — everything happens at request time.

Usage:
    python3 lmcache-proxy-on-demand.py --host 0.0.0.0 --port 8090 \
        --server localhost --llama-port 8081 \
        --cache-dir ~/.cache/llm-kv
"""

import argparse
import hashlib
import json
import logging
import os
import pathlib
import time
import urllib.request
import urllib.parse
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Lock
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


class KVCache:
    """Disk-based KV cache keyed by prompt prefix hash.

    Cache structure on disk:
        <cache_dir>/<sha32_prefix>/<slot_id>_<timestamp>.bin
        <cache_dir>/<sha32_prefix>/<slot_id>_<timestamp>.meta.json

    Each .bin is a llama.cpp KV save from the slot save REST API.
    The .meta.json sidecar stores model metadata for compatibility checks.
    """

    def __init__(self, cache_dir: str, top_k: int = 3):
        self.cache_dir = pathlib.Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.top_k = top_k
        self._lock = Lock()

    def _hash_prefix(self, prefix: str) -> str:
        return hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:32]

    def _list_cached(self, prefix_hash: str) -> list[str]:
        """Return cached KV filenames matching this prefix hash (most recent first)."""
        p = self.cache_dir / prefix_hash
        if not p.is_dir():
            return []
        entries = sorted(p.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
        return [str(e) for e in entries[:self.top_k]]

    def find_match(self, prompt: str) -> list[str]:
        """Return up to top_k cached KV .bin files whose prefix matches the prompt."""
        h = self._hash_prefix(prompt)
        return self._list_cached(h)

    def load_metadata(self, kv_path: str) -> dict | None:
        """Load the .meta.json sidecar next to a KV binary. Returns None on failure."""
        meta_path = kv_path.replace('.bin', '.meta.json')
        try:
            with open(meta_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            log.debug("no metadata for %s", kv_path)
            return None

    def is_compatible(self, meta: dict, server_info: dict) -> bool:
        """Check if cached KV metadata matches the running server's model config."""
        required_keys = ("model_hash", "context_size")
        for key in required_keys:
            if key not in meta or key not in server_info:
                log.debug("missing %s in metadata or server info", key)
                return False

        # Model hash must match (prevents cross-model restore)
        if meta["model_hash"] != server_info.get("model_hash"):
            log.debug("model hash mismatch: cached=%s server=%s",
                       meta["model_hash"], server_info.get("model_hash"))
            return False

        # Context size must match
        if meta["context_size"] != server_info.get("context_size"):
            log.debug("context size mismatch: cached=%d server=%d",
                       meta["context_size"], server_info.get("context_size"))
            return False

        return True


def _call_llama(method: str, path: str, body=None, server: str = "localhost", port: int = 8081):
    """Helper to call llama.cpp's REST API."""
    url = f"http://{server}:{port}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.warning("llama API call failed: %s %s — %s", method, path, e)
        return None


def _restore_slot(slot_id: int, kv_path: str, server: str = "localhost", port: int = 8081):
    """Restore a KV state into a slot via llama.cpp's REST API."""
    path = f"/slots/{slot_id}?action=restore"
    body = {"filename": kv_path}
    result = _call_llama("POST", path, body, server, port)
    if result:
        log.info("restored KV slot %d ← %s", slot_id, kv_path)
    return result


def _get_idle_slots(server: str, port: int) -> list[dict]:
    """Get list of idle slots from llama.cpp."""
    try:
        url = f"http://{server}:{port}/slots"
        with urllib.request.urlopen(url, timeout=10) as resp:
            slots = json.loads(resp.read().decode())
            return [s for s in slots if not s.get("is_busy", False)]
    except Exception as e:
        log.debug("failed to get slots: %s", e)
    return []


def _get_server_model_info(server: str, port: int) -> dict | None:
    """Get the running server's model info from llama.cpp's /health endpoint.

    Returns a dict with model_hash and context_size for compatibility checks.
    Falls back to None if the endpoint doesn't provide enough info.
    """
    try:
        url = f"http://{server}:{port}/health"
        with urllib.request.urlopen(url, timeout=10) as resp:
            health = json.loads(resp.read().decode())

        # Extract model info from health endpoint
        model_info = health.get("model", {})
        model_path = model_info.get("path", "")
        ctx_size = model_info.get("ctx_size", 0)

        # Compute a simple hash of the model path as identifier
        model_hash = hashlib.sha256(model_path.encode("utf-8")).hexdigest()[:16]

        return {
            "model_hash": model_hash,
            "context_size": ctx_size,
        }
    except Exception as e:
        log.debug("failed to get server model info: %s", e)
        return None


class LMCacheHandler(BaseHTTPRequestHandler):
    """HTTP proxy handler that intercepts requests and loads KV on-demand."""

    # Set these from argparse
    llama_server = "localhost"
    llama_port = 8081
    proxy_port = 8090
    cache_dir_obj: KVCache = None  # type: ignore[assignment]
    server_model_info: dict | None = None

    def _forward(self, method: str, path: str, body_bytes: bytes):
        """Forward request to llama.cpp server."""
        url = f"http://{self.llama_server}:{self.llama_port}{path}"
        req = urllib.request.Request(url, data=body_bytes, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                self.send_response(resp.status)
                self._write_headers(resp.headers)
                self._write_body(resp.read())
        except urllib.error.HTTPError as e:
            log.warning("llama server error: %s", e)
            self._send_error(502, f"llama server error: {e}")

    def _extract_prompts(self, body: dict) -> list[str]:
        """Extract prompt strings from various llama.cpp request formats."""
        prompts = []
        # /v1/chat/completions format (OpenAI-compatible)
        if "messages" in body:
            for msg in body.get("messages", []):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for item in content:
                            if item.get("type") == "text":
                                prompts.append(item["text"])
                    else:
                        prompts.append(str(content))
        # /completion format (legacy)
        elif "prompt" in body:
            prompts.append(body["prompt"])
        return prompts

    def _handle_request(self, method: str):
        """Parse request, optionally restore KV, then proxy to llama.cpp."""
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b""

        path = self.path
        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except json.JSONDecodeError:
            body = {}

        # Extract prompts from the request (for KV lookup)
        prompts = self._extract_prompts(body)

        # Try to find and restore a matching KV state before forwarding
        kv_restored = False
        for prompt_text in prompts:
            kv_files = self.cache_dir_obj.find_match(prompt_text)
            if not kv_files:
                continue

            # Load metadata and check compatibility
            meta = self.cache_dir_obj.load_metadata(kv_files[0])
            if not meta or not self._check_compatibility(meta):
                log.debug("KV incompatible, skipping: %s", kv_files[0])
                continue

            # Find an idle slot
            slot_id = self._get_available_slot()
            if slot_id is None:
                break

            # Restore KV into that slot
            result = _restore_slot(slot_id, kv_files[0],
                                   self.llama_server, self.llama_port)
            if result:
                log.info("restored KV into slot %d", slot_id)
                kv_restored = True
                break  # only need one match per request

        if kv_restored:
            log.info("KV restored before forwarding request")

        # Forward to llama.cpp server
        return self._forward(method, path, body_bytes)

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

    def _check_compatibility(self, meta: dict) -> bool:
        """Check if cached KV is compatible with current server config."""
        if self.server_model_info is None:
            log.warning("no server model info available; skipping compatibility check")
            # If we can't verify, fall through to llama.cpp's --slot-prompt-similarity guard
            return True

        return self.cache_dir_obj.is_compatible(meta, self.server_model_info)

    def do_GET(self):
        self._handle_request("GET")

    def do_POST(self):
        self._handle_request("POST")

    def do_PUT(self):
        self._handle_request("PUT")

    def do_DELETE(self):
        self._handle_request("DELETE")

    def _send_error(self, code: int, message: str = ""):
        self.send_response(code)
        self._write_body(message)

    def _write_headers(self, headers):
        """Write response headers from urllib's response."""
        for key in headers.keys():
            val = headers.get(key)
            if val is not None:
                self.headers.add_header(key, val)
        self.send_response(int(headers.status))

    def _write_body(self, body: bytes):
        """Write response body."""
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="LMCache proxy for llama.cpp (on-demand)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8090, help="Proxy port (default: 8090)")
    parser.add_argument("--server", default="localhost", help="llama.cpp server hostname")
    parser.add_argument("--llama-port", type=int, default=8081, help="llama.cpp server port")
    parser.add_argument("--cache-dir", default="~/.cache/llm-kv", help="KV cache directory")
    parser.add_argument("--top-k", type=int, default=3, help="Max cached states to try per prompt")
    args = parser.parse_args()

    # Initialize cache
    cache = KVCache(args.cache_dir, top_k=args.top_k)

    # Set handler class attributes
    LMCacheHandler.llama_server = args.server
    LMCacheHandler.llama_port = args.llama_port
    LMCacheHandler.proxy_port = args.port
    LMCacheHandler.cache_dir_obj = cache

    # Fetch server model info once at startup
    LMCacheHandler.server_model_info = _get_server_model_info(args.server, args.llama_port)
    if LMCacheHandler.server_model_info:
        log.info("server model info: %s", LMCacheHandler.server_model_info)
    else:
        log.warning("could not fetch server model info — compatibility checks disabled")

    log.info("Starting LMCache proxy (on-demand) on %s:%d", args.host, args.port)
    log.info("llama.cpp server: %s:%d", args.server, args.llama_port)
    log.info("KV cache dir: %s (top_k=%d)", args.cache_dir, args.top_k)

    try:
        server = HTTPServer((args.host, args.port), LMCacheHandler)
        log.info("proxy ready — clients should point to port %d", args.proxy_port)
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
