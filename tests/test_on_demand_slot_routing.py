import importlib.util
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

# Load the on-demand proxy module
_spec = importlib.util.spec_from_file_location(
    "lmcache_proxy_on_demand",
    os.path.join(os.path.dirname(__file__), "..", "lmcache-proxy-on-demand.py"),
)
_lmcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lmcp)

SlotState = _lmcp.SlotState


def make_handler(state=None, discover_slots=None, mock_evict=False, nodes=None):
    """Create a minimal handler instance with needed attributes.

    Args:
        state: SlotState instance or None
        discover_slots: list of slot dicts for _discover_slots mock
        mock_evict: if True, mock _evict_slot to return True
        nodes: dict mapping node_id -> {"id", "parent_id", "token_count", ...}
            used to build a fake prefix_cache_obj.get_node()
    """
    class FakeHandler:
        pass
    h = FakeHandler()
    h.slot_state = state
    h.slot_id = 0
    h.min_match_tokens = 5000
    h.min_match_ratio = 0.8
    if discover_slots is not None:
        h._discover_slots = MagicMock(return_value=discover_slots)
    else:
        h._discover_slots = MagicMock(return_value=None)
    if mock_evict:
        h._evict_slot = MagicMock(return_value=True)
    # Build fake prefix_cache_obj with get_node
    class FakeCache:
        def __init__(self, nodes):
            self._nodes = nodes or {}
        def get_node(self, node_id):
            return self._nodes.get(node_id)
    h.prefix_cache_obj = FakeCache(nodes)
    # Bind the actual methods
    h._pick_slot_for_restore = _lmcp.LMCacheHandler._pick_slot_for_restore.__get__(h, FakeHandler)
    h._idle_slots = _lmcp.LMCacheHandler._idle_slots.__get__(h, FakeHandler)
    h._empty_slots = _lmcp.LMCacheHandler._empty_slots.__get__(h, FakeHandler)
    return h


