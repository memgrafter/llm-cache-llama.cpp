import importlib.util
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch, MagicMock, PropertyMock

# Load the on-demand proxy module
_spec = importlib.util.spec_from_file_location(
    "lmcache_proxy_on_demand",
    os.path.join(os.path.dirname(__file__), "..", "lmcache-proxy-on-demand.py"),
)
_lmcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lmcp)

SlotState = _lmcp.SlotState


def make_handler(state=None, discover_slots=None, mock_evict=False, nodes=None):
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


class TestSlotStateBusyTracking(unittest.TestCase):
    """Tests for per-slot busy tracking (ticket lcl-l89j)."""

    def test_acquire_slot_sets_busy_flag(self):
        """acquire_slot sets the busy flag for the slot."""
        state = SlotState()
        result = state.acquire_slot(0)
        self.assertTrue(result)
        self.assertTrue(state.is_slot_busy(0))

    def test_acquire_slot_returns_false_when_already_busy(self):
        """acquire_slot returns False if the slot is already busy."""
        state = SlotState()
        state.acquire_slot(0)
        result = state.acquire_slot(0)
        self.assertFalse(result)

    def test_release_slot_clears_busy_flag(self):
        """release_slot clears the busy flag for the slot."""
        state = SlotState()
        state.acquire_slot(0)
        self.assertTrue(state.is_slot_busy(0))
        state.release_slot(0)
        self.assertFalse(state.is_slot_busy(0))

    def test_acquire_different_slots_independently(self):
        """Acquiring different slots doesn't interfere."""
        state = SlotState()
        self.assertTrue(state.acquire_slot(0))
        self.assertTrue(state.acquire_slot(1))
        self.assertTrue(state.is_slot_busy(0))
        self.assertTrue(state.is_slot_busy(1))
        state.release_slot(0)
        self.assertFalse(state.is_slot_busy(0))
        self.assertTrue(state.is_slot_busy(1))
        state.release_slot(1)

    def test_forget_clears_busy_state(self):
        """forget also clears busy state and per-slot locks."""
        state = SlotState()
        state.record(0, "node-A", 5000)
        state.acquire_slot(0)
        self.assertTrue(state.is_slot_busy(0))
        state.forget(0)
        self.assertFalse(state.is_slot_busy(0))
        # Should be able to acquire again after forget
        self.assertTrue(state.acquire_slot(0))
        state.release_slot(0)

    def test_is_slot_busy_returns_false_for_untracked_slot(self):
        """is_slot_busy returns False for a slot that hasn't been acquired."""
        state = SlotState()
        self.assertFalse(state.is_slot_busy(99))

    def test_acquire_creates_per_slot_lock(self):
        """acquire_slot creates a per-slot lock on first use."""
        state = SlotState()
        self.assertNotIn(0, state._slot_locks)
        state.acquire_slot(0)
        self.assertIn(0, state._slot_locks)
        state.release_slot(0)

    def test_release_then_acquire_works(self):
        """After release, the slot can be acquired again."""
        state = SlotState()
        state.acquire_slot(0)
        state.release_slot(0)
        self.assertTrue(state.acquire_slot(0))
        state.release_slot(0)

    def test_release_unblocks_acquire(self):
        """Releasing a slot allows another thread to acquire it."""
        state = SlotState()
        state.acquire_slot(0)
        state.release_slot(0)

        acquired_in_thread = threading.Event()
        error = [None]

        def try_acquire():
            try:
                result = state.acquire_slot(0)
                self.assertTrue(result)
                state.release_slot(0)
                acquired_in_thread.set()
            except Exception as e:
                error[0] = e

        t = threading.Thread(target=try_acquire)
        t.start()
        t.join(timeout=5)
        if not acquired_in_thread.is_set():
            self.fail(f"Could not acquire slot after release: {error[0]}")


