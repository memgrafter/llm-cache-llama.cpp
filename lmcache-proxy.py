#!/usr/bin/env python3
"""lmcache-proxy — HTTP proxy for llama.cpp with disk-based KV cache.

Sits between a client and llama.cpp server, intercepting requests to check
for cached KV states on disk. When an idle slot is detected, the proxy
pre-loads a matching KV state from disk before the next request arrives.

This works within llama.cpp's slot model: pre-load KV into idle slots,
then let llama.cpp route requests normally.

Usage:
    python3 lmcache-proxy.py --host 0.0.0.0 --port 8090 \
        --server localhost --llama-port 8081 \
        --cache-dir ~/.cache/llm-kv

Clients should point at the proxy's port instead of llama.cpp directly.
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
from threading import Lock, Thread
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


class KVCache:
    """Disk-based KV cache keyed by prompt prefix hash.

    Cache structure on disk:
        <cache_dir>/<sha32_prefix>/<slot_id>_<timestamp>.bin
    Each file is a llama.cpp KV save from the slot save REST API.
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
        """Return up to top_k cached KV files whose prefix matches the prompt."""
        h = self._hash_prefix(prompt)
        return self._list_cached(h)


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


def _save_slot(slot_id: int, filename: str, server: str = "localhost", port: int = 8081):
    """Save a slot's KV state via llama.cpp's REST API."""
    path = f"/slots/{slot_id}?action=save"
    body = {"filename": filename}
    result = _call_llama("POST", path, body, server, port)
    if result:
        log.info("saved KV slot %d → %s", slot_id, filename)
    return result


