---
id: lcl-notu
status: open
deps: []
links: []
created: 2026-07-05T04:25:11Z
type: bug
priority: 1
assignee: memgrafter
tags: [proxy, slot-routing]
---
# Slot lock/semaphore for try_disk_cache — prevent race when multiple requests target the same idle slot

try_disk_cache picks an idle slot and restores KV into it, but another handler thread could grab that same slot between the idle check and the restore. Need a per-slot lock (semaphore) with: expiry tied to request lifecycle, proper unlock on any failure mode, and fallback to try other slots if locked. Consider OS-level file locking or tying lock to a pollable resource like the actual in-flight request.
