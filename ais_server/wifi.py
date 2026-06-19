"""Wi-Fi configuration helpers using NetworkManager (``nmcli``).

Raspberry Pi OS Bookworm ships NetworkManager by default – ``nmcli`` is the
cleanest, most reliable way to scan / connect / forget networks and works on
headless systems.  If ``nmcli`` isn't installed we degrade gracefully so the
rest of the server still works.

The module also exposes an ``ethernet()`` helper – the Wi-Fi page in the web
UI shows whether ``eth0`` is plugged in / has an IP, even though we don't
let the user configure the wired side.  Bookworm + NetworkManager already
auto-configure ``eth0`` over DHCP and prefer it over Wi-Fi when both are up,
so there's nothing to set – this is purely a status read-out.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Ethernet
# ---------------------------------------------------------------------------
def _sysfs_read(path: str) -> str:
    """Read a single line from /sys; quiet about missing files / EIO."""
    try:
        return Path(path).read_text().strip()
    except (OSError, ValueError):
        return ""


def ethernet(interface: str = "eth0") -> Optional[dict]:
    """Return a dict describing the wired link, or ``None`` if we can't tell.

    Shape::

        {
          "interface":  "eth0",
          "available":  True/False,   # the kernel knows about the interface
          "link":       True/False,   # cable plugged in (carrier=1)
          "connected":  True/False,   # NetworkManager state is "connected"
          "state":      "connected" | "disconnected" | "unavailable" | "",
          "speed_mbps": 1000 | 100 | 10 | 0,
          "ip":         "192.168.1.42" | "",
          "gateway":    "192.168.1.1"  | "",
          "mac":        "dc:a6:32:..." | "",
          "connection": "Wired connection 1" | "",
        }

    All fields are best-effort.  Missing values are returned as falsy
    defaults so the UI can still render a sensible message ("cable
    unplugged", "Not available on this device", …).
    """
    info = {
        "interface": interface,
        "available": False,
        "link": False,
        "connected": False,
        "state": "",
        "speed_mbps": 0,
        "ip": "",
        "gateway": "",
        "mac": "",
        "connection": "",
    }

    # 1) sysfs – works whether or not nmcli is installed.  Tells us if the
    #    interface exists at all, and whether the cable is in (carrier=1).
    sys_root = Path(f"/sys/class/net/{interface}")
    if sys_root.is_dir():
        info["available"] = True
        info["mac"] = _sysfs_read(str(sys_root / "address"))
        info["link"] = _sysfs_read(str(sys_root / "carrier")) == "1"
        try:
            # "speed" returns -1 (or EINVAL) when there's no link; coerce
            # those to 0 so the UI just shows "—".
            spd = int(_sysfs_read(str(sys_root / "speed")) or "0")
            info["speed_mbps"] = max(spd, 0)
        except ValueError:
            info["speed_mbps"] = 0

    # 2) nmcli – gives us the NM state and an IP/gateway when one is bound.
    #    If nmcli isn't installed we still return the sysfs view above.
    if _nmcli_available():
        rc, out, _ = _run(
            ["nmcli", "-t", "-f",
             "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS,IP4.GATEWAY",
             "device", "show", interface])
        if rc == 0:
            for line in out.splitlines():
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                if k.endswith("GENERAL.STATE"):
                    # nmcli returns "100 (connected)" / "20 (unavailable)" /
                    # "30 (disconnected)" – strip to the word for the UI.
                    word = ""
                    if "(" in v and ")" in v:
                        word = v.split("(", 1)[1].rstrip(")").strip()
                    else:
                        word = v.strip()
                    info["state"] = word
                    info["connected"] = word == "connected"
                elif k.endswith("GENERAL.CONNECTION"):
                    info["connection"] = "" if v in ("", "--") else v
                elif k.endswith("IP4.ADDRESS[1]"):
                    info["ip"] = v.split("/")[0]
                elif k.endswith("IP4.GATEWAY"):
                    info["gateway"] = "" if v in ("", "--") else v
        elif not info["available"]:
            # nmcli couldn't see the device *and* sysfs has no record – the
            # box really doesn't have an `eth0`.
            return info

    return info
