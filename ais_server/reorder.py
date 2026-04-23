"""Chronological re-order buffer (aka jitter buffer).

Items are pushed in any order, then popped in timestamp order once they have
been held for at least ``hold_ms`` milliseconds.  This is exactly how
real-time audio/video jitter buffers work and it is the canonical solution
for "sort a live stream chronologically with bounded latency".

Characteristics
---------------
* **Heap-based** – :mod:`heapq` gives O(log N) push / pop.
* **Bounded**    – ``max_queue`` caps memory; the oldest item is dropped if
                   we ever exceed it (should never happen in practice).
* **Thread-safe** – a single lock guards the heap.
* **Latency**    – each item is held at most ``hold_ms`` ms, well below the
                   10 s budget in the brief.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

log = logging.getLogger(__name__)


@dataclass(order=True)
class _HeapItem:
    ts: float
    seq: int
    payload: Any = field(compare=False)


class ReorderBuffer:
    def __init__(self, hold_ms: int = 2000, max_queue: int = 50_000) -> None:
        self.hold_s = hold_ms / 1000.0
        self.max_queue = max_queue
        self._heap: List[_HeapItem] = []
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self.pushed = 0
        self.popped = 0
        self.dropped = 0

    # ------------------------------------------------------------------
    def push(self, payload: Any, ts: Optional[float] = None) -> None:
        ts = ts if ts is not None else time.time()
        item = _HeapItem(ts, next(self._seq), payload)
        with self._cv:
            if len(self._heap) >= self.max_queue:
                # Safety valve – shouldn't happen with sane config.
                oldest = heapq.heappop(self._heap)
                self.dropped += 1
                log.warning("Reorder queue full – dropped oldest ts=%.3f", oldest.ts)
            heapq.heappush(self._heap, item)
            self.pushed += 1
            self._cv.notify()

    # ------------------------------------------------------------------
    def pop_ready(self, now: Optional[float] = None) -> List[Any]:
        """Return and remove every item whose hold time has expired."""
        now = now if now is not None else time.time()
        cutoff = now - self.hold_s
        out: List[Any] = []
        with self._lock:
            while self._heap and self._heap[0].ts <= cutoff:
                item = heapq.heappop(self._heap)
                out.append(item.payload)
                self.popped += 1
        return out

    # ------------------------------------------------------------------
    def wait_next(self, timeout: float) -> None:
        """Block up to ``timeout`` seconds or until the next item is ready."""
        with self._cv:
            if not self._heap:
                self._cv.wait(timeout=timeout)
                return
            head_ts = self._heap[0].ts
            sleep = max(0.0, (head_ts + self.hold_s) - time.time())
            sleep = min(sleep, timeout)
            if sleep > 0:
                self._cv.wait(timeout=sleep)

    # ------------------------------------------------------------------
    def qsize(self) -> int:
        with self._lock:
            return len(self._heap)

    def stats(self) -> dict:
        with self._lock:
            size = len(self._heap)
        return {"queue_size": size, "pushed": self.pushed,
                "popped": self.popped, "dropped": self.dropped}
