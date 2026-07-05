#!/usr/bin/env python3
"""Disk-backed prefix-trie metadata for llama.cpp slot KV snapshots.

This is intentionally a small standalone tool:
- SQLite stores prefix-node metadata.
- llama.cpp-compatible .bin files stay on the filesystem because the service
  save/restore API works with filenames.
- Prefix identity is a deterministic token hash:
  BLAKE2b-128 over little-endian uint32 token IDs, streamed with O(1) memory.

The static/manual path (`slot_0_current.bin`) is separate and not managed here.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import logging
import os
import pathlib
import sqlite3
import struct
import sys
import urllib.error
import urllib.request
from typing import Any, Iterable


DEFAULT_CACHE_DIR = pathlib.Path("~/.cache/llama.cpp-launch-scripts/slot-kv").expanduser()
DEFAULT_BASE_URL = "http://127.0.0.1:8081"
HASH_ALGO = "blake2b-128-le-u32-v1"
_PACK_U32 = struct.Struct("<I")
log = logging.getLogger(__name__)


def utc_now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def hash_tokens(tokens: Iterable[int], *, digest_size: int = 16) -> str:
    """Hash token IDs with O(1) extra memory.

    Format is portable across platforms: each token is packed as little-endian
    uint32 before being streamed into BLAKE2b.
    """
    h = hashlib.blake2b(digest_size=digest_size)
    buf = bytearray(4)
    for token in tokens:
        if token < 0 or token > 0xFFFFFFFF:
            raise ValueError(f"token id out of uint32 range: {token}")
        _PACK_U32.pack_into(buf, 0, token)
        h.update(buf)
    return h.hexdigest()


def prefix_hashes(tokens: list[int], lengths: Iterable[int]) -> dict[int, str]:
    """Compute hashes for requested prefix lengths in one pass.

    Complexity: O(N + L) time, O(L) memory, where L is the number of requested
    prefix lengths.
    """
    wanted = {int(n) for n in lengths if int(n) > 0 and int(n) <= len(tokens)}
    if not wanted:
        return {}

    out: dict[int, str] = {}
    h = hashlib.blake2b(digest_size=16)
    buf = bytearray(4)
    for i, token in enumerate(tokens, 1):
        if token < 0 or token > 0xFFFFFFFF:
            raise ValueError(f"token id out of uint32 range: {token}")
        _PACK_U32.pack_into(buf, 0, token)
        h.update(buf)
        if i in wanted:
            out[i] = h.hexdigest()
            if len(out) == len(wanted):
                break
    return out


def node_id_for(tokens: list[int]) -> tuple[str, str]:
    digest = hash_tokens(tokens)
    return f"{len(tokens)}-{digest}", digest


def anchor_node_id_for(label: str, tokens: list[int]) -> tuple[str, str]:
    base_id, digest = node_id_for(tokens)
    label_hash = hashlib.blake2b(label.encode("utf-8"), digest_size=4).hexdigest()
    return f"anchor-{label_hash}-{base_id}", digest


def read_slot_bin_tokens(path: pathlib.Path) -> list[int]:
    """Read token IDs from a llama.cpp slot-save .bin file.

    Current llama.cpp slot files begin with:
      - magic: 4 bytes, b"qsgg"
      - version: uint32 little-endian
      - n_saved: uint32 little-endian
      - n_saved token IDs as uint32 little-endian

    The KV tensors follow the token table and are intentionally ignored here.
    """
    with path.open("rb") as f:
        header = f.read(12)
        if len(header) != 12:
            raise ValueError(f"slot bin too short: {path}")
        magic, version, n_saved = struct.unpack("<4sII", header)
        if magic != b"qsgg":
            raise ValueError(f"unsupported slot bin magic {magic!r}: {path}")
        if version <= 0:
            raise ValueError(f"unsupported slot bin version {version}: {path}")
        raw = f.read(n_saved * 4)
        if len(raw) != n_saved * 4:
            raise ValueError(f"slot bin token table truncated: expected {n_saved} tokens in {path}")
    if n_saved == 0:
        return []
    return list(struct.unpack(f"<{n_saved}I", raw))


class LlamaClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def get_json(self, path: str, *, timeout: float = 30) -> Any:
        req = urllib.request.Request(self.base_url + path, headers={"X-LMCache-Bypass": "1"}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post_json(self, path: str, body: dict[str, Any], *, timeout: float = 180) -> Any:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={
                "Content-Type": "application/json",
                # If this client is pointed at lmcache-proxy-on-demand instead
                # of the backend, management calls must not recursively trigger
                # automatic prefix-cache lookup/save.
                "X-LMCache-Bypass": "1",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def tokenize(self, prompt: str) -> list[int]:
        res = self.post_json("/tokenize", {"content": prompt}, timeout=120)
        tokens = res.get("tokens")
        if not isinstance(tokens, list):
            raise RuntimeError(f"unexpected /tokenize response: {res!r}")
        return [int(t) for t in tokens]

    def props(self) -> dict[str, Any]:
        try:
            return self.get_json("/props", timeout=10)
        except Exception:
            return {}

    def erase_slot(self, slot: int) -> dict[str, Any]:
        return self.post_json(f"/slots/{slot}?action=erase", {"n_keep": 0}, timeout=180)

    def save_slot(self, slot: int, filename: str) -> dict[str, Any]:
        return self.post_json(f"/slots/{slot}?action=save", {"filename": filename}, timeout=300)

    def restore_slot(self, slot: int, filename: str) -> dict[str, Any]:
        return self.post_json(f"/slots/{slot}?action=restore", {"filename": filename}, timeout=300)

    def prefill_completion(self, prompt: str) -> dict[str, Any]:
        # llama.cpp may report one predicted token for n_predict=0, but slot save
        # has been observed to save exactly the prompt token count.
        return self.post_json(
            "/completion",
            {"prompt": prompt, "n_predict": 0, "stream": False, "cache_prompt": False},
            timeout=600,
        )


class PrefixCache:
    def __init__(self, cache_dir: pathlib.Path):
        self.cache_dir = cache_dir.expanduser()
        self.cache_root = self.cache_dir.parent
        self.trie_dir = self.cache_dir / "trie"
        self.nodes_dir = self.trie_dir / "nodes"
        self.db_path = self.trie_dir / "prefix-cache.sqlite"

    def init(self) -> None:
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self.connect()) as db:
            db.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
                    label TEXT NOT NULL DEFAULT '',
                    boundary TEXT NOT NULL DEFAULT 'manual',
                    token_count INTEGER NOT NULL,
                    prefix_hash TEXT NOT NULL,
                    hash_algo TEXT NOT NULL,
                    bin_file TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    n_saved INTEGER NOT NULL,
                    model_alias TEXT,
                    model_path TEXT,
                    ctx_size INTEGER,
                    hits INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_used TEXT,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    meta_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_nodes_token_hash ON nodes(token_count, prefix_hash);
                CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
                CREATE INDEX IF NOT EXISTS idx_nodes_prune ON nodes(pinned, last_used, hits, size_bytes);

                CREATE TABLE IF NOT EXISTS anchors (
                    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    prefix_hash TEXT NOT NULL,
                    hash_algo TEXT NOT NULL,
                    marker TEXT NOT NULL,
                    occurrence INTEGER NOT NULL,
                    side TEXT NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (label, token_count, prefix_hash, node_id)
                );
                CREATE INDEX IF NOT EXISTS idx_anchors_lookup ON anchors(label, token_count, prefix_hash);
                CREATE INDEX IF NOT EXISTS idx_anchors_node ON anchors(node_id);

                CREATE TABLE IF NOT EXISTS anchor_configs (
                    label TEXT PRIMARY KEY,
                    marker TEXT NOT NULL,
                    occurrence INTEGER NOT NULL,
                    side TEXT NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            db.execute(
                """
                INSERT OR IGNORE INTO anchor_configs (
                    label, marker, occurrence, side, pinned, enabled, created_at, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "end-of-system-message",
                    "<|im_end|>",
                    1,
                    "after",
                    0,
                    1,
                    utc_now(),
                    json.dumps({"description": "prefix through first chat-template end-of-message marker"}, sort_keys=True),
                ),
            )
            db.execute(
                "UPDATE anchor_configs SET pinned = 0 WHERE label = ?",
                ("end-of-system-message",),
            )
            db.execute(
                "UPDATE anchors SET pinned = 0 WHERE label = ?",
                ("end-of-system-message",),
            )
            db.execute(
                "UPDATE nodes SET pinned = 0 WHERE boundary = 'anchor' AND label = ?",
                ("end-of-system-message",),
            )
            db.commit()

    def connect(self) -> sqlite3.Connection:
        self.trie_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def relative_node_bin(self, node_id: str) -> str:
        # llama.cpp's slot save/restore API is conservative about filenames and
        # may reject path separators even inside --slot-save-path. Keep node bins
        # flat in cache_dir; SQLite metadata still carries trie relationships.
        return f"prefix_node_{node_id}.bin"

    def absolute_bin_path(self, bin_file: str) -> pathlib.Path:
        return self.cache_dir / bin_file

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        with contextlib.closing(self.connect()) as db:
            row = db.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
            return dict(row) if row else None

    def lengths_leq(self, token_count: int) -> list[int]:
        with contextlib.closing(self.connect()) as db:
            rows = db.execute(
                "SELECT DISTINCT token_count FROM nodes WHERE token_count <= ? ORDER BY token_count",
                (token_count,),
            ).fetchall()
        return [int(r[0]) for r in rows]

    @staticmethod
    def touch_node_in_db(db: sqlite3.Connection, node: dict[str, Any]) -> None:
        now = utc_now()
        db.execute(
            "UPDATE nodes SET hits = hits + 1, last_used = ? WHERE id = ?",
            (now, node["id"]),
        )
        node["hits"] = int(node["hits"]) + 1
        node["last_used"] = now

    def lookup(self, tokens: list[int], *, touch: bool = False, strictly_less: bool = False) -> dict[str, Any] | None:
        max_len = len(tokens) - 1 if strictly_less else len(tokens)
        if max_len <= 0:
            return None
        lengths = self.lengths_leq(max_len)
        hashes = prefix_hashes(tokens, lengths)
        if not hashes:
            return None

        with contextlib.closing(self.connect()) as db:
            best: sqlite3.Row | None = None
            for length in sorted(hashes, reverse=True):
                row = db.execute(
                    "SELECT * FROM nodes WHERE token_count = ? AND prefix_hash = ? LIMIT 1",
                    (length, hashes[length]),
                ).fetchone()
                if row:
                    best = row
                    break

            if not best:
                return None

            node = dict(best)
            if touch:
                self.touch_node_in_db(db, node)
                db.commit()
            return node

    def list_anchor_configs(self) -> list[dict[str, Any]]:
        self.init()
        with contextlib.closing(self.connect()) as db:
            rows = db.execute(
                """
                SELECT label, marker, occurrence, side, pinned, enabled, created_at, meta_json
                FROM anchor_configs
                WHERE enabled = 1
                ORDER BY label
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_anchor(self, anchor: dict[str, Any]) -> None:
        with contextlib.closing(self.connect()) as db:
            db.execute(
                """
                INSERT OR IGNORE INTO anchors (
                    node_id, label, token_count, prefix_hash, hash_algo, marker,
                    occurrence, side, pinned, created_at, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    anchor["node_id"],
                    anchor["label"],
                    int(anchor["token_count"]),
                    anchor["prefix_hash"],
                    anchor.get("hash_algo", HASH_ALGO),
                    anchor.get("marker", ""),
                    int(anchor.get("occurrence", 0)),
                    anchor.get("side", "after"),
                    1 if anchor.get("pinned") else 0,
                    anchor.get("created_at", utc_now()),
                    json.dumps(anchor.get("meta", {}), sort_keys=True),
                ),
            )
            db.commit()

    def lookup_materialized_anchor(self, *, label: str, tokens: list[int], touch: bool = False) -> dict[str, Any] | None:
        if not tokens:
            return None
        digest = hash_tokens(tokens)
        with contextlib.closing(self.connect()) as db:
            row = db.execute(
                """
                SELECT n.*, a.label AS anchor_label, a.token_count AS anchor_token_count,
                       a.prefix_hash AS anchor_prefix_hash, a.marker AS anchor_marker,
                       a.occurrence AS anchor_occurrence, a.side AS anchor_side,
                       a.pinned AS anchor_pinned
                FROM anchors a
                JOIN nodes n ON n.id = a.node_id
                WHERE a.label = ?
                  AND a.token_count = ?
                  AND a.prefix_hash = ?
                  AND n.boundary = 'anchor'
                  AND n.token_count = a.token_count
                  AND n.prefix_hash = a.prefix_hash
                ORDER BY n.hits DESC, COALESCE(n.last_used, n.created_at) DESC, n.size_bytes ASC
                LIMIT 1
                """,
                (label, len(tokens), digest),
            ).fetchone()
            if not row:
                return None
            node = dict(row)
            if touch:
                self.touch_node_in_db(db, node)
                db.commit()
            return node

    def lookup_anchor(self, *, label: str, tokens: list[int], touch: bool = False) -> dict[str, Any] | None:
        if not tokens:
            return None
        digest = hash_tokens(tokens)
        with contextlib.closing(self.connect()) as db:
            row = db.execute(
                """
                SELECT n.*, a.label AS anchor_label, a.token_count AS anchor_token_count,
                       a.prefix_hash AS anchor_prefix_hash, a.marker AS anchor_marker,
                       a.occurrence AS anchor_occurrence, a.side AS anchor_side,
                       a.pinned AS anchor_pinned
                FROM anchors a
                JOIN nodes n ON n.id = a.node_id
                WHERE a.label = ? AND a.token_count = ? AND a.prefix_hash = ?
                ORDER BY n.hits DESC, COALESCE(n.last_used, n.created_at) DESC, n.size_bytes ASC
                LIMIT 1
                """,
                (label, len(tokens), digest),
            ).fetchone()
            if not row:
                return None
            node = dict(row)
            if touch:
                self.touch_node_in_db(db, node)
                db.commit()
            return node

    def parent_for(self, tokens: list[int], node_id: str) -> str | None:
        lengths = [n for n in self.lengths_leq(len(tokens)) if n < len(tokens)]
        hashes = prefix_hashes(tokens, lengths)
        if not hashes:
            return None
        with contextlib.closing(self.connect()) as db:
            for length in sorted(hashes, reverse=True):
                row = db.execute(
                    "SELECT id FROM nodes WHERE token_count = ? AND prefix_hash = ? AND id != ? LIMIT 1",
                    (length, hashes[length], node_id),
                ).fetchone()
                if row:
                    return str(row[0])
        return None

    def insert_node(self, node: dict[str, Any]) -> None:
        with contextlib.closing(self.connect()) as db:
            db.execute(
                """
                INSERT INTO nodes (
                    id, parent_id, label, boundary, token_count, prefix_hash, hash_algo,
                    bin_file, size_bytes, n_saved, model_alias, model_path, ctx_size,
                    hits, created_at, last_used, pinned, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node["id"],
                    node.get("parent_id"),
                    node.get("label", ""),
                    node.get("boundary", "manual"),
                    node["token_count"],
                    node["prefix_hash"],
                    node.get("hash_algo", HASH_ALGO),
                    node["bin_file"],
                    node["size_bytes"],
                    node["n_saved"],
                    node.get("model_alias"),
                    node.get("model_path"),
                    node.get("ctx_size"),
                    node.get("hits", 0),
                    node["created_at"],
                    node.get("last_used") or utc_now(),
                    1 if node.get("pinned") else 0,
                    json.dumps(node.get("meta", {}), sort_keys=True),
                ),
            )
            db.commit()

    def list_nodes(self) -> list[dict[str, Any]]:
        with contextlib.closing(self.connect()) as db:
            rows = db.execute(
                "SELECT * FROM nodes ORDER BY token_count ASC, created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def total_bytes(self) -> int:
        with contextlib.closing(self.connect()) as db:
            row = db.execute(
                """
                SELECT COALESCE(SUM(size_bytes), 0)
                FROM (SELECT bin_file, MAX(size_bytes) AS size_bytes FROM nodes GROUP BY bin_file)
                """
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _total_bytes_in_db(db: sqlite3.Connection) -> int:
        row = db.execute(
            """
            SELECT COALESCE(SUM(size_bytes), 0)
            FROM (SELECT bin_file, MAX(size_bytes) AS size_bytes FROM nodes GROUP BY bin_file)
            """
        ).fetchone()
        return int(row[0])

    @classmethod
    def discover(cls, cache_root: pathlib.Path) -> list["PrefixCache"]:
        root = cache_root.expanduser()
        out: list[PrefixCache] = []
        for db_path in sorted(root.glob("*/trie/prefix-cache.sqlite")):
            cache = cls(db_path.parent.parent)
            cache.init()
            out.append(cache)
        return out

    def total_bytes_global(self) -> int:
        total = 0
        for cache in self.discover(self.cache_root):
            if not cache.db_path.exists():
                continue
            with contextlib.closing(cache.connect()) as db:
                total += self._total_bytes_in_db(db)
        return total

    def estimate_save_size_bytes(
        self,
        expected_n_saved: int,
        *,
        model_alias: str | None = None,
        model_path: str | None = None,
        ctx_size: int | None = None,
    ) -> int | None:
        if expected_n_saved <= 0:
            return None

        samples: list[tuple[int, int]] = []
        for cache in self.discover(self.cache_root):
            if not cache.db_path.exists():
                continue
            with contextlib.closing(cache.connect()) as db:
                where = ["n_saved > 0", "size_bytes > 0"]
                params: list[Any] = []
                if model_path:
                    where.append("model_path = ?")
                    params.append(model_path)
                elif model_alias:
                    where.append("model_alias = ?")
                    params.append(model_alias)
                if ctx_size is not None:
                    where.append("ctx_size = ?")
                    params.append(int(ctx_size))
                rows = db.execute(
                    f"SELECT MAX(n_saved) AS n_saved, MAX(size_bytes) AS size_bytes FROM nodes WHERE {' AND '.join(where)} GROUP BY bin_file"
                    ,
                    params,
                ).fetchall()
                for row in rows:
                    n_saved = int(row[0])
                    size_bytes = int(row[1])
                    if n_saved > 0 and size_bytes > 0:
                        samples.append((n_saved, size_bytes))

        samples = sorted(set(samples))
        if len(samples) < 2:
            return None

        xs = [float(x) for x, _ in samples]
        ys = [float(y) for _, y in samples]
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        denom = sum((x - x_mean) ** 2 for x in xs)
        slope = 0.0 if denom <= 0 else sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom
        slope = max(0.0, slope)
        intercept = max(0.0, y_mean - slope * x_mean)
        regression_estimate = intercept + slope * float(expected_n_saved)

        local_estimate = ys[0]
        if expected_n_saved <= samples[0][0]:
            local_estimate = ys[0]
        elif expected_n_saved >= samples[-1][0]:
            x1, y1 = samples[-2]
            x2, y2 = samples[-1]
            tail_slope = 0.0 if x2 <= x1 else max(0.0, (float(y2) - float(y1)) / float(x2 - x1))
            local_estimate = float(y2) + tail_slope * float(expected_n_saved - x2)
        else:
            for (x1, y1), (x2, y2) in zip(samples, samples[1:]):
                if x1 <= expected_n_saved <= x2:
                    if x2 == x1:
                        local_estimate = float(max(y1, y2))
                    else:
                        ratio = float(expected_n_saved - x1) / float(x2 - x1)
                        local_estimate = float(y1) + ratio * float(y2 - y1)
                    break

        return max(1, int(max(regression_estimate, local_estimate)))

    @staticmethod
    def _prune_candidates_query() -> str:
        return """
            SELECT
              n.*,
              (SELECT COUNT(*) FROM nodes r WHERE r.bin_file = n.bin_file) AS bin_refs
            FROM nodes n
            LEFT JOIN nodes c ON c.parent_id = n.id
            WHERE c.id IS NULL AND n.pinned = 0
            ORDER BY
              COALESCE(n.last_used, n.created_at) ASC,
              n.id ASC
        """

    def prune_global(self, *, max_bytes: int | None, max_nodes: int | None, dry_run: bool) -> list[dict[str, Any]]:
        removed: list[dict[str, Any]] = []
        while True:
            caches = [cache for cache in self.discover(self.cache_root) if cache.db_path.exists()]
            total_bytes = 0
            total_nodes = 0
            per_cache_candidates: list[tuple[tuple[str, str, str], PrefixCache, dict[str, Any]]] = []

            for cache in caches:
                with contextlib.closing(cache.connect()) as db:
                    total_bytes += self._total_bytes_in_db(db)
                    total_nodes += int(db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
                    row = db.execute(self._prune_candidates_query() + " LIMIT 1").fetchone()
                    if row:
                        candidate = dict(row)
                        sort_key = (
                            str(candidate.get("last_used") or candidate.get("created_at") or ""),
                            str(candidate.get("id") or ""),
                            str(cache.cache_dir),
                        )
                        per_cache_candidates.append((sort_key, cache, candidate))

            over_bytes = max_bytes is not None and total_bytes > max_bytes
            over_nodes = max_nodes is not None and total_nodes > max_nodes
            if not over_bytes and not over_nodes:
                break

            if not per_cache_candidates:
                log.warning(
                    "prefix-cache global prune wanted but found no removable leaf nodes: %s",
                    json.dumps(
                        {
                            "scope": "global",
                            "cache_root": str(self.cache_root),
                            "total_bytes": total_bytes,
                            "total_nodes": total_nodes,
                            "max_bytes": max_bytes,
                            "max_nodes": max_nodes,
                            "over_bytes": over_bytes,
                            "over_nodes": over_nodes,
                            "dry_run": dry_run,
                            "candidates": [],
                        },
                        sort_keys=True,
                    ),
                )
                break

            per_cache_candidates.sort(key=lambda item: item[0])
            _, chosen_cache, chosen = per_cache_candidates[0]
            decision_data = []
            for rank, (_, cache, candidate) in enumerate(per_cache_candidates, 1):
                decision_data.append(
                    {
                        "rank": rank,
                        "selected": rank == 1,
                        "cache_dir": str(cache.cache_dir),
                        "id": candidate["id"],
                        "boundary": candidate.get("boundary"),
                        "label": candidate.get("label"),
                        "token_count": candidate.get("token_count"),
                        "parent_id": candidate.get("parent_id"),
                        "bin_file": candidate.get("bin_file"),
                        "bin_refs": candidate.get("bin_refs"),
                        "would_unlink_bin": int(candidate.get("bin_refs") or 0) <= 1,
                        "size_bytes": candidate.get("size_bytes"),
                        "hits": candidate.get("hits"),
                        "created_at": candidate.get("created_at"),
                        "last_used": candidate.get("last_used"),
                        "sort_key": {
                            "last_used_or_created_at": candidate.get("last_used") or candidate.get("created_at"),
                            "id": candidate.get("id"),
                            "cache_dir": str(cache.cache_dir),
                        },
                    }
                )

            node = dict(chosen)
            node.pop("bin_refs", None)
            node["cache_dir"] = str(chosen_cache.cache_dir)
            log.info(
                "prefix-cache global prune selected %s: %s",
                node["id"],
                json.dumps(
                    {
                        "scope": "global",
                        "cache_root": str(self.cache_root),
                        "selected_id": node["id"],
                        "selected_cache_dir": str(chosen_cache.cache_dir),
                        "reason": "least-recently-used unpinned leaf across cache dirs by COALESCE(last_used, created_at)",
                        "total_bytes": total_bytes,
                        "total_nodes": total_nodes,
                        "max_bytes": max_bytes,
                        "max_nodes": max_nodes,
                        "over_bytes": over_bytes,
                        "over_nodes": over_nodes,
                        "dry_run": dry_run,
                        "candidates": decision_data,
                    },
                    sort_keys=True,
                ),
            )
            removed.append(node)
            if dry_run:
                break

            with contextlib.closing(chosen_cache.connect()) as db:
                old_bin = node["bin_file"]
                db.execute("DELETE FROM nodes WHERE id = ?", (node["id"],))
                remaining_refs = int(
                    db.execute("SELECT COUNT(*) FROM nodes WHERE bin_file = ?", (old_bin,)).fetchone()[0]
                )
                if remaining_refs == 0:
                    bin_path = chosen_cache.absolute_bin_path(old_bin)
                    try:
                        bin_path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    # Other nodes (ancestors) still reference this file — can't unlink.
                    # Redirect them to the largest available alternative file so we
                    # can actually free disk space. Any file with >= ancestor's
                    # token_count works because llama.cpp uses prefix matching.
                    alt_row = db.execute(
                        """
                        SELECT bf.bin_file, MAX(bf.token_count) AS max_tokens
                        FROM nodes bf
                        WHERE bf.bin_file != ?
                        GROUP BY bf.bin_file
                        ORDER BY max_tokens DESC
                        LIMIT 1
                        """,
                        (old_bin,),
                    ).fetchone()
                    if alt_row is not None:
                        alt_bin = alt_row[0]
                        db.execute(
                            "UPDATE nodes SET bin_file = ? WHERE bin_file = ?",
                            (alt_bin, old_bin),
                        )
                        log.info(
                            "prefix-cache prune redirected %d ancestor(s) from %s → %s",
                            remaining_refs, old_bin, alt_bin,
                        )
                        bin_path = chosen_cache.absolute_bin_path(old_bin)
                        try:
                            bin_path.unlink()
                        except FileNotFoundError:
                            pass
                    # else: no alternative file available, can't unlink yet
                db.commit()
        return removed

    def prune(self, *, max_bytes: int | None, max_nodes: int | None, dry_run: bool) -> list[dict[str, Any]]:
        removed: list[dict[str, Any]] = []
        with contextlib.closing(self.connect()) as db:
            while True:
                total_bytes = int(db.execute(
                    """
                    SELECT COALESCE(SUM(size_bytes), 0)
                    FROM (SELECT bin_file, MAX(size_bytes) AS size_bytes FROM nodes GROUP BY bin_file)
                    """
                ).fetchone()[0])
                total_nodes = int(db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
                over_bytes = max_bytes is not None and total_bytes > max_bytes
                over_nodes = max_nodes is not None and total_nodes > max_nodes
                if not over_bytes and not over_nodes:
                    break

                candidate_rows = db.execute(
                    """
                    SELECT
                      n.*,
                      (SELECT COUNT(*) FROM nodes r WHERE r.bin_file = n.bin_file) AS bin_refs
                    FROM nodes n
                    LEFT JOIN nodes c ON c.parent_id = n.id
                    WHERE c.id IS NULL AND n.pinned = 0
                    ORDER BY
                      COALESCE(n.last_used, n.created_at) ASC,
                      n.id ASC
                    """
                ).fetchall()
                if not candidate_rows:
                    log.warning(
                        "prefix-cache prune wanted but found no removable leaf nodes: %s",
                        json.dumps(
                            {
                                "total_bytes": total_bytes,
                                "total_nodes": total_nodes,
                                "max_bytes": max_bytes,
                                "max_nodes": max_nodes,
                                "over_bytes": over_bytes,
                                "over_nodes": over_nodes,
                                "dry_run": dry_run,
                                "candidates": [],
                            },
                            sort_keys=True,
                        ),
                    )
                    break
                decision_data = []
                for rank, r in enumerate(candidate_rows, 1):
                    candidate = dict(r)
                    decision_data.append(
                        {
                            "rank": rank,
                            "selected": rank == 1,
                            "id": candidate["id"],
                            "boundary": candidate.get("boundary"),
                            "label": candidate.get("label"),
                            "token_count": candidate.get("token_count"),
                            "parent_id": candidate.get("parent_id"),
                            "bin_file": candidate.get("bin_file"),
                            "bin_refs": candidate.get("bin_refs"),
                            "would_unlink_bin": int(candidate.get("bin_refs") or 0) <= 1,
                            "size_bytes": candidate.get("size_bytes"),
                            "hits": candidate.get("hits"),
                            "created_at": candidate.get("created_at"),
                            "last_used": candidate.get("last_used"),
                            "sort_key": {
                                "last_used_or_created_at": candidate.get("last_used") or candidate.get("created_at"),
                                "id": candidate.get("id"),
                            },
                        }
                    )
                row = candidate_rows[0]
                node = dict(row)
                node.pop("bin_refs", None)
                log.info(
                    "prefix-cache prune selected %s: %s",
                    node["id"],
                    json.dumps(
                        {
                            "selected_id": node["id"],
                            "reason": "least-recently-used unpinned leaf by COALESCE(last_used, created_at)",
                            "total_bytes": total_bytes,
                            "total_nodes": total_nodes,
                            "max_bytes": max_bytes,
                            "max_nodes": max_nodes,
                            "over_bytes": over_bytes,
                            "over_nodes": over_nodes,
                            "dry_run": dry_run,
                            "candidates": decision_data,
                        },
                        sort_keys=True,
                    ),
                )
                removed.append(node)
                if dry_run:
                    # Simulate only one candidate in dry-run to avoid fake totals.
                    break
                old_bin = node["bin_file"]
                db.execute("DELETE FROM nodes WHERE id = ?", (node["id"],))
                remaining_refs = int(
                    db.execute("SELECT COUNT(*) FROM nodes WHERE bin_file = ?", (old_bin,)).fetchone()[0]
                )
                if remaining_refs == 0:
                    bin_path = self.absolute_bin_path(old_bin)
                    try:
                        bin_path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    # Other nodes (ancestors) still reference this file — can't unlink.
                    # Redirect them to the largest available alternative file.
                    alt_row = db.execute(
                        """
                        SELECT bf.bin_file, MAX(bf.token_count) AS max_tokens
                        FROM nodes bf
                        WHERE bf.bin_file != ?
                        GROUP BY bf.bin_file
                        ORDER BY max_tokens DESC
                        LIMIT 1
                        """,
                        (old_bin,),
                    ).fetchone()
                    if alt_row is not None:
                        alt_bin = alt_row[0]
                        db.execute(
                            "UPDATE nodes SET bin_file = ? WHERE bin_file = ?",
                            (alt_bin, old_bin),
                        )
                        log.info(
                            "prefix-cache prune redirected %d ancestor(s) from %s → %s",
                            remaining_refs, old_bin, alt_bin,
                        )
                        bin_path = self.absolute_bin_path(old_bin)
                        try:
                            bin_path.unlink()
                        except FileNotFoundError:
                            pass
                db.commit()
        return removed


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        return pathlib.Path(args.prompt_file).expanduser().read_text()
    if args.prompt is not None:
        return args.prompt
    return sys.stdin.read()


def cmd_init(args: argparse.Namespace) -> None:
    PrefixCache(args.cache_dir).init()
    print(json.dumps({"ok": True, "db": str(PrefixCache(args.cache_dir).db_path)}, indent=2))


def cmd_add(args: argparse.Namespace) -> None:
    cache = PrefixCache(args.cache_dir)
    cache.init()
    client = LlamaClient(args.base_url)
    prompt = read_prompt(args)
    tokens = client.tokenize(prompt)
    node_id, digest = node_id_for(tokens)

    existing = cache.get_node(node_id)
    if existing and not args.force:
        print(json.dumps({"ok": True, "exists": True, "node": existing}, indent=2, default=str))
        return
    if existing and args.force:
        raise SystemExit("--force replacement is not implemented yet; prune/delete the node first")

    parent_id = cache.parent_for(tokens, node_id)
    bin_file = cache.relative_node_bin(node_id)
    bin_path = cache.absolute_bin_path(bin_file)
    bin_path.parent.mkdir(parents=True, exist_ok=True)

    if bin_path.exists() and not args.force:
        raise SystemExit(f"bin already exists without DB node: {bin_path}")

    if not args.no_erase:
        client.erase_slot(args.slot)

    prefill = client.prefill_completion(prompt)
    save = client.save_slot(args.slot, bin_file)
    n_saved = int(save.get("n_saved", -1))
    size_bytes = bin_path.stat().st_size

    if n_saved != len(tokens) and not args.allow_token_mismatch:
        try:
            bin_path.unlink()
        except FileNotFoundError:
            pass
        raise SystemExit(
            f"refusing node: n_saved={n_saved} but token_count={len(tokens)} "
            "(use --allow-token-mismatch to keep it)"
        )

    props = client.props()
    settings = props.get("default_generation_settings", {}) if isinstance(props, dict) else {}
    node = {
        "id": node_id,
        "parent_id": parent_id,
        "label": args.label,
        "boundary": args.boundary,
        "token_count": len(tokens),
        "prefix_hash": digest,
        "hash_algo": HASH_ALGO,
        "bin_file": bin_file,
        "size_bytes": size_bytes,
        "n_saved": n_saved,
        "model_alias": props.get("model_alias") if isinstance(props, dict) else None,
        "model_path": props.get("model_path") if isinstance(props, dict) else None,
        "ctx_size": settings.get("n_ctx") if isinstance(settings, dict) else None,
        "hits": 0,
        "created_at": utc_now(),
        "last_used": None,
        "pinned": args.pinned,
        "meta": {
            "prefill_timings": prefill.get("timings", {}) if isinstance(prefill, dict) else {},
            "save_response": save,
        },
    }
    cache.insert_node(node)
    print(json.dumps({"ok": True, "node": node}, indent=2, default=str))


def cmd_lookup(args: argparse.Namespace) -> None:
    cache = PrefixCache(args.cache_dir)
    cache.init()
    client = LlamaClient(args.base_url)
    prompt = read_prompt(args)
    tokens = client.tokenize(prompt)
    node = cache.lookup(tokens, touch=args.touch)
    print(json.dumps({"ok": True, "token_count": len(tokens), "match": node}, indent=2, default=str))


def cmd_list(args: argparse.Namespace) -> None:
    cache = PrefixCache(args.cache_dir)
    cache.init()
    nodes = cache.list_nodes()
    if args.json:
        print(json.dumps({"nodes": nodes, "total_bytes": cache.total_bytes()}, indent=2, default=str))
        return
    print(f"{'tokens':>8} {'size_mb':>9} {'hits':>6} {'pinned':>6} {'id':<45} label")
    for n in nodes:
        print(
            f"{int(n['token_count']):8d} {int(n['size_bytes'])/1024/1024:9.1f} "
            f"{int(n['hits']):6d} {int(n['pinned']):6d} {n['id']:<45} {n['label']}"
        )
    print(f"total: {len(nodes)} nodes, {cache.total_bytes()/1024/1024:.1f} MiB")


def cmd_prune(args: argparse.Namespace) -> None:
    cache = PrefixCache(args.cache_dir)
    cache.init()
    removed = cache.prune(max_bytes=args.max_bytes, max_nodes=args.max_nodes, dry_run=args.dry_run)
    print(json.dumps({"ok": True, "dry_run": args.dry_run, "removed": removed}, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Manage llama.cpp slot-KV prefix cache trie metadata")
    p.add_argument("--cache-dir", type=pathlib.Path, default=DEFAULT_CACHE_DIR)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="llama.cpp/proxy base URL without /v1")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="initialize prefix-cache sqlite DB")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("add", help="prefill prompt, save slot bin, and add trie node")
    s.add_argument("--label", required=True)
    s.add_argument("--boundary", default="manual")
    s.add_argument("--prompt-file")
    s.add_argument("--prompt")
    s.add_argument("--slot", type=int, default=0)
    s.add_argument("--pinned", action="store_true")
    s.add_argument("--no-erase", action="store_true")
    s.add_argument("--allow-token-mismatch", action="store_true")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("lookup", help="find longest cached prefix for prompt")
    s.add_argument("--prompt-file")
    s.add_argument("--prompt")
    s.add_argument("--touch", action="store_true", help="increment hit count and last_used")
    s.set_defaults(func=cmd_lookup)

    s = sub.add_parser("list", help="list trie nodes")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("prune", help="delete leaf nodes until under budget")
    s.add_argument("--max-bytes", type=int)
    s.add_argument("--max-nodes", type=int)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_prune)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