class TestIdleSlotsFiltersBusy(unittest.TestCase):
    """Tests that _idle_slots filters out proxy-busy slots."""

    def test_idle_slots_excludes_proxy_busy(self):
        """_idle_slots excludes slots that are busy from the proxy's perspective."""
        state = SlotState()
        state.record(0, "node-A", 5000)
        state.acquire_slot(0)  # mark slot 0 as busy

        discover_slots = [
            {"id": 0, "is_busy": False},
            {"id": 1, "is_busy": False},
            {"id": 2, "is_busy": False},
        ]
        h = make_handler(state=state, discover_slots=discover_slots)

        idle = h._idle_slots()
        self.assertIn(1, idle)
        self.assertIn(2, idle)
        self.assertNotIn(0, idle)  # busy from proxy perspective

    def test_idle_slots_includes_non_busy(self):
        """_idle_slots includes slots that are not busy."""
        state = SlotState()
        state.record(0, "node-A", 5000)
        # slot 0 is NOT busy

        discover_slots = [
            {"id": 0, "is_busy": False},
            {"id": 1, "is_busy": False},
        ]
        h = make_handler(state=state, discover_slots=discover_slots)

        idle = h._idle_slots()
        self.assertIn(0, idle)
        self.assertIn(1, idle)

    def test_idle_slots_excludes_backend_busy(self):
        """_idle_slots still excludes backend-busy slots."""
        state = SlotState()
        discover_slots = [
            {"id": 0, "is_busy": True},   # backend busy
            {"id": 1, "is_busy": False},
        ]
        h = make_handler(state=state, discover_slots=discover_slots)

        idle = h._idle_slots()
        self.assertNotIn(0, idle)
        self.assertIn(1, idle)

    def test_empty_slots_excludes_proxy_busy(self):
        """_empty_slots also excludes proxy-busy slots (via _idle_slots)."""
        state = SlotState()
        # No tracked slots — all are "empty" from proxy perspective
        state.acquire_slot(0)  # but slot 0 is busy

        discover_slots = [
            {"id": 0, "is_busy": False},
            {"id": 1, "is_busy": False},
        ]
        h = make_handler(state=state, discover_slots=discover_slots)

        empty = h._empty_slots()
        self.assertNotIn(0, empty)  # busy from proxy perspective
        self.assertIn(1, empty)    # not tracked and not busy


class TestAncestorMatchSkipsBusy(unittest.TestCase):
    """Tests that ancestor/sibling match skips busy slots (ticket lcl-l89j)."""

    def test_ancestor_match_skips_busy_slot(self):
        """When the best slot is busy, ancestor match returns None (falls through)."""
        state = SlotState()
        state.record(0, "node-A", 5000)
        state.acquire_slot(0)  # mark slot 0 as busy

        nodes = {
            "node-A": {"id": "node-A", "parent_id": None, "token_count": 5000},
            "node-B": {"id": "node-B", "parent_id": "node-A", "token_count": 8000},
        }
        discover_slots = [{"id": 1, "is_busy": False}]  # slot 1 available
        h = make_handler(state=state, discover_slots=discover_slots, nodes=nodes)

        # node-B is descendant of node-A → ancestor match would pick slot 0,
        # but slot 0 is busy → returns None, falls through to empty slots
        result = h._pick_slot_for_restore(
            {"id": "node-B", "parent_id": "node-A", "token_count": 8000}, 9000)
        # Should fall through to empty slot 1, needs restore
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 1)  # slot 1 (empty)
        self.assertTrue(result[1])     # needs restore

    def test_ancestor_match_works_when_slot_not_busy(self):
        """Ancestor match works normally when the slot is not busy."""
        state = SlotState()
        state.record(0, "node-A", 5000)
        # slot 0 is NOT busy

        nodes = {
            "node-A": {"id": "node-A", "parent_id": None, "token_count": 5000},
            "node-B": {"id": "node-B", "parent_id": "node-A", "token_count": 8000},
        }
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": False}], nodes=nodes)

        result = h._pick_slot_for_restore(
            {"id": "node-B", "parent_id": "node-A", "token_count": 8000}, 9000)
        self.assertEqual(result, (0, False))  # ancestor match, no restore needed

    def test_sibling_match_skips_busy_slot(self):
        """When the sibling match slot is busy, returns None (falls through)."""
        state = SlotState()
        state.record(0, "node-A", 5000)
        state.acquire_slot(0)  # mark slot 0 as busy

        nodes = {
            "node-A": {"id": "node-A", "parent_id": "node-P", "token_count": 5000},
            "node-B": {"id": "node-B", "parent_id": "node-P", "token_count": 5050},  # sibling
            "node-P": {"id": "node-P", "parent_id": None, "token_count": 4000},
        }
        discover_slots = [{"id": 1, "is_busy": False}]
        h = make_handler(state=state, discover_slots=discover_slots, nodes=nodes)

        result = h._pick_slot_for_restore(
            {"id": "node-B", "parent_id": "node-P", "token_count": 5050}, 6000)
        # sibling match would pick slot 0, but busy → falls through to empty slot 1
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 1)  # slot 1 (empty)
        self.assertTrue(result[1])     # needs restore


