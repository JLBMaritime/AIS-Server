"""Wi-Fi configuration helpers using NetworkManager (``nmcli``).

Raspberry Pi OS Bookworm ships NetworkManager by default – ``nmcli`` is the
cleanest, most reliable way to scan / connect / forget networks and works on
headless systems.  If ``nmcli`` isn't installed we degrade gracefully so the
rest of the server still works.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)


def _run(args: List[str], timeout: int = 15) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"{args[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def _nmcli_available() -> bool:
    return shutil.which("nmcli") is not None


# ---------------------------------------------------------------------------
def scan(interface: str = "wlan0") -> List[dict]:
    """Return a list of dicts ``{ssid, signal, security, in_use}``."""
    if not _nmcli_available():
        return []
    _run(["nmcli", "-t", "device", "wifi", "rescan", "ifname", interface])
    rc, out, _ = _run(
        ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
         "device", "wifi", "list", "ifname", interface])
    if rc != 0:
        return []
    networks: List[dict] = []
    seen = set()
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue
        in_use = parts[0] == "*"
        ssid   = parts[1]
        try:
            signal = int(parts[2]) if parts[2] else 0
        except ValueError:
            signal = 0
        security = parts[3] or "Open"
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        networks.append({
            "ssid": ssid, "signal": signal,
            "security": security, "in_use": in_use,
        })
    networks.sort(key=lambda n: n["signal"], reverse=True)
    return networks


def current(interface: str = "wlan0") -> Optional[dict]:
    if not _nmcli_available():
        return None
    rc, out, _ = _run(
        ["nmcli", "-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,"
         "IP4.ADDRESS,IP4.GATEWAY", "device", "show", interface])
    if rc != 0:
        return None
    info = {"interface": interface, "state": "", "ssid": "",
            "ip": "", "gateway": ""}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        if k.endswith("GENERAL.STATE"):
            info["state"] = v
        elif k.endswith("GENERAL.CONNECTION"):
            info["ssid"] = v
        elif k.endswith("IP4.ADDRESS[1]"):
            info["ip"] = v.split("/")[0]
        elif k.endswith("IP4.GATEWAY"):
            info["gateway"] = v
    return info


def connect(ssid: str, password: Optional[str] = None,
            interface: str = "wlan0") -> Tuple[bool, str]:
    if not _nmcli_available():
        return False, "nmcli not available"
    args = ["nmcli", "device", "wifi", "connect", ssid, "ifname", interface]
    if password:
        args += ["password", password]
    rc, out, err = _run(args, timeout=30)
    if rc == 0:
        return True, (out or "connected").strip()
    return False, (err or out or "connect failed").strip()


def forget(ssid: str) -> Tuple[bool, str]:
    if not _nmcli_available():
        return False, "nmcli not available"
    rc, out, err = _run(["nmcli", "connection", "delete", ssid])
    if rc == 0:
        return True, "forgotten"
    return False, (err or out or "not found").strip()


def saved() -> List[dict]:
    if not _nmcli_available():
        return []
    rc, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    if rc != 0:
        return []
    out_list = []
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "802-11-wireless":
            out_list.append({"ssid": parts[0]})
    return out_list
