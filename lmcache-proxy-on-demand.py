#!/usr/bin/env python3
"""lmcache-proxy — HTTP proxy for llama.cpp with disk-based KV cache.

On-demand version: intercepts requests, restores the best matching saved prefix
from the prefix trie, forwards the original request unchanged, then saves the
post-generation slot state back into the trie.
"""

import argparse
import hashlib
import json
import logging
import pathlib
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Lock

import prefix_cache

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

GIB = 1024 * 1024 * 1024
MIB = 1024 * 1024


class KVCache:
    """Legacy disk-based KV cache keyed by whole prompt hash.

    Kept for old tests/fallback. New automatic cache behavior uses
    prefix_cache.PrefixCache.
    """

    def __init__(self, cache_dir: str, top_k: int = 3):
        self.cache_dir = pathlib.Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.top_k = top_k
        self._lock = Lock()

    def _hash_prefix(self, prefix: str) -> str:
        return hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:32]

    def _list_cached(self, prefix_hash: str) -> list[str]:
        p = self.cache_dir / prefix_hash
        if not p.is_dir():
            return []
        entries = sorted(p.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
        return [str(e) for e in entries if str(e).endswith(".bin")][:self.top_k]

    def find_match(self, prompt: str) -> list[str]:
        h = self._hash_prefix(prompt)
        return self._list_cached(h)

    def load_metadata(self, kv_path: str) -> dict | None:
        meta_path = kv_path.replace(".bin", ".meta.json")
        try:
            with open(meta_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            log.debug("no metadata for %s", kv_path)
            return None

    def is_compatible(self, meta: dict, server_info: dict) -> bool:
        required_keys = ("model_hash", "context_size")
        for key in required_keys:
            if key not in meta or key not in server_info:
                log.debug("missing %s in metadata or server info", key)
                return False
        if meta["model_hash"] != server_info.get("model_hash"):
            log.debug("model hash mismatch: cached=%s server=%s", meta["model_hash"], server_info.get("model_hash"))
            return False
        if meta["context_size"] != server_info.get("context_size"):
            log.debug("context size mismatch: cached=%s server=%s", meta["context_size"], server_info.get("context_size"))
            return False
        return True


@dataclass
class RequestCacheContext:
    prompt_text: str
    prompt_tokens: list[int]
    restored_node_id: str | None = None


@dataclass
class ForwardResult:
    status: int
    content_type: str
    body: bytes


def _call_llama(method: str, path: str, body=None, server: str = "localhost", port: int = 8081, timeout: int = 30):
    """Helper to call llama.cpp's REST API."""
    url = f"http://{server}:{port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except Exception as e:
        log.warning("llama API call failed: %s %s — %s", method, path, e)
        return None


def _restore_slot(slot_id: int, kv_path: str, server: str = "localhost", port: int = 8081):
    path = f"/slots/{slot_id}?action=restore"
    result = _call_llama("POST", path, {"filename": kv_path}, server, port, timeout=300)
    if result:
        log.info("restored KV slot %d ← %s", slot_id, kv_path)
    return result


def _save_slot(slot_id: int, kv_path: str, server: str = "localhost", port: int = 8081):
    path = f"/slots/{slot_id}?action=save"
    result = _call_llama("POST", path, {"filename": kv_path}, server, port, timeout=300)
    if result:
        log.info("saved KV slot %d → %s", slot_id, kv_path)
    return result


def _get_server_model_info(server: str, port: int) -> dict | None:
    """Get compatibility info for the legacy cache path."""
    try:
        url = f"http://{server}:{port}/health"
        with urllib.request.urlopen(url, timeout=10) as resp:
            health = json.loads(resp.read().decode())
        model_info = health.get("model", {})
        model_path = model_info.get("path", "")
        ctx_size = model_info.get("ctx_size", 0)
        model_hash = hashlib.sha256(model_path.encode("utf-8")).hexdigest()[:16]
        return {"model_hash": model_hash, "context_size": ctx_size}
    except Exception as e:
        log.debug("failed to get server model info: %s", e)
        return None


class LMCacheHandler(BaseHTTPRequestHandler):
    """HTTP proxy handler that restores and saves prefix-cache KV on demand."""

    llama_server = "localhost"
    llama_port = 8081
    proxy_port = 8090
    slot_id = 0

    # Legacy cache path, preserved for old tests/fallback.
    cache_dir_obj: KVCache | None = None
    server_model_info: dict | None = None

    # New trie-backed prefix cache path.
    prefix_cache_obj: prefix_cache.PrefixCache | None = None
    prefix_cache_enabled = True
    auto_save_enabled = True
    min_save_tokens = 256
    max_cache_bytes = 2 * GIB
    min_free_bytes = 512 * MIB
    strict_prefix_restore = True

    def _forward(self, method: str, path: str, body_bytes: bytes) -> ForwardResult | None:
        """Forward request to llama.cpp, streaming response chunks to the client."""
        url = f"http://{self.llama_server}:{self.llama_port}{path}"
        req = urllib.request.Request(url, data=body_bytes if method != "GET" else None, method=method)
        if body_bytes:
            req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))

        collected = bytearray()
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                content_type = resp.headers.get("Content-Type", "")
                self.send_response(resp.status, resp.reason)
                for key in resp.headers.keys():
                    lower = key.lower()
                    if lower in {"transfer-encoding", "connection", "content-length"}:
                        continue
                    val = resp.headers.get(key)
                    if val is not None:
                        self.send_header(key, val)
                self.end_headers()

                if "text/event-stream" in content_type:
                    while True:
                        chunk = resp.readline()
                        if not chunk:
                            break
                        collected.extend(chunk)
                        self.wfile.write(chunk)
                        self.wfile.flush()
                else:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        collected.extend(chunk)
                        self.wfile.write(chunk)
                    self.wfile.flush()
                return ForwardResult(resp.status, content_type, bytes(collected))
        except urllib.error.HTTPError as e:
            log.warning("llama server error: %s", e)
            try:
                err_body = e.read()
            except Exception:
                err_body = str(e).encode("utf-8")
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "text/plain; charset=utf-8"))
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self._write_body(err_body)
            return ForwardResult(e.code, e.headers.get("Content-Type", ""), err_body)
        except Exception as e:
            log.warning("llama server error: %s", e)
            self._send_error(502, f"llama server error: {e}")
            return None

    def _extract_prompts(self, body: dict) -> list[str]:
        """Legacy prompt extraction for the old whole-prompt hash cache."""
        prompts = []
        if "messages" in body:
            for msg in body.get("messages", []):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                prompts.append(item.get("text", ""))
                    else:
                        prompts.append(str(content))
        elif "prompt" in body:
            prompts.append(str(body["prompt"]))
        return prompts

    def _tokenize(self, prompt_text: str) -> list[int] | None:
        res = _call_llama(
            "POST",
            "/tokenize",
            {"content": prompt_text},
            self.llama_server,
            self.llama_port,
            timeout=120,
        )
        if not isinstance(res, dict) or not isinstance(res.get("tokens"), list):
            log.warning("unexpected /tokenize response: %r", res)
            return None
        return [int(t) for t in res["tokens"]]

    def _render_prompt_for_cache(self, path: str, body: dict) -> str | None:
        if "prompt" in body:
            prompt = body.get("prompt")
            return prompt if isinstance(prompt, str) else json.dumps(prompt, separators=(",", ":"))

        if "messages" in body and (path.startswith("/v1/chat/completions") or path.startswith("/chat/completions")):
            # Send the full chat body, not just messages. llama.cpp's chat
            # template can include tools, tool_choice, parallel_tool_calls,
            # chat_template_kwargs, add_generation_prompt, reasoning_format,
            # and other template-affecting fields. Omitting them creates cache
            # keys that do not match the actual slot prompt.
            res = _call_llama(
                "POST",
                "/apply-template",
                dict(body),
                self.llama_server,
                self.llama_port,
                timeout=120,
            )
            if isinstance(res, dict) and isinstance(res.get("prompt"), str):
                return res["prompt"]
            log.warning("unexpected /apply-template response: %r", res)
        return None

    def _request_cache_context(self, path: str, body: dict) -> RequestCacheContext | None:
        if not self.prefix_cache_enabled or self.prefix_cache_obj is None:
            return None
        prompt_text = self._render_prompt_for_cache(path, body)
        if not prompt_text:
            return None
        tokens = self._tokenize(prompt_text)
        if not tokens:
            return None
        return RequestCacheContext(prompt_text=prompt_text, prompt_tokens=tokens)

    def _lookup_and_restore_prefix(self, ctx: RequestCacheContext) -> None:
        cache = self.prefix_cache_obj
        if cache is None:
            return
        cache.init()
        node = cache.lookup(ctx.prompt_tokens, touch=True, strictly_less=self.strict_prefix_restore)
        if not node:
            return
        result = _restore_slot(self.slot_id, node["bin_file"], self.llama_server, self.llama_port)
        if result:
            ctx.restored_node_id = node["id"]
            log.info("prefix-cache restored node %s (%s tokens)", node["id"], node["token_count"])

    def _restore_legacy_cache(self, body: dict) -> None:
        if self.prefix_cache_obj is not None or self.cache_dir_obj is None:
            return
        for prompt_text in self._extract_prompts(body):
            kv_files = self.cache_dir_obj.find_match(prompt_text)
            if not kv_files:
                continue
            for kv_path in kv_files:
                meta = self.cache_dir_obj.load_metadata(kv_path)
                if not meta or not self._check_compatibility(meta):
                    continue
                if _restore_slot(self.slot_id, kv_path, self.llama_server, self.llama_port):
                    log.info("legacy KV restored before forwarding request")
                    return

    def _ensure_storage_room(self) -> bool:
        cache = self.prefix_cache_obj
        if cache is None:
            return False
        cache.init()
        cache.prune(max_bytes=self.max_cache_bytes, max_nodes=None, dry_run=False)

        while True:
            free = shutil.disk_usage(cache.cache_dir).free
            if free >= self.min_free_bytes:
                return True
            total = cache.total_bytes()
            if total <= 0:
                log.warning(
                    "prefix-cache autosave skipped: free disk %d bytes below minimum %d and no cache is prunable",
                    free,
                    self.min_free_bytes,
                )
                return False
            removed = cache.prune(max_bytes=max(total - 1, 0), max_nodes=None, dry_run=False)
            if not removed:
                log.warning(
                    "prefix-cache autosave skipped: free disk %d bytes below minimum %d and no leaf cache node is prunable",
                    free,
                    self.min_free_bytes,
                )
                return False
            log.info("prefix-cache pruned %d node(s) to recover low disk space", len(removed))

    def _props(self) -> dict:
        props = _call_llama("GET", "/props", None, self.llama_server, self.llama_port, timeout=30)
        return props if isinstance(props, dict) else {}

    def _auto_save_prefix_cache(self, ctx: RequestCacheContext, request_body: dict, result: ForwardResult | None) -> None:
        if not self.auto_save_enabled or self.prefix_cache_obj is None or result is None:
            return
        if result.status < 200 or result.status >= 300:
            return

        # Key automatic nodes by the exact incoming prompt tokens we can prove.
        # The saved slot may contain additional generated tokens; that is safe.
        # On a later longer chat request, llama.cpp will compute the true LCP
        # against the restored slot and reuse any matching generated tokens too.
        tokens = ctx.prompt_tokens
        if len(tokens) < self.min_save_tokens:
            log.debug("prefix-cache autosave skipped: %d prompt tokens < min %d", len(tokens), self.min_save_tokens)
            return

        cache = self.prefix_cache_obj
        cache.init()
        if not self._ensure_storage_room():
            return

        tmp_file = f"prefix_tmp_{time.time_ns()}.bin"
        tmp_path = cache.absolute_bin_path(tmp_file)
        save = _save_slot(self.slot_id, tmp_file, self.llama_server, self.llama_port)
        if not isinstance(save, dict):
            return
        n_saved = int(save.get("n_saved", -1))
        if n_saved < len(tokens):
            log.warning("prefix-cache autosave skipped: n_saved=%d prompt_tokens=%d", n_saved, len(tokens))
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            return
        if not tmp_path.exists():
            log.warning("prefix-cache autosave skipped: save succeeded but bin is missing: %s", tmp_path)
            return

        node_id, digest = prefix_cache.node_id_for(tokens)
        if cache.get_node(node_id):
            log.debug("prefix-cache autosave skipped: node already exists %s", node_id)
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            return

        parent_id = cache.parent_for(tokens, node_id)
        bin_file = cache.relative_node_bin(node_id)
        bin_path = cache.absolute_bin_path(bin_file)
        if bin_path.exists():
            log.warning("prefix-cache autosave skipped: bin exists without DB node: %s", bin_path)
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            return
        tmp_path.rename(bin_path)
        size_bytes = bin_path.stat().st_size

        props = self._props()
        settings = props.get("default_generation_settings", {}) if isinstance(props, dict) else {}
        node = {
            "id": node_id,
            "parent_id": parent_id,
            "label": "auto",
            "boundary": "auto-response",
            "token_count": len(tokens),
            "prefix_hash": digest,
            "hash_algo": prefix_cache.HASH_ALGO,
            "bin_file": bin_file,
            "size_bytes": size_bytes,
            "n_saved": n_saved,
            "model_alias": props.get("model_alias") if isinstance(props, dict) else None,
            "model_path": props.get("model_path") if isinstance(props, dict) else None,
            "ctx_size": settings.get("n_ctx") if isinstance(settings, dict) else None,
            "hits": 0,
            "created_at": prefix_cache.utc_now(),
            "last_used": None,
            "pinned": False,
            "meta": {
                "source": "lmcache-proxy-on-demand",
                "restored_node_id": ctx.restored_node_id,
                "save_response": save,
                "response_content_type": result.content_type,
            },
        }
        cache.insert_node(node)
        cache.prune(max_bytes=self.max_cache_bytes, max_nodes=None, dry_run=False)
        log.info("prefix-cache autosaved node %s (%d tokens, %.1f MiB)", node_id, len(tokens), size_bytes / MIB)

    def _handle_request(self, method: str):
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b""
        path = self.path
        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except json.JSONDecodeError:
            body = {}

        if self.headers.get("X-LMCache-Bypass") == "1":
            return self._forward(method, path, body_bytes)

        ctx = self._request_cache_context(path, body) if method == "POST" else None
        if ctx is not None:
            self._lookup_and_restore_prefix(ctx)
        else:
            self._restore_legacy_cache(body)

        result = self._forward(method, path, body_bytes)

        if ctx is not None:
            try:
                self._auto_save_prefix_cache(ctx, body, result)
            except Exception as e:
                log.warning("prefix-cache autosave failed gracefully: %s", e)
        return result

    def _check_compatibility(self, meta: dict) -> bool:
        if self.server_model_info is None:
            log.warning("no server model info available; skipping compatibility check")
            return True
        assert self.cache_dir_obj is not None
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
        body = message.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write_body(body)

    def _write_response(self, resp):
        self.send_response(resp.status, resp.reason)
        body = resp.read()
        for key in resp.headers.keys():
            lower = key.lower()
            if lower in {"transfer-encoding", "connection"}:
                continue
            val = resp.headers.get(key)
            if val is not None:
                self.send_header(key, val)
        self.end_headers()
        self.wfile.write(body)

    def _write_body(self, body: bytes | str):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)