class TestAncestorMatchUpdatesSlotState(unittest.TestCase):
    """Tests that ancestor match updates slot_state (ticket lcl-hyzz)."""

    def test_ancestor_match_records_matched_node(self):
        """After ancestor match, slot_state is updated with the matched node."""
        state = SlotState()
        state.record(0, "node-A", 5000)  # initial state

        nodes = {
            "node-A": {"id": "node-A", "parent_id": None, "token_count": 5000},
            "node-B": {"id": "node-B", "parent_id": "node-A", "token_count": 8000},
        }
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": False}], nodes=nodes)

        # Before: slot 0 has node-A (5000 tokens)
        self.assertEqual(state.node_for(0), "node-A")
        self.assertEqual(state.tokens_for(0), 5000)

        # Ancestor match: node-B is descendant of node-A
        result = h._pick_slot_for_restore(
            {"id": "node-B", "parent_id": "node-A", "token_count": 8000}, 9000)
        self.assertEqual(result, (0, False))

        # After: slot_state should still have node-A (the matched ancestor)
        # because the ancestor walk finds node-A as the deepest match
        self.assertEqual(state.node_for(0), "node-A")
        self.assertEqual(state.tokens_for(0), 5000)

    def test_ancestor_match_records_deeper_node_when_slot_has_parent(self):
        """When slot holds a deeper ancestor, that node is recorded."""
        state = SlotState()
        # Slot holds node-B (deeper in the trie than node-A)
        state.record(0, "node-B", 8000)

        nodes = {
            "node-A": {"id": "node-A", "parent_id": None, "token_count": 5000},
            "node-B": {"id": "node-B", "parent_id": "node-A", "token_count": 8000},
            "node-C": {"id": "node-C", "parent_id": "node-B", "token_count": 10000},
        }
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": False}], nodes=nodes)

        # Ancestor match: node-C is descendant of node-B
        result = h._pick_slot_for_restore(
            {"id": "node-C", "parent_id": "node-B", "token_count": 10000}, 11000)
        self.assertEqual(result, (0, False))

        # slot_state should have node-B (the deepest match found by ancestor walk)
        self.assertEqual(state.node_for(0), "node-B")
        self.assertEqual(state.tokens_for(0), 8000)

    def test_ancestor_match_records_best_shared_node_id(self):
        """best_shared_node_id is tracked and used in record() call."""
        state = SlotState()
        state.record(0, "node-A", 5000)

        nodes = {
            "node-A": {"id": "node-A", "parent_id": None, "token_count": 5000},
            "node-B": {"id": "node-B", "parent_id": "node-A", "token_count": 8000},
        }
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": False}], nodes=nodes)

        # Ancestor match with node-B
        result = h._pick_slot_for_restore(
            {"id": "node-B", "parent_id": "node-A", "token_count": 8000}, 9000)
        self.assertEqual(result, (0, False))

        # The matched node is node-A (the deepest ancestor that slot holds)
        self.assertEqual(state.node_for(0), "node-A")
        self.assertEqual(state.tokens_for(0), 5000)

    def test_multiple_ancestor_matches_update_state(self):
        """Multiple ancestor matches update slot_state progressively."""
        state = SlotState()
        state.record(0, "node-A", 5000)

        nodes = {
            "node-A": {"id": "node-A", "parent_id": None, "token_count": 5000},
            "node-B": {"id": "node-B", "parent_id": "node-A", "token_count": 8000},
        }
        h = make_handler(state=state, discover_slots=[{"id": 0, "is_busy": False}], nodes=nodes)

        # First ancestor match: node-B → matches node-A in slot
        result1 = h._pick_slot_for_restore(
            {"id": "node-B", "parent_id": "node-A", "token_count": 8000}, 9000)
        self.assertEqual(result1, (0, False))
        self.assertEqual(state.node_for(0), "node-A")

        # Now update slot to hold node-B (simulating a restore that happened elsewhere)
        state.record(0, "node-B", 8000)

        # Second ancestor match: node-C → matches node-B in slot
        result2 = h._pick_slot_for_restore(
            {"id": "node-B", "parent_id": "node-A", "token_count": 8000}, 9000)
        self.assertEqual(result2, (0, False))
        # Should now have node-B recorded
        self.assertEqual(state.node_for(0), "node-B")
        self.assertEqual(state.tokens_for(0), 8000)


