# Ticket lcl-l89j: Per-slot busy tracking to prevent concurrent routing

## Problem Statement

`ThreadingHTTPServer` handles requests concurrently. `_cache_lock` serializes `_lookup_and_restore_prefix` and `_auto_save_prefix_cache`, but **NOT** the backend processing window. Two requests can be routed to the same slot back-to-back before either finishes — when MTP truncates on one, checkpoints for the other are invalidated with no way to detect it.

### Root Cause Flow

1. **Thread 1**: Session A arrives. Acquires `_cache_lock`. Routes to slot 0 (ancestor match or restore). Releases `_cache_lock`.
2. **Thread 1**: Forwards request to llama.cpp. Backend starts processing — this takes time (especially for long prompts with MTP speculative decoding).
3. **Thread 2**: Session B arrives (different conversation, different prompt). Acquires `_cache_lock`. Routes to slot 0 (ancestor match — because slot 0 is idle from the backend's perspective; the `/slots` endpoint reports `is_busy=False` since llama.cpp uses its own internal busy tracking, and the previous request may have already transitioned to processing state).
4. **Thread 2**: Forwards request to llama.cpp with `id_slot=0`. Backend processes Session B's prompt. The common prefix is shorter than Session A's, so `pos_next` is lower.
5. **MTP truncation**: Checkpoints with `pos_max > pos_next` are erased — destroying Session A's checkpoints.
6. **Thread 1**: Session A's response continues streaming, but its checkpoints are gone. No way to recover.

### Why `_cache_lock` doesn't help

The lock only protects the routing decision and autosave. The critical gap is:

```
[LOCK] _lookup_and_restore_prefix → route to slot 0     [UNLOCK]
       ↓
       _forward() → streams response (takes seconds)    ← NO LOCK
       ↓
[LOCK] _auto_save_prefix_cache                          [UNLOCK]
```

Between `[UNLOCK]` after routing and the end of `_forward()`, another thread can route to the same slot.

### Why backend `is_busy` isn't enough

The `/slots` endpoint reports `is_busy` from llama.cpp's perspective. But:
1. The proxy's `_idle_slots()` already filters out busy slots — but the slot may not be marked busy yet when Thread 2 checks (race between Thread 1 releasing the lock and llama.cpp marking the slot busy).
2. Even if the backend marks the slot busy, there's a window where the proxy has released the lock and the backend hasn't acquired its own.
3. The backend's busy state is per-request — it transitions to `processing` → `idle`. The proxy doesn't wait for this cycle.

## Design Decision: Per-slot busy tracking in the proxy

### Option A: Extend `_cache_lock` to cover the entire request lifecycle

Hold `_cache_lock` from routing through `_forward()` and autosave.

**Pros:**
- Simple. No new state to track.
- Guarantees mutual exclusion.

**Cons:**
- **Serializes all requests**. With MTP speculative decoding, responses can take 10+ seconds. This turns the proxy into a bottleneck — no other request can be processed while one is generating.
- Defeats the purpose of `ThreadingHTTPServer`.

### Option B: Per-slot lock with busy flag in `slot_state`

Add a per-slot busy flag to `SlotState`. Mark a slot busy when routed to it, clear when `_forward()` completes. During routing, treat busy slots as unavailable.

**Pros:**
- Allows concurrent requests to different slots.
- Only blocks routing to the same slot.
- Minimal overhead — just a dict check and Lock acquire/release per slot.

**Cons:**
- Adds complexity to `SlotState` (new methods, new state).
- Must handle edge cases: what if `_forward()` fails? Must clear busy flag.
- Doesn't prevent the backend from processing two requests on the same slot if the backend's own busy tracking is bypassed (but this is unlikely with proper `id_slot` routing).

### Option C: Per-slot lock without busy flag (use Lock directly)

Instead of a busy flag, use a per-slot threading.Lock. Acquire before routing to a slot, release after `_forward()` completes.

**Pros:**
- Stronger guarantee — no busy flag race condition.
- Automatically handles edge cases (lock is always released via `finally`).

**Cons:**
- More overhead than a simple flag check.
- Lock contention if many requests target the same slot.
- Can't inspect state from outside (e.g., debugging which slots are locked).

### Option D: Hybrid — busy flag + per-slot lock

Use both: a per-slot Lock for mutual exclusion AND a busy flag for visibility/debugging.

**Pros:**
- Best of both worlds.

**Cons:**
- More complex. Two mechanisms doing similar things.

### Recommendation: Option C (per-slot Lock)

This is the cleanest solution:
1. A per-slot Lock ensures true mutual exclusion — no busy flag race condition.
2. Using `finally` guarantees the lock is released even if `_forward()` fails.
3. The proxy already uses `Lock` objects (see `_cache_lock`, `SlotState._lock`).
4. We can **also** add a busy flag for debugging/visibility, but it's secondary.

## Implementation Plan

### Changes to `SlotState` class (lines ~173-235)

Add per-slot busy tracking:

```python
class SlotState:
    def __init__(self):
        self._slot_node_id: dict[int, str] = {}
        self._slot_tokens: dict[int, int] = {}
        self._slot_time: dict[int, float] = {}
        self._slot_busy: dict[int, bool] = {}     # NEW: slot_id → is_busy
        self._slot_locks: dict[int, Lock] = {}    # NEW: per-slot locks
        self._lock = Lock()
```

Add methods:

```python
def acquire_slot(self, slot_id: int) -> bool:
    """Try to acquire a slot for exclusive use.
    
    Returns True if the slot was acquired, False if already busy.
    Creates a per-slot lock if needed.
    """
    with self._lock:
        if self._slot_busy.get(slot_id, False):
            return False
        if slot_id not in self._slot_locks:
            self._slot_locks[slot_id] = Lock()
        self._slot_busy[slot_id] = True
    # Acquire the per-slot lock (outside _lock to avoid deadlock)
    self._slot_locks[slot_id].acquire()
    return True

def release_slot(self, slot_id: int) -> None:
    """Release a slot after processing completes."""
    with self._lock:
        if slot_id in self._slot_locks:
            try:
                self._slot_locks[slot_id].release()
            except RuntimeError:
                pass  # Lock was already released
        self._slot_busy[slot_id] = False

def is_slot_busy(self, slot_id: int) -> bool:
    """Check if a slot is currently in use."""
    with self._lock:
        return self._slot_busy.get(slot_id, False)
```

### Changes to `_idle_slots()` (lines ~1041-1050)

The current implementation filters out backend-busy slots. Also filter out proxy-busy slots:

```python
def _idle_slots(self) -> list[int] | None:
    """Return ids of slots that are idle (not busy), or None in single-slot mode."""
    slots = self._discover_slots()
    if slots is None:
        return None
    backend_idle = [s["id"] for s in slots if not s.get("is_busy", False)]
    # Also filter out slots that are busy from the proxy's perspective
    proxy_idle = [sid for sid in backend_idle 
                  if self.slot_state is None or not self.slot_state.is_slot_busy(sid)]
    return proxy_idle
```

### Changes to `_empty_slots()` (lines ~1052-1062)

No change needed — `_empty_slots()` calls `_idle_slots()`, which now includes proxy-busy filtering.

### Changes to `_pick_slot_for_restore()` (lines ~684-810)

The ancestor match path should check if the slot is busy before routing:

In the ancestor match section (around line 735):

```python
if best_slot is not None:
    # Check if the slot is available (not busy from proxy's perspective)
    if self.slot_state.is_slot_busy(best_slot):
        log.debug("prefix-cache ancestor match slot %d busy, skipping", best_slot)
        return None  # Let caller fall through to empty slot check
    
    slot_tok = self.slot_state.tokens_for(best_slot)
    if slot_tok is not None and req_tokens >= slot_tok:
        # Ancestor match: slot already has a prefix of the matched node.
        return (best_slot, False)
```

Similarly for the sibling match section (around line 795):

```python
if best_slot is not None:
    if self.slot_state.is_slot_busy(best_slot):
        log.debug("prefix-cache sibling match slot %d busy, skipping", best_slot)
        return None  # Fall through to empty slot check
    
    slot_tok = self.slot_state.tokens_for(best_slot)
    if slot_tok is not None and req_tokens >= slot_tok:
        return (best_slot, True)
```

### Changes to `_handle_request()` (lines ~1401-1445)

This is the critical change. Acquire the slot lock before forwarding, release after:

```python
def _handle_request(self, method: str):
    # ... existing code ...
    
    target_slot: int | None = None
    ctx = self._request_cache_context(path, body) if method == "POST" else None
    if ctx is not None:
        # ... existing code ...
        with self._cache_lock:
            target_slot = self._lookup_and_restore_prefix(ctx)
    else:
        self._restore_legacy_cache(body)
    
    # Inject id_slot when multi-slot routing is enabled and we have a target slot
    if target_slot is not None:
        body["id_slot"] = target_slot
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        log.debug("routed to slot %d", target_slot)
    
    # Acquire the slot lock before forwarding
    acquired = False
    if target_slot is not None and self.slot_state is not None:
        acquired = self.slot_state.acquire_slot(target_slot)
        if not acquired:
            log.warning("slot %d busy, falling back to no-slot routing", target_slot)
            # Optionally: re-route without slot lock (risky) or return error
    
    try:
        result = self._forward(method, path, body_bytes)
    finally:
        # Always release the slot lock
        if acquired and self.slot_state is not None:
            self.slot_state.release_slot(target_slot)
    
    if ctx is not None:
        try:
            with self._cache_lock:
                self._auto_save_prefix_cache(ctx, body, result, target_slot)
        except Exception as e:
            log.warning("prefix-cache autosave failed gracefully: %s", e)
    return result
```

### Changes to `_lookup_and_restore_prefix()` (lines ~814-920)

The slot lock should be acquired **before** routing, not after. Currently, the flow is:

```
[LOCK] _lookup_and_restore_prefix → route → restore → record    [UNLOCK]
```

With per-slot locking, we need to acquire the slot lock inside `_lookup_and_restore_prefix`, but this creates a risk: if we acquire the slot lock and then fail to restore (e.g., KV cache full), we don't want to hold the slot busy indefinitely.

**Revised flow:**

```
[LOCK] _lookup_and_restore_prefix → pick slot → acquire slot lock → restore → record    [UNLOCK]
```

If restore fails, release the slot lock and return None (caller will route without cache).

### Edge Cases

1. **Slot lock acquired but `_forward()` fails**: The `finally` block ensures `release_slot()` is called.
2. **Slot lock acquired but autosave fails**: Autosave runs after `_forward()`, so the slot lock is already released by then. No issue.
3. **Multiple requests for the same conversation**: If Session A sends request 1, then request 2 before request 1 completes, request 2 will see slot 0 as busy and fall through to empty slots (if available) or skip cache entirely. This is correct — we don't want two requests on the same slot.
4. **What if `_pick_slot_for_restore` returns None?**: No slot lock is acquired. The request proceeds without cache. Correct behavior.
5. **What about single-slot mode?**: In single-slot mode, `slot_state` is None. No per-slot locking. All requests share the same slot, which is the existing (broken) behavior. Single-slot mode is inherently sequential — if you want concurrency, use multi-slot mode.

### Alternative: Slot lock in `_pick_slot_for_restore`

Another approach is to acquire the slot lock inside `_pick_slot_for_restore`, right after picking a slot:

```python
def _pick_slot_for_restore(self, node, req_tokens):
    # ... existing ancestor match logic ...
    
    if best_slot is not None and not self.slot_state.is_slot_busy(best_slot):
        if not self.slot_state.acquire_slot(best_slot):
            return None  # Slot became busy between check and acquire
        # ... rest of routing ...
```

This is cleaner because the lock is acquired at the point of decision, not after. But it means `_lookup_and_restore_prefix` needs to release the slot lock if restore fails.

### Lock Ordering (avoiding deadlock)

The proxy uses multiple locks:
1. `_cache_lock` — protects routing and autosave
2. `SlotState._lock` — protects slot_state internal state
3. Per-slot locks — protect slot busy state

**Lock ordering to avoid deadlock:**
- `_cache_lock` is the outermost lock
- Within `_cache_lock`, we call `slot_state.acquire_slot()` which acquires `SlotState._lock` then the per-slot lock
- After releasing `_cache_lock`, we hold only the per-slot lock during `_forward()`

This is safe because:
- We never acquire `_cache_lock` while holding a per-slot lock
- We never acquire `SlotState._lock` while holding a per-slot lock (the per-slot lock is acquired inside `acquire_slot()`, after releasing `SlotState._lock`)

## Impact Assessment

- **Risk**: Medium. Adding locks introduces potential deadlock if lock ordering is violated. The lock ordering above is safe, but testing is critical.
- **Testing**: Verify that two concurrent requests to different slots proceed concurrently. Verify that two concurrent requests to the same slot serialize correctly (second waits or falls through).
- **Performance**: Minimal overhead — one Lock acquire/release per routed request. No impact on non-routed requests.

## Acceptance Criteria

1. Two concurrent requests to different slots proceed without blocking each other.
2. Two concurrent requests that would route to the same slot do not both proceed — the second either waits or falls through to a different slot/no-cache routing.
3. No deadlock under high concurrency (stress test with 10+ concurrent requests).
4. Slot lock is always released, even if `_forward()` fails.
5. Single-slot mode continues to work (no per-slot locking when `slot_state` is None).

