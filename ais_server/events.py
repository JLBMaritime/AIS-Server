"""Tiny thread-safe pub/sub used to bridge pipeline events to the web UI.

Keeping this out of the hot path means the pipeline never touches Flask or
Socket.IO directly – a slow/broken subscriber can never back-pressure ingest.
"""
from __future__ import annotations

import threading
from collections import defaultdict, deque
from typing import Any, Callable, Deque, Dict, List


class EventBus:
    def __init__(self, ring_size: int = 500) -> None:
        self._subs: Dict[str, List[Callable[[Any], None]]] = defaultdict(list)
        self._rings: Dict[str, Deque[Any]] = defaultdict(lambda: deque(maxlen=ring_size))
        self._lock = threading.Lock()

    def subscribe(self, topic: str, callback: Callable[[Any], None]) -> None:
        with self._lock:
            self._subs[topic].append(callback)

    def publish(self, topic: str, payload: Any) -> None:
        with self._lock:
            subs = list(self._subs.get(topic, ()))
            self._rings[topic].append(payload)
        for cb in subs:
            try:
                cb(payload)
            except Exception:  # noqa: BLE001
                # Subscribers are non-critical; ignore their failures.
                pass

    def recent(self, topic: str, n: int = 100) -> List[Any]:
        with self._lock:
            return list(self._rings.get(topic, ()))[-n:]
