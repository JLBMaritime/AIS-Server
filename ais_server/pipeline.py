"""Pipeline glue: Ingest -> Dedup -> Reorder -> Forwarder.

This is the *stateful hub* of the server.  All components live here so they
can be shared between the HTTP API, the SocketIO handlers, and the background
workers.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Deque, Dict, Any

from .dedup import Deduper
from .events import EventBus
from .forwarder import ForwarderManager
from .ingest import NodeRegistry, TcpIngest
from .nmea import extract_mmsi_and_type, extract_timestamp
from .reorder import ReorderBuffer

log = logging.getLogger(__name__)


class Pipeline:
    """Owns dedup, reorder, forwarder, node registry, and runtime stats."""
    def __init__(self, cfg: Dict[str, Any], events: EventBus,
                 forwarder: ForwarderManager) -> None:
        self.cfg = cfg
        self.events = events
        self.forwarder = forwarder
        self.dedup = Deduper(
            ttl_seconds=int(cfg["dedup"]["ttl_seconds"]),
            max_entries=int(cfg["dedup"]["max_entries"]),
        )
        self.reorder = ReorderBuffer(
            hold_ms=int(cfg["reorder"]["hold_ms"]),
            max_queue=int(cfg["reorder"]["max_queue"]),
        )
        self.nodes = NodeRegistry()

        # Rolling msgs/sec tracker – counts last 60 seconds.
        self._rate_lock = threading.Lock()
        self._rate_bucket: Deque[float] = deque(maxlen=10_000)

        # Unique MMSIs seen in the last 10 minutes (for Dashboard).
        self._mmsi_lock = threading.Lock()
        self._mmsi_last: Dict[int, float] = {}

        self.started_at = time.time()

    # ------------------------------------------------------------------
    # Ingest callback – called from ingest worker threads.
    # ------------------------------------------------------------------
    def on_sentence(self, sentence: str, peer: str, arrival_ts: float) -> None:
        # Dedup: if we've seen it before, drop it but let the reorder layer
        # know the *earliest* arrival so chronological order is preserved.
        is_new, effective_ts = self.dedup.check(sentence, arrival_ts)
        if not is_new:
            return

        # Prefer the sentence's own UTC timestamp when available (Type 4/11);
        # otherwise use the first-arrival time.
        parts = sentence.split(",")
        payload = parts[5] if len(parts) > 5 else ""
        intrinsic = None
        if payload:
            try:
                intrinsic = extract_timestamp(payload)
            except Exception:  # noqa: BLE001
                intrinsic = None
        ts = intrinsic if intrinsic is not None else effective_ts

        self.reorder.push({"sentence": sentence, "peer": peer,
                           "arrival_ts": arrival_ts}, ts=ts)

        # Rate / MMSI stats.
        with self._rate_lock:
            self._rate_bucket.append(arrival_ts)
        mmsi = None
        if payload:
            try:
                mmsi, _mt = extract_mmsi_and_type(payload)
            except Exception:  # noqa: BLE001
                mmsi = None
        if mmsi:
            with self._mmsi_lock:
                self._mmsi_last[mmsi] = arrival_ts

        # Publish to the UI incoming-data feed.
        self.events.publish("incoming", {
            "sentence": sentence, "peer": peer, "ts": arrival_ts,
            "mmsi": mmsi,
        })

    # ------------------------------------------------------------------
    # Output loop – pops chronologically-ordered sentences and forwards.
    # ------------------------------------------------------------------
    def output_loop(self) -> None:
        log.info("Output loop running (hold=%.0fms)", self.reorder.hold_s * 1000)
        while True:
            ready = self.reorder.pop_ready()
            if ready:
                for item in ready:
                    self.forwarder.dispatch(item["sentence"])
            else:
                # Block up to 250 ms or until the next item is near-ready.
                self.reorder.wait_next(timeout=0.25)

    # ------------------------------------------------------------------
    # Stats used by Dashboard + CLI.
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        now = time.time()
        with self._rate_lock:
            # keep only last 60s
            cutoff = now - 60.0
            while self._rate_bucket and self._rate_bucket[0] < cutoff:
                self._rate_bucket.popleft()
            msgs_per_sec = len(self._rate_bucket) / 60.0
        with self._mmsi_lock:
            cutoff = now - 600.0
            for m in list(self._mmsi_last):
                if self._mmsi_last[m] < cutoff:
                    del self._mmsi_last[m]
            unique_mmsi = len(self._mmsi_last)

        return {
            "uptime_seconds":  int(now - self.started_at),
            "msgs_per_sec":    round(msgs_per_sec, 2),
            "unique_mmsi":     unique_mmsi,
            "dedup":           self.dedup.stats(),
            "reorder":         self.reorder.stats(),
            "nodes":           len(self.nodes.snapshot()),
            "nodes_connected": sum(1 for n in self.nodes.snapshot() if n["connected"]),
        }
