"""Network self-test / auto-recovery watchdog.

Background
----------
On Raspberry Pi 4B running RPi OS Lite, the Broadcom ``brcmfmac`` Wi-Fi
firmware can silently wedge after long periods of low-traffic association.
When that happens the radio stays "connected" but no packets get through
and every network-bound thread in this process blocks in a kernel
``wait_for_completion``.  Externally the box looks like it has crashed —
even SSH stops responding — and the only fix is a hard power cycle.

This module runs a tiny supervised thread that:

1. Every ``interval`` seconds, opens an outbound TCP connection to a
   known reachable peer (default: the default-route gateway, port 53).
2. Counts consecutive failures.
3. After ``fail_threshold`` consecutive failures, logs ``WARN`` and (if
   ``auto_recover=True``) bounces ``wlan0`` via ``nmcli``.

The probe is non-fatal: failures only ever produce log lines.  The thread
itself is wrapped by ``SupervisedThread`` so any unexpected exception is
caught and restarted.

This is *not* part of the hot NMEA path.  It exists purely so the box
can heal itself instead of needing a power cycle.
"""
from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)


def _default_gateway() -> Optional[str]:
    """Return the IPv4 default-route gateway, or None."""
    try:
        with open("/proc/net/route", "r", encoding="ascii") as fh:
            next(fh)  # header
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                # Destination == 00000000 && flags has UG (0x0003)
                if parts[1] == "00000000" and (int(parts[3], 16) & 0x2):
                    gw_hex = parts[2]
                    octets = [str(int(gw_hex[i:i + 2], 16))
                              for i in (6, 4, 2, 0)]
                    return ".".join(octets)
    except (OSError, ValueError, StopIteration):
        return None
    return None


class NetCheck:
    """Self-test thread with optional auto-recovery."""

    def __init__(self,
                 interval: float = 30.0,
                 fail_threshold: int = 3,
                 timeout: float = 4.0,
                 host: Optional[str] = None,
                 port: int = 53,
                 interface: str = "wlan0",
                 auto_recover: bool = True) -> None:
        self.interval = interval
        self.fail_threshold = fail_threshold
        self.timeout = timeout
        self.host = host
        self.port = port
        self.interface = interface
        self.auto_recover = auto_recover
        self._stop = threading.Event()
        self._fails = 0
        self._last_ok = 0.0
        self._last_fail = 0.0
        self._recoveries = 0

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def _probe(self, target: str) -> bool:
        try:
            with socket.create_connection((target, self.port),
                                          timeout=self.timeout):
                return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    def _bounce_wifi(self) -> None:
        """Best-effort: cycle the Wi-Fi interface via nmcli.

        Done in a subprocess with a short timeout so a hung NetworkManager
        cannot wedge this thread too.  Any failure is logged and ignored —
        worst case we'll try again on the next interval.
        """
        if not shutil.which("nmcli"):
            log.warning("netcheck: nmcli not available – cannot auto-recover")
            return
        for args in (
            ["nmcli", "device", "disconnect", self.interface],
            ["nmcli", "device", "connect",    self.interface],
        ):
            try:
                subprocess.run(args, capture_output=True, text=True,
                               timeout=20)
            except (subprocess.TimeoutExpired, OSError) as exc:
                log.warning("netcheck: %s failed: %s", " ".join(args), exc)

    # ------------------------------------------------------------------
    def run(self) -> None:
        log.info("netcheck: starting (interval=%.0fs, threshold=%d, "
                 "auto_recover=%s)",
                 self.interval, self.fail_threshold, self.auto_recover)
        while not self._stop.wait(self.interval):
            target = self.host or _default_gateway()
            if not target:
                # No default route yet — boot/transient.  Don't count as fail.
                continue
            if self._probe(target):
                if self._fails:
                    log.info("netcheck: connectivity to %s restored "
                             "(was failing %d×)", target, self._fails)
                self._fails = 0
                self._last_ok = time.time()
                continue
            self._fails += 1
            self._last_fail = time.time()
            log.warning("netcheck: probe %s:%d failed (%d/%d)",
                        target, self.port, self._fails, self.fail_threshold)
            if self._fails >= self.fail_threshold:
                if self.auto_recover:
                    log.error("netcheck: %d consecutive failures – "
                              "bouncing %s", self._fails, self.interface)
                    self._bounce_wifi()
                    self._recoveries += 1
                # Reset so we don't bounce every cycle if it stays dead.
                self._fails = 0

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return {
            "fails": self._fails,
            "last_ok": self._last_ok,
            "last_fail": self._last_fail,
            "recoveries": self._recoveries,
        }