def parse_bytes(value: str) -> int:
    s = value.strip().lower()
    if s.endswith("gib"):
        return int(float(s[:-3]) * GIB)
    if s.endswith("gb"):
        return int(float(s[:-2]) * 1000 * 1000 * 1000)
    if s.endswith("mib"):
        return int(float(s[:-3]) * MIB)
    if s.endswith("mb"):
        return int(float(s[:-2]) * 1000 * 1000)
    return int(s)


def main():
    parser = argparse.ArgumentParser(description="LMCache proxy for llama.cpp (on-demand)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8090, help="Proxy port (default: 8090)")
    parser.add_argument("--server", default="localhost", help="llama.cpp server hostname")
    parser.add_argument("--llama-port", type=int, default=8081, help="llama.cpp server port")
    parser.add_argument("--cache-dir", default=str(prefix_cache.DEFAULT_CACHE_DIR), help="KV cache directory")
    parser.add_argument("--top-k", type=int, default=3, help="Legacy cache top-k fallback")
    parser.add_argument("--slot", type=int, default=0, help="llama.cpp slot id to restore/save (default: 0)")
    parser.add_argument("--min-save-tokens", type=int, default=256, help="Minimum tokens before autosaving a prefix node")
    parser.add_argument("--prefix-cache-max-bytes", type=parse_bytes, default=2 * GIB, help="Max prefix cache bytes before pruning (default: 2GiB)")
    parser.add_argument("--prefix-cache-min-free-bytes", type=parse_bytes, default=512 * MIB, help="Minimum filesystem free bytes before autosave (default: 512MiB)")
    parser.add_argument("--no-prefix-cache", action="store_true", help="Disable trie-backed prefix cache")
    parser.add_argument("--no-auto-save", action="store_true", help="Disable autosaving completed requests into prefix cache")
    parser.add_argument("--allow-exact-prefix-restore", action="store_true", help="Allow restoring exact-length prefix matches (currently unsafe on this llama.cpp build)")
    args = parser.parse_args()

    cache_dir = pathlib.Path(args.cache_dir).expanduser()
    legacy_cache = KVCache(str(cache_dir), top_k=args.top_k)
    trie_cache = None if args.no_prefix_cache else prefix_cache.PrefixCache(cache_dir)
    if trie_cache is not None:
        trie_cache.init()

    LMCacheHandler.llama_server = args.server
    LMCacheHandler.llama_port = args.llama_port
    LMCacheHandler.proxy_port = args.port
    LMCacheHandler.slot_id = args.slot
    LMCacheHandler.cache_dir_obj = legacy_cache
    LMCacheHandler.prefix_cache_obj = trie_cache
    LMCacheHandler.prefix_cache_enabled = trie_cache is not None
    LMCacheHandler.auto_save_enabled = not args.no_auto_save
    LMCacheHandler.min_save_tokens = args.min_save_tokens
    LMCacheHandler.max_cache_bytes = args.prefix_cache_max_bytes
    LMCacheHandler.min_free_bytes = args.prefix_cache_min_free_bytes
    LMCacheHandler.strict_prefix_restore = not args.allow_exact_prefix_restore

    LMCacheHandler.server_model_info = _get_server_model_info(args.server, args.llama_port)
    if LMCacheHandler.server_model_info:
        log.info("server model info: %s", LMCacheHandler.server_model_info)
    else:
        log.warning("could not fetch server model info — legacy compatibility checks disabled")

    log.info("Starting LMCache proxy (on-demand) on %s:%d", args.host, args.port)
    log.info("llama.cpp server: %s:%d", args.server, args.llama_port)
    log.info("KV cache dir: %s", cache_dir)
    if trie_cache is not None:
        log.info(
            "prefix cache enabled: min_save_tokens=%d max_bytes=%d min_free_bytes=%d strict_prefix_restore=%s auto_save=%s",
            args.min_save_tokens,
            args.prefix_cache_max_bytes,
            args.prefix_cache_min_free_bytes,
            not args.allow_exact_prefix_restore,
            not args.no_auto_save,
        )

    try:
        server = HTTPServer((args.host, args.port), LMCacheHandler)
        log.info("proxy ready — clients should point to port %d", args.port)
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
