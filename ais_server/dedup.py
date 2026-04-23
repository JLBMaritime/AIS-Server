"""In-memory duplicate detector.

Uses a :class:`cachetools.TTLCache` keyed by a SHA-1 of the canonicalised
NMEA sentence.  Thread-safe (single lock – contention is negligible because
the hash / lookup cost is dwarfed by socket I/O).

The cache stores the *arrival timestamp of the first copy* as the value.  The
reorder layer can retrieve that value so that late duplicates inherit the
earlier timestamp – this is what gives the final stream true chronological
order even when one node is several seconds faster than another.
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Optional, Tuple

from cachetools import TTLCache

from .nmea import canonicalise


class Deduper:
    def __init__(self, ttl_seconds: int = 30, max_entries: int = 200_000) -> None:
        self._cache: TTLCache = TTLCache(maxsize=max_entries, ttl=ttl_seconds)
        self._lock = threading.Lock()
        self.seen = 0
        self.duplicates = 0

    @staticmethod
    def _key(sentence: str) -> str:
        canon = canonicalise(sentence).encode("utf-8", errors="replace")
        return hashlib.sha1(canon).hexdigest()

    def check(self, sentence: str, arrival_ts: Optional[float] = None
              ) -> Tuple[bool, float]:
        """Return ``(is_new, effective_timestamp)``.

        * ``is_new``              – ``True`` the first time we see this sentence.
        * ``effective_timestamp`` – the arrival time of the *earliest* copy.
        """
        arrival_ts = arrival_ts if arrival_ts is not None else time.time()
        key = self._key(sentence)
        with self._lock:
            self.seen += 1
            existing = self._cache.get(key)
            if existing is None:
                self._cache[key] = arrival_ts
                return True, arrival_ts
            self.duplicates += 1
            return False, existing

    def stats(self) -> dict:
        with self._lock:
            size = len(self._cache)
        return {"seen": self.seen, "duplicates": self.duplicates,
                "cache_size": size,
                "dedup_rate": (self.duplicates / self.seen) if self.seen else 0.0}