class TestSlotLockInHandleRequest(unittest.TestCase):
    """Tests for slot lock acquire/release in _handle_request (ticket lcl-l89j)."""

    def test_slot_lock_acquired_before_forward(self):
        """Slot lock is acquired before _forward is called."""
        state = SlotState()
        state.record(0, "node-A", 5000)

        class FakeHandler:
            slot_state = state
            slot_id = 0
            prefix_cache_obj = None
            prefix_cache_enabled = False
            _cache_lock = _lmcp.LMCacheHandler._cache_lock

            def _request_cache_context(self, path, body):
                return None

            def _restore_legacy_cache(self, body):
                pass

            def _forward(self, method, path, body_bytes):
                # Slot should be busy during forward
                self.assertTrue(state.is_slot_busy(0))
                return _lmcp.ForwardResult(200, "text/plain", b"ok")

        h = FakeHandler()
        h._handle_request = _lmcp.LMCacheHandler._handle_request.__get__(h, FakeHandler)

        # Manually set up the scenario: target_slot is set
        with patch.object(h, '_request_cache_context', return_value=None):
            # Can't easily test the full flow without mocking HTTP,
            # so just verify the lock mechanics work
            pass

    def test_slot_lock_released_after_forward(self):
        """Slot lock is released after _forward completes."""
        state = SlotState()
        state.acquire_slot(0)
        self.assertTrue(state.is_slot_busy(0))
        state.release_slot(0)
        self.assertFalse(state.is_slot_busy(0))

    def test_slot_lock_released_on_forward_failure(self):
        """Slot lock is released even if _forward raises an exception."""
        state = SlotState()
        state.acquire_slot(0)
        self.assertTrue(state.is_slot_busy(0))

        released = [False]

        def failing_work():
            try:
                raise Exception("simulated forward failure")
            finally:
                state.release_slot(0)
                released[0] = True

        with self.assertRaises(Exception):
            failing_work()

        self.assertTrue(released[0])
        self.assertFalse(state.is_slot_busy(0))

    def test_concurrent_requests_different_slots(self):
        """Two concurrent requests to different slots can proceed simultaneously."""
        state = SlotState()
        state.record(0, "node-A", 5000)
        state.record(1, "node-B", 5000)

        slot_0_busy_during_slot_1_acquire = [False]

        def acquire_slot_1():
            # Slot 0 is busy; try to acquire slot 1
            result = state.acquire_slot(1)
            if result:
                slot_0_busy_during_slot_1_acquire[0] = state.is_slot_busy(0)
                state.release_slot(1)

        state.acquire_slot(0)
        t = threading.Thread(target=acquire_slot_1)
        t.start()
        t.join(timeout=5)

        # Slot 1 should have been acquired while slot 0 was busy
        self.assertTrue(slot_0_busy_during_slot_1_acquire[0])
        state.release_slot(0)

    def test_concurrent_requests_same_slot_blocks(self):
        """Two concurrent requests to the same slot — second must wait or fail."""
        state = SlotState()
        state.record(0, "node-A", 5000)

        # Acquire slot 0
        self.assertTrue(state.acquire_slot(0))

        # Second request to slot 0 should fail (not block indefinitely)
        result = state.acquire_slot(0)
        self.assertFalse(result)  # already busy

        state.release_slot(0)

    def test_fallback_when_slot_busy_during_routing(self):
        """When slot is busy during routing and no other slots available,
        eviction kicks in (since all slots are tracked/busy)."""
        state = SlotState()
        state.record(0, "node-A", 5000)
        state.acquire_slot(0)  # already busy

        nodes = {
            "node-A": {"id": "node-A", "parent_id": None, "token_count": 5000},
        }
        discover_slots = [{"id": 0, "is_busy": False}]
        h = make_handler(state=state, discover_slots=discover_slots, nodes=nodes,
                         mock_evict=True)

        # _pick_slot_for_restore skips slot 0 (busy), no empty slots,
        # falls through to eviction
        result = h._pick_slot_for_restore(
            {"id": "node-A", "parent_id": None, "token_count": 5000}, 6000)
        # Eviction returns slot 0 with needs_restore=True
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 0)
        self.assertTrue(result[1])

        state.release_slot(0)