class SlotManager:
    """Manages pre-loading KV states into idle slots."""

    def __init__(self, llama_server, llama_port, cache, poll_interval: float = 5.0,
                 min_match_tokens: int = 5000, min_match_ratio: float = 0.8):
        self.llama_server = llama_server
        self.llama_port = llama_port
        self.cache = cache
        self.poll_interval = poll_interval
        self.min_match_tokens = min_match_tokens
        self.min_match_ratio = min_match_ratio
        self._running = False
        self._thread = None
        self._slot_hash: dict[int, str] = {}  # slot_id -> prompt hash of loaded KV
        self._slot_tokens: dict[int, int] = {}  # slot_id -> estimated token count in loaded KV
        self._slot_time: dict[int, float] = {}  # slot_id -> last used timestamp (for LRU)
        self._lock = Lock()

    def get_best_slot(self, prompt: str) -> int | None:
        """Return the slot whose loaded KV best matches the prompt hash, or None.

        Enforces minimum match threshold: the slot's loaded token count must be at
        least min_match_tokens AND at least min_match_ratio of the request's estimated
        token count (whichever is lower). Prevents routing short system-prompt-only
        requests to slots loaded with long conversations.
        """
        h = self.cache._hash_prefix(prompt)
        # estimate tokens from char count (~4 chars per token)
        req_tokens = max(1, len(prompt) // 4)
        threshold = min(self.min_match_tokens, int(req_tokens * self.min_match_ratio))

        with self._lock:
            for sid, sh in self._slot_hash.items():
                if sh == h:
                    slot_tok = self._slot_tokens.get(sid, 0)
                    if slot_tok >= threshold:
                        return sid
        return None

    def try_disk_cache(self, prompt: str) -> int | None:
        """If no slot matches, check on-disk trie cache for a matching KV file.

        If found and an idle slot is available, restore the KV into that slot,
        update tracking state, and return the slot_id. Otherwise return None.
        """
        kv_files = self.cache.find_match(prompt)
        if not kv_files:
            return None

        # find an idle slot (not tracked in _slot_hash)
        idle_slots = self._get_idle_slots()
        if not idle_slots:
            return None

        slot_id = idle_slots[0]["id"]
        kv_path = kv_files[0]
        log.info("disk cache hit for prompt, restoring %s into slot %d", kv_path, slot_id)
        result = _restore_slot(slot_id, kv_path, self.llama_server, self.llama_port)
        if result:
            ph = self.cache._hash_prefix(prompt)
            est_tokens = max(1, len(prompt) // 4)
            with self._lock:
                self._slot_hash[slot_id] = ph
                self._slot_tokens[slot_id] = est_tokens
                self._slot_time[slot_id] = time.monotonic()
            log.info("KV restored from disk cache into slot %d", slot_id)
            return slot_id
        return None

    def lru_slot(self) -> int | None:
        """Return the slot_id with the oldest last-used time (for eviction)."""
        with self._lock:
            if not self._slot_time:
                return None
            return min(self._slot_time, key=self._slot_time.get)

    def update_slot_time(self, slot_id: int):
        """Record that a slot was used at the current time."""
        with self._lock:
            self._slot_time[slot_id] = time.monotonic()

    def start(self):
        """Start the slot manager background thread."""
        self._running = True
        self._thread = Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("slot manager started (poll every %.1fs)", self.poll_interval)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _get_idle_slots(self) -> list[dict]:
        """Get list of idle slots from llama.cpp."""
        try:
            url = f"http://{self.llama_server}:{self.llama_port}/slots"
            with urllib.request.urlopen(url, timeout=10) as resp:
                slots = json.loads(resp.read().decode())
                return [s for s in slots if not s.get("is_busy", False)]
        except Exception as e:
            log.debug("failed to get slots: %s", e)
            return []

    def _loop(self):
        """Background loop: find idle slots, try to load KV states."""
        while self._running:
            idle_slots = self._get_idle_slots()
            if not idle_slots:
                time.sleep(self.poll_interval)
                continue

            # For each idle slot, try to load a KV state
            for slot_info in idle_slots:
                slot_id = slot_info["id"]
                with self._lock:
                    already_loaded = slot_id in self._slot_hash
                if already_loaded:
                    continue

                # Get available prompts from loaded slots' history
                history = slot_info.get("history", [])
                if not history:
                    continue

                # Try to find a matching KV state for the first prompt
                for msg in history:
                    prompt_text = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                    if not prompt_text:
                        continue
                    kv_files = self.cache.find_match(prompt_text)
                    if not kv_files:
                        continue

                    # Try to restore the first cached KV state
                    kv_path = kv_files[0]
                    log.info("loading KV from %s into slot %d", kv_path, slot_id)
                    result = _restore_slot(slot_id, kv_path, self.llama_server, self.llama_port)
                    if result:
                        ph = self.cache._hash_prefix(prompt_text)
                        est_tokens = max(1, len(prompt_text) // 4)
                        with self._lock:
                            self._slot_hash[slot_id] = ph
                            self._slot_tokens[slot_id] = est_tokens
                            self._slot_time[slot_id] = time.monotonic()
                        log.info("KV restored into slot %d", slot_id)
                        break

            time.sleep(self.poll_interval)


class LMCacheHandler(BaseHTTPRequestHandler):
    """HTTP proxy handler that forwards requests to llama.cpp."""

    # Set these from argparse
    llama_server = "localhost"
    llama_port = 8081
    proxy_port = 8090
    cache_dir_obj: KVCache = None  # type: ignore[assignment]
    slot_manager: SlotManager | None = None  # type: ignore[assignment]

    def _forward(self, method: str, path: str, body_bytes: bytes):
        """Forward request to llama.cpp server."""
        url = f"http://{self.llama_server}:{self.llama_port}{path}"
        req = urllib.request.Request(url, data=body_bytes, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                self.send_response(resp.status)
                self.write_headers(resp.headers)
                self.write_body(resp.read())
        except urllib.error.HTTPError as e:
            log.warning("llama server error: %s", e)
            self.send_error(502, f"llama server error: {e}")

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
        """Parse request, then proxy to llama.cpp."""
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b""

        path = self.path
        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except json.JSONDecodeError:
            body = {}

        # Extract prompts from the request (for logging/debugging)
        prompts = self._extract_prompts(body)
        if prompts:
            log.debug("request prompts: %s", prompts[:1])  # just first one

            # Route to best matching slot by prompt hash
            sm = self.slot_manager
            if sm is not None:
                target = sm.get_best_slot(prompts[0])
                if target is None:
                    # No slot match — try on-disk trie cache fallback
                    target = sm.try_disk_cache(prompts[0])
                if target is not None:
                    body["id_slot"] = target
                    body_bytes = json.dumps(body).encode("utf-8")
                    sm.update_slot_time(target)
                    log.debug("routed to slot %d", target)

        # Forward to llama.cpp server
        return self._forward(method, path, body_bytes)

    def do_GET(self):
        self._handle_request("GET")

    def do_POST(self):
        self._handle_request("POST")

    def do_PUT(self):
        self._handle_request("PUT")

    def do_DELETE(self):
        self._handle_request("DELETE")

    def send_error(self, code: int, message: str = ""):
        self.send_response(code)
        self.write_body(message)


def main():
    parser = argparse.ArgumentParser(description="LMCache proxy for llama.cpp")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8090, help="Proxy port (default: 8090)")
    parser.add_argument("--server", default="localhost", help="llama.cpp server hostname")
    parser.add_argument("--llama-port", type=int, default=8081, help="llama.cpp server port")
    parser.add_argument("--cache-dir", default="~/.cache/llm-kv", help="KV cache directory")
    parser.add_argument("--top-k", type=int, default=3, help="Max cached states to try per prompt")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="Slot poll interval (seconds)")
    args = parser.parse_args()

    # Initialize cache
    cache = KVCache(args.cache_dir, top_k=args.top_k)

    # Initialize slot manager
    slot_manager = SlotManager(args.server, args.llama_port, cache, poll_interval=args.poll_interval)

    # Set handler class attributes
    LMCacheHandler.llama_server = args.server
    LMCacheHandler.llama_port = args.llama_port
    LMCacheHandler.proxy_port = args.port
    LMCacheHandler.cache_dir_obj = cache
    LMCacheHandler.slot_manager = slot_manager

    log.info("Starting LMCache proxy on %s:%d", args.host, args.port)
    log.info("llama.cpp server: %s:%d", args.server, args.llama_port)
    log.info("KV cache dir: %s (top_k=%d)", args.cache_dir, args.top_k)

    # Start slot manager before starting the HTTP server
    slot_manager.start()

    try:
        server = HTTPServer((args.host, args.port), LMCacheHandler)
        log.info("proxy ready — clients should point to port %d", args.proxy_port)
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        slot_manager.stop()


if __name__ == "__main__":
    main()
