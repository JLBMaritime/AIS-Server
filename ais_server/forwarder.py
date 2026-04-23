"""Endpoint forwarder.

Each configured endpoint gets its own thread + bounded queue.  If an endpoint
is slow, full, or unreachable, it **only** affects that endpoint — never the
pipeline or the other endpoints.

Protocols
---------
* ``tcp`` – persistent client connection; reconnects with exponential backoff
            (capped at 30 s).
* ``udp`` – fire-and-forget datagrams (scaffolded, disabled in UI by default).
* ``http``– POST each batch as ``text/plain`` (scaffolded).
"""
from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import requests

from .db import Endpoint
from .events import EventBus

log = logging.getLogger(__name__)

_MAX_BACKOFF = 30.0


@dataclass
class EndpointStats:
    sent: int = 0
    dropped: int = 0
    errors: int = 0
    last_error: str = ""
    connected: bool = False
    last_send: float = 0.0
    queue_depth: int = 0


# ---------------------------------------------------------------------------
# Per-endpoint worker
# ---------------------------------------------------------------------------
class _EndpointWorker(threading.Thread):
    def __init__(self, ep: Endpoint, queue_size: int, events: EventBus) -> None:
        super().__init__(name=f"fwd-{ep.name}", daemon=True)
        self.ep = ep
        self.queue: queue.Queue[str] = queue.Queue(maxsize=queue_size)
        self.stats = EndpointStats()
        self.events = events
        self._stop = threading.Event()
        self._sock: Optional[socket.socket] = None

    # ------------------------------------------------------------------
    def put(self, sentence: str) -> None:
        try:
            self.queue.put_nowait(sentence)
        except queue.Full:
            # Drop oldest so newest still delivers.
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(sentence)
            except queue.Full:
                self.stats.dropped += 1

    def stop(self) -> None:
        self._stop.set()
        try:
            self.queue.put_nowait("")  # wake loop
        except queue.Full:
            pass

    # ------------------------------------------------------------------
    def run(self) -> None:
        handlers = {
            "tcp":  self._run_tcp,
            "udp":  self._run_udp,
            "http": self._run_http,
        }
        fn = handlers.get(self.ep.protocol, self._run_tcp)
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("Forwarder %s crashed", self.ep.name)
        finally:
            self._close_sock()

    # ------------------------------------------------------------------
    # TCP
    # ------------------------------------------------------------------
    def _run_tcp(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self._sock:
                try:
                    self._sock = socket.create_connection(
                        (self.ep.host, self.ep.port), timeout=5)
                    self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.stats.connected = True
                    log.info("Forwarder %s connected to %s:%d",
                             self.ep.name, self.ep.host, self.ep.port)
                    backoff = 1.0
                except OSError as exc:
                    self.stats.connected = False
                    self.stats.errors += 1
                    self.stats.last_error = str(exc)
                    log.warning("Forwarder %s cannot connect (%s) – retry in %.1fs",
                                self.ep.name, exc, backoff)
                    time.sleep(backoff)
                    backoff = min(_MAX_BACKOFF, backoff * 2)
                    continue
            try:
                sentence = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if not sentence:
                continue
            data = (sentence + "\r\n").encode("ascii", errors="ignore")
            try:
                self._sock.sendall(data)
                self.stats.sent += 1
                self.stats.last_send = time.time()
                self.stats.queue_depth = self.queue.qsize()
                self.events.publish("outgoing", {
                    "endpoint_id": self.ep.id,
                    "endpoint": self.ep.name,
                    "sentence": sentence,
                    "ts": self.stats.last_send,
                })
            except OSError as exc:
                self.stats.connected = False
                self.stats.errors += 1
                self.stats.last_error = str(exc)
                log.warning("Forwarder %s send failed: %s", self.ep.name, exc)
                self._close_sock()
                # Re-queue the sentence so we don't lose it on the reconnect.
                try:
                    self.queue.put_nowait(sentence)
                except queue.Full:
                    self.stats.dropped += 1

    # ------------------------------------------------------------------
    # UDP  (scaffold – not exposed in UI by default)
    # ------------------------------------------------------------------
    def _run_udp(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.stats.connected = True
        while not self._stop.is_set():
            try:
                sentence = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if not sentence:
                continue
            try:
                self._sock.sendto((sentence + "\r\n").encode("ascii", "ignore"),
                                  (self.ep.host, self.ep.port))
                self.stats.sent += 1
                self.stats.last_send = time.time()
                self.events.publish("outgoing", {
                    "endpoint_id": self.ep.id, "endpoint": self.ep.name,
                    "sentence": sentence, "ts": self.stats.last_send,
                })
            except OSError as exc:
                self.stats.errors += 1
                self.stats.last_error = str(exc)

    # ------------------------------------------------------------------
    # HTTP (scaffold)
    # ------------------------------------------------------------------
    def _run_http(self) -> None:
        url = f"http://{self.ep.host}:{self.ep.port}{self.ep.path or '/'}"
        self.stats.connected = True
        batch: List[str] = []
        last_flush = time.time()
        while not self._stop.is_set():
            try:
                sentence = self.queue.get(timeout=1.0)
                if sentence:
                    batch.append(sentence)
            except queue.Empty:
                pass
            now = time.time()
            if batch and (len(batch) >= 100 or now - last_flush > 1.0):
                try:
                    requests.post(url, data="\r\n".join(batch),
                                  timeout=5,
                                  headers={"Content-Type": "text/plain"})
                    self.stats.sent += len(batch)
                    self.stats.last_send = now
                except Exception as exc:  # noqa: BLE001
                    self.stats.errors += 1
                    self.stats.last_error = str(exc)
                batch.clear()
                last_flush = now

    # ------------------------------------------------------------------
    def _close_sock(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self.stats.connected = False


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
class ForwarderManager:
    def __init__(self, queue_size: int, events: EventBus) -> None:
        self.queue_size = queue_size
        self.events = events
        self._workers: Dict[int, _EndpointWorker] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def sync(self, endpoints: List[Endpoint]) -> None:
        """Align running workers with the DB state (enabled/disabled/changed)."""
        with self._lock:
            existing = set(self._workers.keys())
            wanted = {ep.id: ep for ep in endpoints if ep.enabled}
            # Stop removed / disabled / changed workers.
            for eid in list(existing):
                ep = wanted.get(eid)
                w = self._workers[eid]
                if ep is None or not self._endpoint_match(ep, w.ep):
                    log.info("Stopping forwarder %s", w.ep.name)
                    w.stop()
                    w.join(timeout=3)
                    del self._workers[eid]
            # Start new / restarted workers.
            for eid, ep in wanted.items():
                if eid not in self._workers:
                    log.info("Starting forwarder %s -> %s:%d (%s)",
                             ep.name, ep.host, ep.port, ep.protocol)
                    w = _EndpointWorker(ep, self.queue_size, self.events)
                    w.start()
                    self._workers[eid] = w

    @staticmethod
    def _endpoint_match(a: Endpoint, b: Endpoint) -> bool:
        return (a.protocol == b.protocol and a.host == b.host
                and a.port == b.port and a.path == b.path
                and a.name == b.name and a.enabled == b.enabled)

    # ------------------------------------------------------------------
    def dispatch(self, sentence: str) -> None:
        """Fan out a sentence to every running worker."""
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            w.put(sentence)

    # ------------------------------------------------------------------
    def stats(self) -> list[dict]:
        out = []
        with self._lock:
            for w in self._workers.values():
                s = w.stats
                out.append({
                    "id": w.ep.id, "name": w.ep.name,
                    "protocol": w.ep.protocol,
                    "host": w.ep.host, "port": w.ep.port,
                    "enabled": w.ep.enabled,
                    "connected": s.connected,
                    "sent": s.sent, "dropped": s.dropped, "errors": s.errors,
                    "last_error": s.last_error,
                    "last_send": s.last_send,
                    "queue_depth": w.queue.qsize(),
                })
        return out

    def stop_all(self) -> None:
        with self._lock:
            for w in self._workers.values():
                w.stop()
            self._workers.clear()

    # ------------------------------------------------------------------
    @staticmethod
    def test_endpoint(ep: Endpoint, sentence: str = "!AIVDM,1,1,,A,13aEOK?P00PD2wVMdLDRhgvL289?,0*26"
                      ) -> tuple[bool, str]:
        """Blocking, synchronous one-shot test – used by the UI Test button."""
        try:
            if ep.protocol == "tcp":
                with socket.create_connection((ep.host, ep.port), timeout=5) as s:
                    s.sendall((sentence + "\r\n").encode("ascii"))
                return True, "Sent"
            if ep.protocol == "udp":
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.sendto((sentence + "\r\n").encode("ascii"),
                             (ep.host, ep.port))
                return True, "Sent"
            if ep.protocol == "http":
                url = f"http://{ep.host}:{ep.port}{ep.path or '/'}"
                r = requests.post(url, data=sentence, timeout=5)
                return (200 <= r.status_code < 300,
                        f"HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return False, "Unknown protocol"
