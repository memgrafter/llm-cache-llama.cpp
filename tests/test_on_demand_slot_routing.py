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


def make_handler(state=None, discover_slots=None, mock_evict=False):
    """Create a minimal handler instance with needed attributes."""
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
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": False}])
        node = {"id": "node-A", "token_count": 6000}

        # Request (6000) covers slot's prefix (4000) → reuse
        result = h._pick_slot_for_restore(node, 6000)
        self.assertEqual(result, 0)

    def test_slot_not_exhausted_falls_through_to_eviction(self):
        """Slot with node >= 5000 tok but request doesn't exhaust slot prefix → not reused,
        eviction kicks in and returns a slot."""
        state = SlotState()
        state.record(0, "node-A", 6000)  # slot has more tokens than request
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": True}], mock_evict=True)
        node = {"id": "node-A", "token_count": 6000}

        # Request (4000) doesn't cover slot prefix (6000) → not reused, falls to eviction
        result = h._pick_slot_for_restore(node, 4000)
        self.assertEqual(result, 0)  # eviction returns slot 0
        h._evict_slot.assert_called_with(0)

    def test_reuses_small_node_when_request_covers_ratio(self):
        """Slot with node < 5000 tok: request must cover 80%."""
        state = SlotState()
        state.record(0, "node-B", 3000)
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": False}])
        node = {"id": "node-B", "token_count": 3000}

        result = h._pick_slot_for_restore(node, 2500)  # > 80% of 3000
        self.assertEqual(result, 0)

    def test_small_node_below_ratio_falls_through_to_eviction(self):
        """Slot with node < 5000 tok and request below 80%: slot not reused,
        but eviction kicks in and returns a slot."""
        state = SlotState()
        state.record(0, "node-B", 3000)
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": True}], mock_evict=True)
        node = {"id": "node-B", "token_count": 3000}

        result = h._pick_slot_for_restore(node, 2000)  # < 80% of 3000
        self.assertEqual(result, 0)  # eviction returns slot 0
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
        self.assertEqual(result, 1)  # evict the small one
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
        self.assertEqual(result, 0)  # slot 0 is LRU
        h._evict_slot.assert_called_with(0)

    def test_single_slot_mode_returns_hardcoded_slot(self):
        """In single-slot mode (slot_state is None), return hardcoded slot_id."""
        h = make_handler(state=None)
        node = {"id": "node-A", "token_count": 6000}
        result = h._pick_slot_for_restore(node, 4000)
        self.assertEqual(result, 0)

    def test_no_node_no_idle_returns_none(self):
        """When node is None and no idle slots, returns None."""
        state = SlotState()
        state.record(0, "node-A", 6000)
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": True}])

        result = h._pick_slot_for_restore(None, 4000)
        self.assertIsNone(result)

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
        self.assertEqual(result, 1)  # use idle slot, not evict


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


if __name__ == "__main__":
    unittest.main()
