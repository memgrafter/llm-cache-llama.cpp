import pathlib
import tempfile
import unittest

import prefix_cache


class PrefixCacheTests(unittest.TestCase):
    def _node(self, tokens, label="node", parent_id=None, bin_file=None):
        node_id, digest = prefix_cache.node_id_for(tokens)
        return {
            "id": node_id,
            "parent_id": parent_id,
            "label": label,
            "boundary": "manual",
            "token_count": len(tokens),
            "prefix_hash": digest,
            "hash_algo": prefix_cache.HASH_ALGO,
            "bin_file": bin_file or f"trie/nodes/{node_id}.bin",
            "size_bytes": 123,
            "n_saved": len(tokens),
            "model_alias": "test-model",
            "model_path": "/tmp/model.gguf",
            "ctx_size": 32768,
            "hits": 0,
            "created_at": "2026-05-20T00:00:00Z",
            "last_used": None,
            "pinned": False,
            "meta": {},
        }

    def test_hash_tokens_is_stable_and_length_sensitive(self):
        self.assertEqual(prefix_cache.hash_tokens([1, 2, 3]), prefix_cache.hash_tokens([1, 2, 3]))
        self.assertNotEqual(prefix_cache.hash_tokens([1, 2, 3]), prefix_cache.hash_tokens([1, 2, 3, 4]))

    def test_prefix_hashes_returns_requested_prefixes(self):
        tokens = [10, 20, 30, 40]
        result = prefix_cache.prefix_hashes(tokens, [1, 3, 99])

        self.assertEqual(set(result), {1, 3})
        self.assertEqual(result[1], prefix_cache.hash_tokens(tokens[:1]))
        self.assertEqual(result[3], prefix_cache.hash_tokens(tokens[:3]))

    def test_init_and_list_empty_cache(self):
        with tempfile.TemporaryDirectory() as d:
            cache = prefix_cache.PrefixCache(pathlib.Path(d))
            cache.init()

            self.assertEqual(cache.list_nodes(), [])
            self.assertEqual(cache.total_bytes(), 0)
            self.assertTrue(cache.db_path.exists())
            configs = cache.list_anchor_configs()
            self.assertEqual(len(configs), 1)
            self.assertEqual(configs[0]["label"], "end-of-system-message")
            self.assertEqual(configs[0]["marker"], "<|im_end|>")

    def test_lookup_returns_longest_matching_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            cache = prefix_cache.PrefixCache(pathlib.Path(d))
            cache.init()
            short = self._node([1, 2], label="short")
            long = self._node([1, 2, 3, 4], label="long", parent_id=short["id"])
            cache.insert_node(short)
            cache.insert_node(long)

            match = cache.lookup([1, 2, 3, 4, 5])

            self.assertIsNotNone(match)
            self.assertEqual(match["id"], long["id"])
            self.assertEqual(match["label"], "long")

    def test_lookup_no_cache_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            cache = prefix_cache.PrefixCache(pathlib.Path(d))
            cache.init()

            self.assertIsNone(cache.lookup([1, 2, 3]))

    def test_lookup_missed_cache_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            cache = prefix_cache.PrefixCache(pathlib.Path(d))
            cache.init()
            cache.insert_node(self._node([1, 2, 3], label="other-prefix"))

            self.assertIsNone(cache.lookup([1, 2, 9, 10]))

    def test_lookup_cache_too_long_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            cache = prefix_cache.PrefixCache(pathlib.Path(d))
            cache.init()
            cache.insert_node(self._node([1, 2, 3, 4], label="too-long"))

            self.assertIsNone(cache.lookup([1, 2, 3]))

    def test_lookup_touch_updates_hits_and_last_used(self):
        with tempfile.TemporaryDirectory() as d:
            cache = prefix_cache.PrefixCache(pathlib.Path(d))
            cache.init()
            node = self._node([7, 8, 9], label="touch")
            cache.insert_node(node)

            match = cache.lookup([7, 8, 9, 10], touch=True)
            stored = cache.get_node(node["id"])

            self.assertIsNotNone(match)
            self.assertEqual(match["hits"], 1)
            self.assertIsNotNone(match["last_used"])
            self.assertEqual(stored["hits"], 1)
            self.assertIsNotNone(stored["last_used"])

    def test_parent_for_returns_longest_existing_parent(self):
        with tempfile.TemporaryDirectory() as d:
            cache = prefix_cache.PrefixCache(pathlib.Path(d))
            cache.init()
            root = self._node([1], label="root")
            parent = self._node([1, 2, 3], label="parent", parent_id=root["id"])
            cache.insert_node(root)
            cache.insert_node(parent)
            child_id, _ = prefix_cache.node_id_for([1, 2, 3, 4, 5])

            self.assertEqual(cache.parent_for([1, 2, 3, 4, 5], child_id), parent["id"])

    def test_prune_removes_leaf_but_keeps_parent(self):
        with tempfile.TemporaryDirectory() as d:
            cache_dir = pathlib.Path(d)
            cache = prefix_cache.PrefixCache(cache_dir)
            cache.init()
            parent = self._node([1, 2], label="parent", bin_file="trie/nodes/parent.bin")
            leaf = self._node([1, 2, 3], label="leaf", parent_id=parent["id"], bin_file="trie/nodes/leaf.bin")
            (cache_dir / parent["bin_file"]).parent.mkdir(parents=True, exist_ok=True)
            (cache_dir / parent["bin_file"]).write_bytes(b"parent")
            (cache_dir / leaf["bin_file"]).write_bytes(b"leaf")
            parent["size_bytes"] = 6
            leaf["size_bytes"] = 4
            cache.insert_node(parent)
            cache.insert_node(leaf)

            removed = cache.prune(max_bytes=6, max_nodes=None, dry_run=False)

            self.assertEqual([n["id"] for n in removed], [leaf["id"]])
            self.assertIsNotNone(cache.get_node(parent["id"]))
            self.assertIsNone(cache.get_node(leaf["id"]))
            self.assertTrue((cache_dir / parent["bin_file"]).exists())
            self.assertFalse((cache_dir / leaf["bin_file"]).exists())

    def test_prune_uses_plain_lru_not_hits_or_size(self):
        with tempfile.TemporaryDirectory() as d:
            cache_dir = pathlib.Path(d)
            cache = prefix_cache.PrefixCache(cache_dir)
            cache.init()
            old_hot = self._node([1], label="old-hot", bin_file="old-hot.bin")
            new_cold = self._node([2], label="new-cold", bin_file="new-cold.bin")
            old_hot["created_at"] = "2026-05-20T00:00:00Z"
            old_hot["last_used"] = "2026-05-20T01:00:00Z"
            old_hot["hits"] = 99
            old_hot["size_bytes"] = 1
            new_cold["created_at"] = "2026-05-20T00:00:00Z"
            new_cold["last_used"] = "2026-05-20T02:00:00Z"
            new_cold["hits"] = 0
            new_cold["size_bytes"] = 999
            (cache_dir / old_hot["bin_file"]).write_bytes(b"a")
            (cache_dir / new_cold["bin_file"]).write_bytes(b"b")
            cache.insert_node(old_hot)
            cache.insert_node(new_cold)

            removed = cache.prune(max_bytes=None, max_nodes=1, dry_run=False)

            self.assertEqual([n["id"] for n in removed], [old_hot["id"]])
            self.assertIsNone(cache.get_node(old_hot["id"]))
            self.assertIsNotNone(cache.get_node(new_cold["id"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