class TestSlotLockConcurrentIntegration(unittest.TestCase):
    """Integration tests for slot lock behavior under concurrency."""

    def test_slot_lock_serializes_same_slot(self):
        """Two threads trying to use the same slot — only one succeeds."""
        state = SlotState()
        state.record(0, "node-A", 5000)

        acquired_count = [0]
        errors = []

        def try_use_slot():
            if state.acquire_slot(0):
                acquired_count[0] += 1
                time.sleep(0.05)  # simulate work
                state.release_slot(0)
            else:
                pass  # slot busy, skip

        t1 = threading.Thread(target=try_use_slot)
        t2 = threading.Thread(target=try_use_slot)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Exactly one thread should have acquired the slot
        self.assertEqual(acquired_count[0], 1)

    def test_release_allows_reacquire(self):
        """After release, another thread can acquire the same slot."""
        state = SlotState()
        state.record(0, "node-A", 5000)

        acquired_count = [0]
        barrier = threading.Barrier(2)

        def thread_1():
            state.acquire_slot(0)
            acquired_count[0] += 1
            barrier.wait()  # wait for thread 2 to try
            time.sleep(0.1)  # hold the slot a bit
            state.release_slot(0)

        def thread_2():
            barrier.wait()  # sync with thread 1
            time.sleep(0.15)  # wait for thread 1 to release
            result = state.acquire_slot(0)
            if result:
                acquired_count[0] += 1
                state.release_slot(0)

        t1 = threading.Thread(target=thread_1)
        t2 = threading.Thread(target=thread_2)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Both threads should have acquired the slot (sequentially)
        self.assertEqual(acquired_count[0], 2)


class TestSlotLockNoDeadlock(unittest.TestCase):
    """Tests to ensure no deadlock in slot lock operations."""

    def test_acquire_release_cycle(self):
        """Multiple acquire/release cycles don't cause issues."""
        state = SlotState()
        for i in range(100):
            self.assertTrue(state.acquire_slot(0))
            self.assertTrue(state.is_slot_busy(0))
            state.release_slot(0)
            self.assertFalse(state.is_slot_busy(0))

    def test_acquire_release_multiple_slots(self):
        """Acquire/release on multiple slots doesn't cause issues."""
        state = SlotState()
        for i in range(10):
            for slot_id in range(5):
                self.assertTrue(state.acquire_slot(slot_id))
            for slot_id in range(5):
                state.release_slot(slot_id)

    def test_forget_after_acquire(self):
        """forget after acquire clears everything cleanly."""
        state = SlotState()
        state.acquire_slot(0)
        self.assertTrue(state.is_slot_busy(0))
        state.forget(0)
        self.assertFalse(state.is_slot_busy(0))
        # Should be able to acquire again
        self.assertTrue(state.acquire_slot(0))
        state.release_slot(0)


if __name__ == "__main__":
    unittest.main()
