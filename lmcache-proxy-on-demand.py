#!/usr/bin/env python3
"""lmcache-proxy — HTTP proxy for llama.cpp with disk-based KV cache.

On-demand version: intercepts requests, restores the best matching saved prefix
from the prefix trie, forwards the original request unchanged, then saves the
post-generation slot state back into the trie.
"""

import argparse
import hashlib
import json
import copy
import logging
import os
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
class AnchorConfig:
    label: str
    marker: str
    occurrence: int = 1
    side: str = "after"
    pinned: bool = False


@dataclass
class RequestAnchor:
    config: AnchorConfig
    text: str
    tokens: list[int]


@dataclass
class RequestCacheContext:
    prompt_text: str
    prompt_tokens: list[int]
    anchors: list[RequestAnchor]
    restored_node_id: str | None = None
    restored_via: str | None = None
    exact_prefix_newline_workaround: bool = False
    exact_prefix_node_id: str | None = None


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


def _erase_slot(slot_id: int, server: str = "localhost", port: int = 8081):
    path = f"/slots/{slot_id}?action=erase"
    result = _call_llama("POST", path, {"n_keep": 0}, server, port, timeout=180)
    if result:
        log.info("erased KV slot %d", slot_id)
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


class SlotState:
    """Per-slot tracking for multi-slot routing.

    Records which prefix node is loaded in each slot, the token count,
    and the last-used timestamp (for LRU eviction).
    """

    def __init__(self):
        self._slot_node_id: dict[int, str] = {}   # slot_id → node_id of loaded KV
        self._slot_tokens: dict[int, int] = {}     # slot_id → token count in loaded KV
        self._slot_time: dict[int, float] = {}     # slot_id → last-used monotonic time
        self._lock = Lock()

    def record(self, slot_id: int, node_id: str, token_count: int) -> None:
        """Record that a slot has the given node loaded."""
        with self._lock:
            self._slot_node_id[slot_id] = node_id
            self._slot_tokens[slot_id] = token_count
            self._slot_time[slot_id] = time.monotonic()

    def touch(self, slot_id: int) -> None:
        """Update last-used timestamp for a slot."""
        with self._lock:
            self._slot_time[slot_id] = time.monotonic()

    def forget(self, slot_id: int) -> None:
        """Remove tracking for a slot (e.g. after eviction/erase)."""
        with self._lock:
            self._slot_node_id.pop(slot_id, None)
            self._slot_tokens.pop(slot_id, None)
            self._slot_time.pop(slot_id, None)

    def has_slot(self, slot_id: int) -> bool:
        """Whether we are tracking this slot."""
        with self._lock:
            return slot_id in self._slot_node_id

    def node_for(self, slot_id: int) -> str | None:
        """Return the node_id loaded in a slot, or None."""
        with self._lock:
            return self._slot_node_id.get(slot_id)

    def tokens_for(self, slot_id: int) -> int | None:
        """Return the token count for a slot, or None."""
        with self._lock:
            return self._slot_tokens.get(slot_id)

    def lru_slot(self) -> int | None:
        """Return the least-recently-used tracked slot id, or None."""
        with self._lock:
            if not self._slot_time:
                return None
            return min(self._slot_time, key=self._slot_time.get)

    def all_slot_ids(self) -> list[int]:
        """Return all tracked slot ids."""
        with self._lock:
            return list(self._slot_node_id.keys())

    def idle_slots(self, available: list[int]) -> list[int]:
        """Return slots that are available but not currently tracked as loaded."""
        with self._lock:
            tracked = set(self._slot_node_id.keys())
            return [s for s in available if s not in tracked]


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
    min_match_tokens = 5000  # minimum tokens a match must cover to overwrite a loaded slot
    min_match_ratio = 0.8   # minimum fraction of request tokens a match must cover
    max_cache_bytes = 2 * GIB
    min_free_bytes = 512 * MIB
    strict_prefix_restore = True
    generated_prefix_enabled = True
    anchor_configs: list[AnchorConfig] = []

    # Multi-slot routing state.
    slot_state: SlotState | None = None
    n_parallel: int | None = None  # discovered from /slots endpoint, or None = single-slot mode"}}]}}]}}}}}}}}}}}}}}} catch (e) { log.warning(

    def _forward(self, method: str, path: str, body_bytes: bytes) -> ForwardResult | None:
        """Forward request to llama.cpp, streaming response chunks to the client."""
        url = f"http://{self.llama_server}:{self.llama_port}{path}"
        req = urllib.request.Request(url, data=body_bytes if method != "GET" else None, method=method)
        if body_bytes:
            req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))

        collected = bytearray()
        try:
            with urllib.request.urlopen(req, timeout=int(os.environ.get("LLAMA_FORWARD_TIMEOUT", "600"))) as resp:
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

    @staticmethod
    def _find_marker_boundary(text: str, cfg: AnchorConfig) -> int | None:
        if not cfg.marker or cfg.occurrence <= 0:
            return None
        start = 0
        pos = -1
        for _ in range(cfg.occurrence):
            pos = text.find(cfg.marker, start)
            if pos < 0:
                return None
            start = pos + len(cfg.marker)
        if cfg.side == "before":
            return pos
        if cfg.side == "after":
            return pos + len(cfg.marker)
        log.warning("unknown anchor side %r for %s", cfg.side, cfg.label)
        return None

    def _anchors_for_prompt(self, path: str, body: dict, prompt_text: str) -> list[RequestAnchor]:
        anchors: list[RequestAnchor] = []
        messages = body.get("messages") if isinstance(body, dict) else None
        first_role = None
        if path.startswith("/v1/chat/completions") or path.startswith("/chat/completions"):
            if isinstance(messages, list) and messages and isinstance(messages[0], dict):
                first_role = str(messages[0].get("role") or "")

        for cfg in self.anchor_configs:
            if cfg.label == "end-of-system-message" and first_role not in ("system", "developer"):
                continue
            end = self._find_marker_boundary(prompt_text, cfg)
            if end is None or end <= 0:
                continue
            text = prompt_text[:end]
            tokens = self._tokenize(text)
            if not tokens:
                continue
            anchors.append(RequestAnchor(config=cfg, text=text, tokens=tokens))
        return anchors

    def _request_cache_context(self, path: str, body: dict) -> RequestCacheContext | None:
        if not self.prefix_cache_enabled or self.prefix_cache_obj is None:
            return None
        prompt_text = self._render_prompt_for_cache(path, body)
        if not prompt_text:
            return None
        tokens = self._tokenize(prompt_text)
        if not tokens:
            return None
        anchors = self._anchors_for_prompt(path, body, prompt_text)
        return RequestCacheContext(prompt_text=prompt_text, prompt_tokens=tokens, anchors=anchors)

    def _materialize_anchor_once(self, anchor: RequestAnchor) -> dict | None:
        cache = self.prefix_cache_obj
        if cache is None:
            return None
        existing = cache.lookup_materialized_anchor(label=anchor.config.label, tokens=anchor.tokens, touch=True)
        if existing:
            return existing
        if not self._ensure_storage_room():
            return None

        node_id, digest = prefix_cache.anchor_node_id_for(anchor.config.label, anchor.tokens)
        if cache.get_node(node_id):
            return cache.lookup_materialized_anchor(label=anchor.config.label, tokens=anchor.tokens, touch=True)

        bin_file = cache.relative_node_bin(node_id)
        bin_path = cache.absolute_bin_path(bin_file)
        if bin_path.exists():
            log.warning("prefix-cache anchor materialize skipped: bin exists without DB node: %s", bin_path)
            return None

        if not _erase_slot(self.slot_id, self.llama_server, self.llama_port):
            return None
        prefill = _call_llama(
            "POST",
            "/completion",
            {"prompt": anchor.text, "n_predict": 0, "stream": False, "cache_prompt": False},
            self.llama_server,
            self.llama_port,
            timeout=600,
        )
        if not isinstance(prefill, dict):
            return None

        tmp_file = f"prefix_anchor_tmp_{time.time_ns()}.bin"
        tmp_path = cache.absolute_bin_path(tmp_file)
        save = _save_slot(self.slot_id, tmp_file, self.llama_server, self.llama_port)
        if not isinstance(save, dict):
            return None
        n_saved = int(save.get("n_saved", -1))
        if n_saved != len(anchor.tokens):
            log.warning("prefix-cache anchor materialize skipped: n_saved=%d anchor_tokens=%d", n_saved, len(anchor.tokens))
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            _erase_slot(self.slot_id, self.llama_server, self.llama_port)
            return None
        if not tmp_path.exists():
            log.warning("prefix-cache anchor materialize skipped: save succeeded but bin is missing: %s", tmp_path)
            return None
        tmp_path.rename(bin_path)
        size_bytes = bin_path.stat().st_size

        props = self._props()
        settings = props.get("default_generation_settings", {}) if isinstance(props, dict) else {}
        node = {
            "id": node_id,
            "parent_id": cache.parent_for(anchor.tokens, node_id),
            "label": anchor.config.label,
            "boundary": "anchor",
            "token_count": len(anchor.tokens),
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
            "pinned": anchor.config.pinned,
            "meta": {
                "source": "lmcache-proxy-on-demand",
                "prefill_response": prefill,
                "anchor_text": anchor.text,
                "anchor_tokens": anchor.tokens,
            },
        }
        cache.insert_node(node)
        cache.insert_anchor({
            "node_id": node_id,
            "label": anchor.config.label,
            "token_count": len(anchor.tokens),
            "prefix_hash": digest,
            "hash_algo": prefix_cache.HASH_ALGO,
            "marker": anchor.config.marker,
            "occurrence": anchor.config.occurrence,
            "side": anchor.config.side,
            "pinned": anchor.config.pinned,
            "created_at": prefix_cache.utc_now(),
            "meta": {
                "materialized": True,
                "anchor_text": anchor.text,
                "anchor_tokens": anchor.tokens,
            },
        })
        log.info("prefix-cache materialized anchor node %s (%d tokens, %.1f MiB)", node_id, len(anchor.tokens), size_bytes / MIB)
        return cache.get_node(node_id)

    @staticmethod
    def _append_newline_to_request_body(path: str, body: dict) -> dict | None:
        modified = copy.deepcopy(body)
        if isinstance(modified.get("prompt"), str):
            modified["prompt"] += "\n"
            return modified

        if not (path.startswith("/v1/chat/completions") or path.startswith("/chat/completions")):
            return None
        messages = modified.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        last = messages[-1]
        if not isinstance(last, dict):
            return None
        content = last.get("content")
        if isinstance(content, str):
            last["content"] = content + "\n"
            return modified
        if isinstance(content, list):
            content.append({"type": "text", "text": "\n"})
            return modified
        return None

    def _maybe_apply_exact_prefix_newline_workaround(
        self,
        path: str,
        body: dict,
        ctx: RequestCacheContext,
    ) -> tuple[dict, RequestCacheContext, bytes | None]:
        cache = self.prefix_cache_obj
        if cache is None or not self.strict_prefix_restore:
            return body, ctx, None

        exact = cache.lookup(ctx.prompt_tokens, touch=False, strictly_less=False)
        if not exact or int(exact.get("token_count", -1)) != len(ctx.prompt_tokens):
            return body, ctx, None

        modified = self._append_newline_to_request_body(path, body)
        if modified is None:
            log.warning(
                "prefix-cache exact-prefix newline workaround unavailable for node %s on %s; falling back to strict-prefix lookup",
                exact.get("id"),
                path,
            )
            return body, ctx, None

        modified_ctx = self._request_cache_context(path, modified)
        if modified_ctx is None:
            return body, ctx, None
        if modified_ctx.prompt_tokens[: len(ctx.prompt_tokens)] != ctx.prompt_tokens:
            log.warning(
                "prefix-cache exact-prefix newline workaround skipped for node %s: appended newline did not preserve token prefix",
                exact.get("id"),
            )
            return body, ctx, None

        modified_ctx.exact_prefix_newline_workaround = True
        modified_ctx.exact_prefix_node_id = str(exact.get("id"))
        log.info(
            "prefix-cache exact-prefix newline workaround: appended newline so node %s is restored as a strict prefix",
            exact.get("id"),
        )
        return modified, modified_ctx, json.dumps(modified, separators=(",", ":")).encode("utf-8")

    def _evict_slot(self, slot_id: int) -> bool:
        """Save the current KV state of a slot before evicting it.

        Saves the slot's current state into the prefix cache so it can be
        restored later. Returns True if the save succeeded.
        """
        cache = self.prefix_cache_obj
        if cache is None:
            return False

        # Read token table from the slot to get the current token count and hash
        tmp_file = f"prefix_evict_tmp_{time.time_ns()}.bin"
        tmp_path = cache.absolute_bin_path(tmp_file)
        save = _save_slot(slot_id, tmp_file, self.llama_server, self.llama_port)
        if not isinstance(save, dict):
            return False
        n_saved = int(save.get("n_saved", -1))
        if n_saved <= 0:
            log.warning("prefix-cache eviction skipped: slot %d has no saved tokens", slot_id)
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            return False

        # Read token IDs from the saved bin to compute the node hash
        try:
            slot_tokens = prefix_cache.read_slot_bin_tokens(tmp_path)
        except Exception as e:
            log.debug("prefix-cache eviction could not parse slot tokens: %s", e)
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            return False

        node_id, digest = prefix_cache.node_id_for(slot_tokens)
        bin_file = cache.relative_node_bin(node_id)
        bin_path = cache.absolute_bin_path(bin_file)

        # Check if this node already exists — if so, just unlink the temp file
        if cache.get_node(node_id) is not None:
            log.debug("prefix-cache eviction skipped: node %s already exists", node_id)
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            return True

        # Move temp file to final location and insert node into trie
        tmp_path.rename(bin_path)
        size_bytes = bin_path.stat().st_size
        props = self._props()
        settings = props.get("default_generation_settings", {}) if isinstance(props, dict) else {}
        parent_id = cache.parent_for(slot_tokens, node_id)
        node = {
            "id": node_id,
            "parent_id": parent_id,
            "label": "eviction",
            "boundary": "auto-eviction",
            "token_count": len(slot_tokens),
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
                "eviction_slot": slot_id,
                "save_response": save,
            },
        }
        cache.insert_node(node)
        log.info("prefix-cache evicted slot %d → node %s (%d tokens, %.1f MiB)",
                 slot_id, node_id, len(slot_tokens), size_bytes / MIB)
        return True

    def _pick_slot_for_restore(self, node: dict | None, req_tokens: int) -> int | None:
        """Pick the best slot to restore a node into.

        In single-slot mode, returns the hardcoded slot_id.
        In multi-slot mode, prefers a slot that already has this node loaded,
        then any idle slot. If no idle slots exist and the match is too short
        to justify overwriting a loaded slot, returns None (caller should skip
        restore and let llama.cpp handle fresh).
        """
        if self.slot_state is None:
            return self.slot_id

        # If we have a node, check if any tracked slot already has it
        if node is not None and node.get("id"):
            for sid in self.slot_state.all_slot_ids():
                if self.slot_state.node_for(sid) == node["id"]:
                    self.slot_state.touch(sid)
                    return sid

        # Otherwise find an idle slot
        idle = self._idle_slots()
        if idle is not None and idle:
            return idle[0]

        # No idle slots — check if the match is long enough to justify overwriting
        # a tracked slot. Threshold: at least min_match_tokens AND at least
        # min_match_ratio of the request's token count (whichever is lower).
        if node is not None:
            node_tok = int(node.get("token_count", 0))
            threshold = max(self.min_match_tokens, int(req_tokens * self.min_match_ratio))
            if node_tok >= threshold:
                # Match is long enough — pick the LRU slot to overwrite
                lru = self.slot_state.lru_slot()
                if lru is not None:
                    # Save the evicted slot's state before overwriting
                    self._evict_slot(lru)
                    self.slot_state.forget(lru)
                    return lru

        # Match too short to justify overwriting — skip restore entirely.
        return None

    def _lookup_and_restore_prefix(self, ctx: RequestCacheContext) -> int | None:
        """Restore the best matching prefix node into a slot.

        Returns the target slot_id (for id_slot injection), or None in single-slot mode.
        In multi-slot mode, may also return None if the match is too short to justify
        overwriting a loaded slot (min_match_tokens / min_match_ratio threshold).
        """
        cache = self.prefix_cache_obj
        if cache is None:
            return None
        cache.init()

        req_tokens = len(ctx.prompt_tokens)

        node = cache.lookup(ctx.prompt_tokens, touch=True, strictly_less=self.strict_prefix_restore)
        via = "full-prefix"
        if node and node.get("boundary") == "anchor":
            via = "materialized-anchor"
        if node:
            slot = self._pick_slot_for_restore(node, req_tokens)
            if slot is None:
                log.debug("prefix-cache match too short to overwrite loaded slot (%d < threshold)",
                          int(node.get("token_count", 0)))
                return None
            result = _restore_slot(slot, node["bin_file"], self.llama_server, self.llama_port)
            if result:
                ctx.restored_node_id = node["id"]
                ctx.restored_via = via
                if self.slot_state is not None:
                    self.slot_state.record(slot, node["id"], int(node.get("token_count", 0)))
                log.info("prefix-cache restored node %s (%s tokens) via %s into slot %d",
                         node["id"], node["token_count"], via, slot)
            return slot if self.slot_state is not None else None

        for anchor in sorted(ctx.anchors, key=lambda a: len(a.tokens), reverse=True):
            anchor_node = cache.lookup_materialized_anchor(label=anchor.config.label,
                                                           tokens=anchor.tokens, touch=True)
            if anchor_node:
                slot = self._pick_slot_for_restore(anchor_node, req_tokens)
                if slot is None:
                    log.debug("prefix-cache anchor match too short to overwrite loaded slot")
                    continue
                result = _restore_slot(slot, anchor_node["bin_file"],
                                       self.llama_server, self.llama_port)
                if result:
                    ctx.restored_node_id = anchor_node["id"]
                    ctx.restored_via = f"anchor:{anchor.config.label}"
                    if self.slot_state is not None:
                        self.slot_state.record(slot, anchor_node["id"],
                                               int(anchor_node.get("token_count", 0)))
                    log.info("prefix-cache restored node %s (%s tokens) via %s into slot %d",
                             anchor_node["id"], anchor_node["token_count"],
                             ctx.restored_via, slot)
                return slot if self.slot_state is not None else None

            node = self._materialize_anchor_once(anchor)
            if node:
                slot = self._pick_slot_for_restore(node, req_tokens)
                if slot is None:
                    log.debug("prefix-cache materialized anchor too short to overwrite loaded slot")
                    continue
                ctx.restored_node_id = node["id"]
                ctx.restored_via = f"anchor-materialized:{anchor.config.label}"
                if self.slot_state is not None:
                    self.slot_state.record(slot, node["id"], int(node.get("token_count", 0)))
                log.info("prefix-cache using newly materialized anchor node %s (%s tokens) into slot %d",
                         node["id"], node["token_count"], slot)
                return slot if self.slot_state is not None else None

        # No match found — in multi-slot mode, try to find an idle slot anyway
        if self.slot_state is not None:
            idle = self._idle_slots()
            if idle is not None and idle:
                log.debug("prefix-cache no node match; using idle slot %d", idle[0])
                return idle[0]

        return None

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

    def _ensure_storage_room(self, reserve_bytes: int = 0) -> bool:
        cache = self.prefix_cache_obj
        if cache is None:
            return False
        cache.init()
        reserve_bytes = max(0, int(reserve_bytes))
        target_max_bytes = None if self.max_cache_bytes is None else max(self.max_cache_bytes - reserve_bytes, 0)
        cache.prune_global(max_bytes=target_max_bytes, max_nodes=None, dry_run=False)

        total = cache.total_bytes_global()
        if self.max_cache_bytes is not None and total + reserve_bytes > self.max_cache_bytes:
            log.warning(
                "prefix-cache autosave skipped: could not reserve %d bytes within max cache budget %d (current=%d)",
                reserve_bytes,
                self.max_cache_bytes,
                total,
            )
            return False

        required_free = self.min_free_bytes + reserve_bytes
        while True:
            free = shutil.disk_usage(cache.cache_dir).free
            if free >= required_free:
                return True
            total = cache.total_bytes_global()
            if total <= 0:
                log.warning(
                    "prefix-cache autosave skipped: free disk %d bytes below required %d and no cache is prunable",
                    free,
                    required_free,
                )
                return False
            removed = cache.prune_global(max_bytes=max(total - 1, 0), max_nodes=None, dry_run=False)
            if not removed:
                log.warning(
                    "prefix-cache autosave skipped: free disk %d bytes below required %d and no leaf cache node is prunable",
                    free,
                    required_free,
                )
                return False
            log.info("prefix-cache pruned %d node(s) to recover low disk space", len(removed))

    def _props(self) -> dict:
        try:
            props = _call_llama("GET", "/props", None, self.llama_server, self.llama_port, timeout=30)
        except Exception as e:
            log.debug("/props lookup failed: %s", e)
            return {}
        return props if isinstance(props, dict) else {}

    def _discover_slots(self) -> list[dict] | None:
        """Query llama.cpp /slots endpoint to discover available slots.

        Returns a list of slot dicts, or None on failure. In single-slot mode
        (n_parallel is None), returns None so the proxy falls back to the
        hardcoded slot_id.
        """
        if self.n_parallel is None:
            return None
        try:
            url = f"http://{self.llama_server}:{self.llama_port}/slots"
            with urllib.request.urlopen(url, timeout=10) as resp:
                slots = json.loads(resp.read().decode())
                if isinstance(slots, list):
                    return slots
        except Exception as e:
            log.debug("/slots discovery failed: %s", e)
        return None

    def _idle_slots(self) -> list[int] | None:
        """Return ids of slots that are idle (not busy), or None in single-slot mode."""
        slots = self._discover_slots()
        if slots is None:
            return None
        return [s["id"] for s in slots if not s.get("is_busy", False)]

    @staticmethod
    def _text_fragments_from_event(obj) -> list[str]:
        fragments: list[str] = []
        if not isinstance(obj, dict):
            return fragments

        choices = obj.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    for key in ("reasoning_content", "content", "text"):
                        value = delta.get(key)
                        if isinstance(value, str):
                            fragments.append(value)
                message = choice.get("message")
                if isinstance(message, dict):
                    for key in ("reasoning_content", "content"):
                        value = message.get(key)
                        if isinstance(value, str):
                            fragments.append(value)
                value = choice.get("text")
                if isinstance(value, str):
                    fragments.append(value)

        for key in ("content", "response", "text"):
            value = obj.get(key)
            if isinstance(value, str):
                fragments.append(value)
        return fragments

    @classmethod
    def _captured_response_text(cls, result: ForwardResult) -> str | None:
        fragments: list[str] = []
        body_text = result.body.decode("utf-8", errors="replace")

        if "text/event-stream" in result.content_type:
            for line in body_text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                fragments.extend(cls._text_fragments_from_event(event))
        else:
            try:
                obj = json.loads(body_text) if body_text else {}
            except json.JSONDecodeError:
                obj = {}
            fragments.extend(cls._text_fragments_from_event(obj))

        text = "".join(fragments)
        return text if text else None

    @staticmethod
    def _lcp_len(left: list[int], right: list[int]) -> int:
        limit = min(len(left), len(right))
        i = 0
        while i < limit and left[i] == right[i]:
            i += 1
        return i

    def _verified_generated_prefix_tokens(
        self,
        ctx: RequestCacheContext,
        result: ForwardResult,
        slot_tokens: list[int] | None,
    ) -> list[int] | None:
        if not self.generated_prefix_enabled or not slot_tokens or len(slot_tokens) <= len(ctx.prompt_tokens):
            return None
        if slot_tokens[: len(ctx.prompt_tokens)] != ctx.prompt_tokens:
            log.warning("prefix-cache generated-node skipped: saved slot tokens do not start with prompt tokens")
            return None

        response_text = self._captured_response_text(result)
        if not response_text:
            return None
        optimistic_tokens = self._tokenize(ctx.prompt_text + response_text)
        if not optimistic_tokens:
            return None

        lcp = self._lcp_len(optimistic_tokens, slot_tokens)
        if lcp <= len(ctx.prompt_tokens):
            log.debug(
                "prefix-cache generated-node skipped: captured response verified only %d/%d prompt tokens",
                lcp,
                len(ctx.prompt_tokens),
            )
            return None
        return slot_tokens[:lcp]

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
        props = self._props()
        settings = props.get("default_generation_settings", {}) if isinstance(props, dict) else {}
        response_text = self._captured_response_text(result)
        expected_n_saved = len(tokens)
        if response_text:
            optimistic_tokens = self._tokenize(ctx.prompt_text + response_text)
            if optimistic_tokens and len(optimistic_tokens) > expected_n_saved:
                expected_n_saved = len(optimistic_tokens)
        estimated_size = cache.estimate_save_size_bytes(
            expected_n_saved,
            model_alias=props.get("model_alias") if isinstance(props, dict) else None,
            model_path=props.get("model_path") if isinstance(props, dict) else None,
            ctx_size=settings.get("n_ctx") if isinstance(settings, dict) else None,
        )
        reserve_bytes = int(estimated_size * 1.3) if estimated_size is not None else 0
        if reserve_bytes > 0:
            log.info(
                "prefix-cache reserving %.1f MiB before autosave (%d expected saved tokens, 30%% margin)",
                reserve_bytes / MIB,
                expected_n_saved,
            )
        if not self._ensure_storage_room(reserve_bytes=reserve_bytes):
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

        slot_tokens: list[int] | None = None
        try:
            slot_tokens = prefix_cache.read_slot_bin_tokens(tmp_path)
        except Exception as e:
            log.debug("prefix-cache autosave could not parse slot token table for generated node: %s", e)
        if slot_tokens is not None and slot_tokens[: len(tokens)] != tokens:
            log.warning("prefix-cache autosave skipped: saved slot token prefix does not match rendered prompt tokens")
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            return

        node_id, digest = prefix_cache.node_id_for(tokens)
        prompt_exists = cache.get_node(node_id) is not None

        generated_tokens = self._verified_generated_prefix_tokens(ctx, result, slot_tokens)
        generated_node_id = None
        generated_digest = None
        generated_exists = True
        if generated_tokens is not None:
            generated_node_id, generated_digest = prefix_cache.node_id_for(generated_tokens)
            generated_exists = cache.get_node(generated_node_id) is not None

        if prompt_exists and (generated_node_id is None or generated_exists):
            log.debug("prefix-cache autosave skipped: node already exists %s", node_id)
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            return

        primary_bin_node_id = node_id if not prompt_exists else generated_node_id
        if primary_bin_node_id is None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            return
        bin_file = cache.relative_node_bin(primary_bin_node_id)
        bin_path = cache.absolute_bin_path(bin_file)
        if bin_path.exists():
            log.warning("prefix-cache autosave skipped: bin exists without expected DB node: %s", bin_path)
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            return
        tmp_path.rename(bin_path)
        size_bytes = bin_path.stat().st_size

        common_meta = {
            "source": "lmcache-proxy-on-demand",
            "restored_node_id": ctx.restored_node_id,
            "save_response": save,
            "response_content_type": result.content_type,
            "exact_prefix_newline_workaround": ctx.exact_prefix_newline_workaround,
            "exact_prefix_node_id": ctx.exact_prefix_node_id,
        }
        inserted_nodes: list[str] = []

        if not prompt_exists:
            parent_id = cache.parent_for(tokens, node_id)
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
                "meta": common_meta,
            }
            cache.insert_node(node)
            inserted_nodes.append(node_id)
            for anchor in ctx.anchors:
                anchor_id, anchor_digest = prefix_cache.node_id_for(anchor.tokens)
                cache.insert_anchor({
                    "node_id": node_id,
                    "label": anchor.config.label,
                    "token_count": len(anchor.tokens),
                    "prefix_hash": anchor_digest,
                    "hash_algo": prefix_cache.HASH_ALGO,
                    "marker": anchor.config.marker,
                    "occurrence": anchor.config.occurrence,
                    "side": anchor.config.side,
                    "pinned": anchor.config.pinned,
                    "created_at": prefix_cache.utc_now(),
                    "meta": {
                        "anchor_id": anchor_id,
                        "anchor_text": anchor.text,
                        "anchor_tokens": anchor.tokens,
                    },
                })

        if generated_tokens is not None and generated_node_id is not None and generated_digest is not None and not generated_exists:
            generated_node = {
                "id": generated_node_id,
                "parent_id": node_id if cache.get_node(node_id) else cache.parent_for(generated_tokens, generated_node_id),
                "label": "auto-generated",
                "boundary": "auto-generated-response",
                "token_count": len(generated_tokens),
                "prefix_hash": generated_digest,
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
                    **common_meta,
                    "prompt_node_id": node_id,
                    "prompt_token_count": len(tokens),
                    "verified_generated_tokens": len(generated_tokens) - len(tokens),
                    "slot_token_count": len(slot_tokens) if slot_tokens is not None else None,
                },
            }
            cache.insert_node(generated_node)
            inserted_nodes.append(generated_node_id)

        cache.prune_global(max_bytes=self.max_cache_bytes, max_nodes=None, dry_run=False)
        log.info(
            "prefix-cache autosaved %d node(s): prompt=%s generated=%s (%d→%d saved tokens, %.1f MiB, anchors=%d)",
            len(inserted_nodes),
            node_id if not prompt_exists else "exists",
            generated_node_id if generated_node_id and not generated_exists else None,
            len(tokens),
            n_saved,
            size_bytes / MIB,
            len(ctx.anchors),
        )

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

        target_slot: int | None = None
        ctx = self._request_cache_context(path, body) if method == "POST" else None
        if ctx is not None:
            modified_body, modified_ctx, modified_body_bytes = self._maybe_apply_exact_prefix_newline_workaround(path, body, ctx)
            if modified_body_bytes is not None:
                body = modified_body
                body_bytes = modified_body_bytes
                ctx = modified_ctx
            target_slot = self._lookup_and_restore_prefix(ctx)
        else:
            self._restore_legacy_cache(body)

        # Inject id_slot when multi-slot routing is enabled and we have a target slot
        if target_slot is not None:
            body["id_slot"] = target_slot
            body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
            log.debug("routed to slot %d", target_slot)

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
    parser.add_argument("--parallel", type=int, default=None,
        help="Number of llama.cpp slots (--parallel). When set, enables multi-slot routing; "
             "without it the proxy uses a single hardcoded slot_id")
    parser.add_argument("--min-save-tokens", type=int, default=256, help="Minimum tokens before autosaving a prefix node")
    parser.add_argument("--min-match-tokens", type=int, default=5000,
        help="Minimum token match length to overwrite a loaded slot (default: 5000)")
    parser.add_argument("--min-match-ratio", type=float, default=0.8,
        help="Minimum fraction of request tokens a match must cover (default: 0.8)")
    parser.add_argument("--prefix-cache-max-bytes", type=parse_bytes, default=8 * GIB, help="Max prefix cache bytes before pruning (default: 8GiB)")
    parser.add_argument("--prefix-cache-min-free-bytes", type=parse_bytes, default=512 * MIB, help="Minimum filesystem free bytes before autosave (default: 512MiB)")
    parser.add_argument("--no-prefix-cache", action="store_true", help="Disable trie-backed prefix cache")
    parser.add_argument("--no-auto-save", action="store_true", help="Disable autosaving completed requests into prefix cache")
    parser.add_argument("--no-generated-prefix-cache", action="store_true", help="Disable optimistic generated-response prefix nodes after stream completion")
    parser.add_argument("--allow-exact-prefix-restore", action="store_true", help="Allow restoring exact-length prefix matches (currently unsafe on this llama.cpp build)")
    args = parser.parse_args()

    cache_dir = pathlib.Path(args.cache_dir).expanduser()
    legacy_cache = KVCache(str(cache_dir), top_k=args.top_k)
    trie_cache = None if args.no_prefix_cache else prefix_cache.PrefixCache(cache_dir)
    anchor_configs: list[AnchorConfig] = []
    if trie_cache is not None:
        trie_cache.init()
        anchor_configs = [
            AnchorConfig(
                label=str(row["label"]),
                marker=str(row["marker"]),
                occurrence=int(row["occurrence"]),
                side=str(row["side"]),
                pinned=bool(row["pinned"]),
            )
            for row in trie_cache.list_anchor_configs()
        ]

    LMCacheHandler.llama_server = args.server
    LMCacheHandler.llama_port = args.llama_port
    LMCacheHandler.proxy_port = args.port
    LMCacheHandler.slot_id = args.slot
    LMCacheHandler.cache_dir_obj = legacy_cache
    LMCacheHandler.prefix_cache_obj = trie_cache
    LMCacheHandler.prefix_cache_enabled = trie_cache is not None
    LMCacheHandler.auto_save_enabled = not args.no_auto_save
    LMCacheHandler.generated_prefix_enabled = not args.no_generated_prefix_cache
    LMCacheHandler.min_save_tokens = args.min_save_tokens
    LMCacheHandler.min_match_tokens = args.min_match_tokens
    LMCacheHandler.min_match_ratio = args.min_match_ratio
    LMCacheHandler.max_cache_bytes = args.prefix_cache_max_bytes
    LMCacheHandler.min_free_bytes = args.prefix_cache_min_free_bytes
    LMCacheHandler.strict_prefix_restore = not args.allow_exact_prefix_restore
    LMCacheHandler.anchor_configs = anchor_configs

    # Multi-slot routing: enabled when --parallel is set.
    if args.parallel is not None and args.parallel > 1:
        LMCacheHandler.n_parallel = args.parallel
        LMCacheHandler.slot_state = SlotState()
        log.info("multi-slot routing enabled with %d slots", args.parallel)
    else:
        LMCacheHandler.n_parallel = None
        LMCacheHandler.slot_state = None
        log.info("single-slot mode (slot_id=%d)", args.slot)

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
            "prefix cache enabled: min_save_tokens=%d max_bytes=%d min_free_bytes=%d strict_prefix_restore=%s auto_save=%s generated_prefix=%s anchors=%d",
            args.min_save_tokens,
            args.prefix_cache_max_bytes,
            args.prefix_cache_min_free_bytes,
            not args.allow_exact_prefix_restore,
            not args.no_auto_save,
            not args.no_generated_prefix_cache,
            len(anchor_configs),
        )

    try:
        server = HTTPServer((args.host, args.port), LMCacheHandler)
        log.info("proxy ready — clients should point to port %d", args.port)
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