class TestPickSlotForRestore(unittest.TestCase):
    """Test _pick_slot_for_restore in the on-demand proxy."""

    def test_reuses_slot_with_matching_large_node(self):
        """Slot with node >= 5000 tok: request must exhaust slot's prefix (req >= slot_tok)."""
        state = SlotState()
        state.record(0, "node-A", 4000)
        nodes = {
            "node-A": {"id": "node-A", "parent_id": None, "token_count": 4000},
        }
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": False}], nodes=nodes)
        node = {"id": "node-A", "parent_id": None, "token_count": 6000}

        # Request (6000) covers slot's prefix (4000) → reuse, ancestor match → no restore needed
        result = h._pick_slot_for_restore(node, 6000)
        self.assertEqual(result, (0, False))

    def test_slot_not_exhausted_falls_through_to_eviction(self):
        """Slot with node >= 5000 tok but request doesn't exhaust slot prefix → not reused,
        eviction kicks in and returns a slot."""
        state = SlotState()
        state.record(0, "node-A", 6000)  # slot has more tokens than request
        nodes = {
            "node-A": {"id": "node-A", "parent_id": None, "token_count": 6000},
        }
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": True}], mock_evict=True, nodes=nodes)
        node = {"id": "node-A", "parent_id": None, "token_count": 6000}

        # Request (4000) doesn't cover slot prefix (6000) → not reused, falls to eviction
        result = h._pick_slot_for_restore(node, 4000)
        self.assertEqual(result, (0, True))  # eviction returns slot 0, needs restore
        h._evict_slot.assert_called_with(0)

    def test_reuses_small_node_when_request_covers_slot(self):
        """Slot reused only when request covers the slot's loaded prefix."""
        state = SlotState()
        state.record(0, "node-B", 3000)
        nodes = {
            "node-B": {"id": "node-B", "parent_id": None, "token_count": 3000},
        }
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": False}], nodes=nodes)
        node = {"id": "node-B", "parent_id": None, "token_count": 3000}

        result = h._pick_slot_for_restore(node, 3500)  # >= slot tokens, ancestor match → no restore
        self.assertEqual(result, (0, False))

    def test_request_shorter_than_slot_falls_through_to_eviction(self):
        """When request is shorter than slot's loaded prefix, slot not reused,
        eviction kicks in and returns a slot."""
        state = SlotState()
        state.record(0, "node-B", 3000)
        nodes = {
            "node-B": {"id": "node-B", "parent_id": None, "token_count": 3000},
        }
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": True}], mock_evict=True, nodes=nodes)
        node = {"id": "node-B", "parent_id": None, "token_count": 3000}

        result = h._pick_slot_for_restore(node, 2000)  # < slot tokens, falls to eviction
        self.assertEqual(result, (0, True))  # eviction returns slot 0, needs restore
        h._evict_slot.assert_called_with(0)

    def test_evicts_small_node_first(self):
        """When all slots are full, prefer evicting a slot with < 10000 tok."""
        state = SlotState()
        state.record(0, "node-big", 50000)
        state.record(1, "node-small", 8000)
        state.record(2, "node-medium", 30000)
        h = make_handler(state=state, discover_slots=[
            {"id": 0, "is_busy": True},
            {"id": 1, "is_busy": True},
            {"id": 2, "is_busy": True},
        ], mock_evict=True)

        node = {"id": "node-new", "token_count": 1000}
        result = h._pick_slot_for_restore(node, 500)
        self.assertEqual(result, (1, True))  # evict the small one, needs restore
        h._evict_slot.assert_called_with(1)

    def test_evicts_lru_when_no_small_node(self):
        """When all slots have >= 10000 tok, evict LRU."""
        state = SlotState()
        state.record(0, "node-A", 50000)
        time.sleep(0.01)
        state.record(1, "node-B", 30000)
        time.sleep(0.01)
        state.record(2, "node-C", 20000)
        h = make_handler(state=state, discover_slots=[
            {"id": 0, "is_busy": True},
            {"id": 1, "is_busy": True},
            {"id": 2, "is_busy": True},
        ], mock_evict=True)

        node = {"id": "node-new", "token_count": 1000}
        result = h._pick_slot_for_restore(node, 500)
        self.assertEqual(result, (0, True))  # slot 0 is LRU, needs restore
        h._evict_slot.assert_called_with(0)

    def test_single_slot_mode_returns_hardcoded_slot(self):
        """In single-slot mode (slot_state is None), return hardcoded slot_id."""
        h = make_handler(state=None)
        node = {"id": "node-A", "token_count": 6000}
        result = h._pick_slot_for_restore(node, 4000)
        # single-slot mode returns just slot_id (int), not tuple
        self.assertEqual(result, 0)

    def test_no_node_no_idle_returns_none(self):
        """When node is None and no idle slots, returns None."""
        state = SlotState()
        state.record(0, "node-A", 6000)
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": True}])

        result = h._pick_slot_for_restore(None, 4000)
        self.assertIsNone(result)

    def test_ancestor_match_reuses_slot(self):
        """When matched node is a descendant of what a slot holds, reuse that slot."""
        state = SlotState()
        state.record(0, "node-A", 99308)  # slot holds ancestor
        nodes = {
            "node-A": {"id": "node-A", "parent_id": None, "token_count": 99308},
            "node-B": {"id": "node-B", "parent_id": "node-A", "token_count": 99747},
        }
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": False}], nodes=nodes)
        node = {"id": "node-B", "parent_id": "node-A", "token_count": 99747}

        # node-B is descendant of node-A → slot 0 holds ancestor → reuse, no restore needed
        result = h._pick_slot_for_restore(node, 100000)
        self.assertEqual(result, (0, False))

    def test_ancestor_match_returns_needs_restore_false(self):
        """Ancestor match returns needs_restore=False; sibling/eviction returns True."""
        # Ancestor match → (slot, False)
        state = SlotState()
        state.record(0, "node-A", 5000)
        nodes = {
            "node-A": {"id": "node-A", "parent_id": None, "token_count": 5000},
            "node-B": {"id": "node-B", "parent_id": "node-A", "token_count": 8000},
        }
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": False}], nodes=nodes)
        result = h._pick_slot_for_restore(
            {"id": "node-B", "parent_id": "node-A", "token_count": 8000}, 9000)
        self.assertEqual(result, (0, False))  # ancestor match, no restore

        # Eviction → (slot, True)
        state2 = SlotState()
        state2.record(0, "node-A", 50000)
        h2 = make_handler(state=state2, discover_slots=[{"id": 0, "is_busy": True}],
                          mock_evict=True, nodes={})
        result2 = h2._pick_slot_for_restore(
            {"id": "node-new", "token_count": 1000}, 500)
        self.assertEqual(result2, (0, True))  # eviction, needs restore

        # Idle slot → (slot, True)
        state3 = SlotState()
        state3.record(0, "node-A", 50000)
        h3 = make_handler(state=state3, discover_slots=[
            {"id": 0, "is_busy": True},
            {"id": 1, "is_busy": False},
        ], nodes={})
        result3 = h3._pick_slot_for_restore(
            {"id": "node-new", "token_count": 1000}, 500)
        self.assertEqual(result3, (1, True))  # idle slot, needs restore

    def test_shallow_ancestor_not_reused(self):
        """When slot holds only a shallow ancestor (< 5000 tok), 80% ratio applies.
        If request doesn't cover 80% of the shared prefix, don't reuse."""
        state = SlotState()
        state.record(0, "node-root", 100)  # tiny root node
        nodes = {
            "node-root": {"id": "node-root", "parent_id": None, "token_count": 100},
            "node-A": {"id": "node-A", "parent_id": "node-root", "token_count": 5000},
        }
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": True}], mock_evict=True, nodes=nodes)
        node = {"id": "node-A", "parent_id": "node-root", "token_count": 5000}

        # shared prefix is 100 tok — ancestor match → reuse, no restore needed
        result = h._pick_slot_for_restore(node, 3000)
        self.assertEqual(result, (0, False))

    def test_shallow_ancestor_request_shorter_than_slot(self):
        """When request is shorter than slot's loaded prefix, don't reuse."""
        state = SlotState()
        state.record(0, "node-root", 100)
        nodes = {
            "node-root": {"id": "node-root", "parent_id": None, "token_count": 100},
            "node-A": {"id": "node-A", "parent_id": "node-root", "token_count": 5000},
        }
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": True}], mock_evict=True, nodes=nodes)
        node = {"id": "node-A", "parent_id": "node-root", "token_count": 5000}

        # shared prefix is 100 tok, request (50) < 100 → don't reuse, falls to eviction
        result = h._pick_slot_for_restore(node, 50)
        self.assertEqual(result, (0, True))  # eviction returns slot 0, needs restore
        h._evict_slot.assert_called_with(0)

    def test_idle_slot_used_before_eviction(self):
        """Idle slots are preferred over eviction."""
        state = SlotState()
        state.record(0, "node-A", 50000)
        h = make_handler(state=state, discover_slots=[
            {"id": 0, "is_busy": True},
            {"id": 1, "is_busy": False},
        ])
        node = {"id": "node-new", "token_count": 1000}

        result = h._pick_slot_for_restore(node, 500)
        self.assertEqual(result, (1, True))  # use idle slot, needs restore, not evict


