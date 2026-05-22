"""Disk-space / host-resource self-check.

Two responsibilities:

* Run a background thread that wakes every ``interval`` seconds, samples the
  free space on the data filesystem (default ``/var``) and emits an
  ``ERROR`` log line if it drops below ``low_water_mb`` or a ``WARNING``
  below ``warn_water_mb``.  This is what catches a slowly-filling SD card
  *before* it wedges the service.
* Provide a synchronous ``snapshot()`` used by the web ``/api/system/info``
  endpoint and the Dashboard so a human can see disk / RAM / journal / DB
  size at a glance.

Designed to be safe on any POSIX system; gracefully degrades if ``psutil``
or the journal directory aren't available (e.g. running on Windows for a
quick test).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional

try:
    import psutil  # type: ignore
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore

log = logging.getLogger(__name__)


def _du_bytes(path: str) -> int:
    """Best-effort recursive directory size (bytes).  Returns 0 on error."""
    p = Path(path)
    if not p.exists():
        return 0
    total = 0
    try:
        for root, _dirs, files in os.walk(p, onerror=lambda _e: None):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def _journal_usage_bytes() -> Optional[int]:
    """Ask journalctl how much space the journal is using.  None if unknown."""
    try:
        out = subprocess.run(
            ["journalctl", "--disk-usage"], capture_output=True, text=True,
            timeout=4,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    # Output looks like: "Archived and active journals take up 123.4M in the file system."
    # Robustly extract the first numeric+unit pair.
    import re
    m = re.search(r"([\d.]+)\s*([KMGT])", out)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[unit]
    return int(val * mult)


class DiskCheck:
    """Periodic disk-space watchdog + host-info snapshot provider."""

    def __init__(self,
                 data_path: str = "/var",
                 db_path: Optional[str] = None,
                 interval: float = 60.0,
                 low_water_mb: int = 500,
                 warn_water_mb: int = 1500) -> None:
        self.data_path = data_path
        self.db_path = db_path
        self.interval = interval
        self.low_water = low_water_mb * 1024 * 1024
        self.warn_water = warn_water_mb * 1024 * 1024
        self._stop = threading.Event()
        # Latch state so we log transitions, not every cycle.
        self._state: str = "ok"     # ok | warn | low
        self._last_snapshot: Dict[str, object] = {}
        self._snap_lock = threading.Lock()

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def _disk_usage(self, path: str) -> Optional[Dict[str, int]]:
        try:
            u = shutil.disk_usage(path)
            return {"total": u.total, "used": u.used, "free": u.free,
                    "percent": int((u.used / u.total) * 100) if u.total else 0}
        except OSError:
            return None

    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, object]:
        """Synchronous, cheap-ish info bundle.  Safe to call from the web API."""
        info: Dict[str, object] = {}

        # Disk
        disk = self._disk_usage(self.data_path) or self._disk_usage("/")
        info["disk"] = {"path": self.data_path, **(disk or {})}

        # Journal
        journal_bytes = _journal_usage_bytes()
        info["journal"] = {
            "bytes": journal_bytes,
            "path": "/var/log/journal",
        }

        # DB + WAL
        db_info: Dict[str, object] = {"path": self.db_path or ""}
        if self.db_path:
            for key, suffix in (("db", ""), ("wal", "-wal"), ("shm", "-shm")):
                p = Path(self.db_path + suffix)
                try:
                    db_info[key + "_bytes"] = p.stat().st_size if p.exists() else 0
                except OSError:
                    db_info[key + "_bytes"] = 0
        info["db"] = db_info

        # Memory + CPU + load + process
        if psutil is not None:
            try:
                vm = psutil.virtual_memory()
                info["memory"] = {
                    "total": int(vm.total), "available": int(vm.available),
                    "used": int(vm.used), "percent": float(vm.percent),
                }
            except Exception:  # noqa: BLE001
                info["memory"] = {}
            try:
                info["cpu_percent"] = float(psutil.cpu_percent(interval=None))
            except Exception:  # noqa: BLE001
                info["cpu_percent"] = None
            try:
                la = os.getloadavg()
                info["loadavg"] = [round(la[0], 2), round(la[1], 2),
                                   round(la[2], 2)]
            except (OSError, AttributeError):
                info["loadavg"] = None
            try:
                proc = psutil.Process()
                with proc.oneshot():
                    info["process"] = {
                        "rss": int(proc.memory_info().rss),
                        "threads": proc.num_threads(),
                        "fds": proc.num_fds() if hasattr(proc, "num_fds") else None,
                        "create_time": int(proc.create_time()),
                    }
            except Exception:  # noqa: BLE001
                info["process"] = {}
        else:
            info["memory"] = {}
            info["cpu_percent"] = None
            info["loadavg"] = None
            info["process"] = {}

        info["state"] = self._state
        info["ts"] = time.time()
        with self._snap_lock:
            self._last_snapshot = info
        return info

    # ------------------------------------------------------------------
    def last(self) -> Dict[str, object]:
        with self._snap_lock:
            return dict(self._last_snapshot)

    # ------------------------------------------------------------------
    def run(self) -> None:
        log.info("diskcheck: starting (path=%s, warn<%dMB, low<%dMB, interval=%ds)",
                 self.data_path, self.warn_water // (1024 * 1024),
                 self.low_water  // (1024 * 1024), int(self.interval))
        # Prime the cached snapshot immediately so /api/system/info works
        # straight away.
        self.snapshot()
        while not self._stop.wait(self.interval):
            try:
                snap = self.snapshot()
            except Exception:  # noqa: BLE001
                log.exception("diskcheck: snapshot failed")
                continue
            disk = snap.get("disk") or {}
            free = int(disk.get("free", 0) or 0)
            new_state = "ok"
            if free and free < self.low_water:
                new_state = "low"
            elif free and free < self.warn_water:
                new_state = "warn"
            if new_state != self._state:
                free_mb = free // (1024 * 1024)
                if new_state == "low":
                    log.error("diskcheck: LOW disk space on %s – %d MB free",
                              self.data_path, free_mb)
                elif new_state == "warn":
                    log.warning("diskcheck: disk space on %s getting tight – "
                                "%d MB free", self.data_path, free_mb)
                else:
                    log.info("diskcheck: disk space on %s recovered – %d MB free",
                             self.data_path, free_mb)
                self._state = new_state
