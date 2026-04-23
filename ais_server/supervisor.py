"""Thread supervisor with exponential backoff.

Wraps a callable as a daemon thread that restarts on any exception so one
buggy path can never take the whole server down.  The main loop also pings
``sd_notify`` if running under systemd with ``Type=notify``.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Callable, List

log = logging.getLogger(__name__)

_MAX_BACKOFF = 30.0


def _sd_notify(msg: str) -> None:
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        if sock_path.startswith("@"):
            sock_path = "\0" + sock_path[1:]
        s.connect(sock_path)
        s.sendall(msg.encode("utf-8"))
        s.close()
    except OSError:
        pass


class SupervisedThread(threading.Thread):
    def __init__(self, name: str, target: Callable[[], None]) -> None:
        super().__init__(name=name, daemon=True)
        self._fn = target
        self._stop = threading.Event()

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                log.info("[supervisor] %s starting", self.name)
                self._fn()
                log.info("[supervisor] %s returned – restarting", self.name)
                backoff = 1.0
            except Exception:  # noqa: BLE001
                log.exception("[supervisor] %s crashed – restarting in %.1fs",
                              self.name, backoff)
                time.sleep(backoff)
                backoff = min(_MAX_BACKOFF, backoff * 2)

    def stop(self) -> None:
        self._stop.set()


class Watchdog(threading.Thread):
    """Pings systemd every ``interval`` seconds so ``WatchdogSec`` works."""
    def __init__(self, interval: float = 10.0) -> None:
        super().__init__(name="watchdog", daemon=True)
        self.interval = interval
        self._stop = threading.Event()

    def run(self) -> None:
        _sd_notify("READY=1")
        while not self._stop.wait(self.interval):
            _sd_notify("WATCHDOG=1")

    def stop(self) -> None:
        _sd_notify("STOPPING=1")
        self._stop.set()


def run_all(threads: List[SupervisedThread], watchdog: Watchdog | None = None,
            stop_event: threading.Event | None = None) -> None:
    for t in threads:
        t.start()
    if watchdog:
        watchdog.start()
    try:
        while True:
            if stop_event and stop_event.is_set():
                break
            time.sleep(1.0)
    finally:
        for t in threads:
            t.stop()
        if watchdog:
            watchdog.stop()