class TestThreadingLock(unittest.TestCase):
    """Test that the threading lock protects shared state."""

    def test_cache_lock_exists(self):
        """The _cache_lock class attribute exists and is a Lock."""
        self.assertTrue(hasattr(_lmcp.LMCacheHandler, '_cache_lock'))
        # Verify it's usable as a context manager (Lock behavior)
        with _lmcp.LMCacheHandler._cache_lock:
            pass  # no exception = it's a valid lock

    def test_cache_lock_is_class_level(self):
        """_cache_lock is shared across all handler instances."""
        self.assertTrue(hasattr(_lmcp.LMCacheHandler, '_cache_lock'))


class TestTryMakeRoom(unittest.TestCase):
    """Tests for _try_make_room_for — erasing another slot to make room for a restore."""

    def _make_handler(self):
        """Create a minimal handler-like object with the needed attributes."""
        class FakeHandler:
            pass
        h = FakeHandler()
        h.slot_state = _lmcp.SlotState()
        h.llama_server = "localhost"
        h.llama_port = 8081
        # Bind the method so we can call it
        h._try_make_room_for = _lmcp.LMCacheHandler._try_make_room_for.__get__(h)
        return h

    def test_erases_small_slot_first(self):
        """Prefers erasing a slot with < 10000 tokens."""
        h = self._make_handler()
        h.slot_state.record(0, "big-node", 50000)
        h.slot_state.record(1, "small-node", 5000)
        with unittest.mock.patch.object(_lmcp, "_erase_slot") as erase:
            result = h._try_make_room_for(0, {"token_count": 50000})
        self.assertEqual(result, 1)
        erase.assert_called_once_with(1, "localhost", 8081)

    def test_erases_lru_when_no_small_slot(self):
        """Falls back to LRU when all slots have >= 10000 tokens."""
        h = self._make_handler()
        h.slot_state.record(2, "big-node-b", 40000)   # older → LRU
        time.sleep(0.01)
        h.slot_state.record(0, "big-node-a", 50000)  # target, newer
        with unittest.mock.patch.object(_lmcp, "_erase_slot") as erase:
            result = h._try_make_room_for(0, {"token_count": 50000})
        self.assertEqual(result, 2)
        erase.assert_called_once_with(2, "localhost", 8081)

    def test_never_erases_target_slot(self):
        """Never erases the target slot itself."""
        h = self._make_handler()
        h.slot_state.record(0, "target-node", 5000)  # target
        h.slot_state.record(1, "other-node", 5000)
        with unittest.mock.patch.object(_lmcp, "_erase_slot") as erase:
            result = h._try_make_room_for(1, {"token_count": 50000})
        self.assertEqual(result, 0)  # erases slot 0, not target slot 1

    def test_returns_none_when_only_target_tracked(self):
        """Returns None when only the target slot is tracked."""
        h = self._make_handler()
        h.slot_state.record(0, "only-node", 50000)
        result = h._try_make_room_for(0, {"token_count": 50000})
        self.assertIsNone(result)

    def test_returns_none_when_no_slots_tracked(self):
        """Returns None when no slots are tracked."""
        h = self._make_handler()
        result = h._try_make_room_for(0, {"token_count": 50000})
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
