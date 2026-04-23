"""`aisctl` command-line tool.

Talks to the locally running ais-server over ``http://127.0.0.1:<web.port>``
using the same JSON API as the web UI.  All destructive actions require a
successful login – credentials are cached in ``~/.aisctl/session`` (chmod 600).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import requests
import typer
import yaml
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False,
                  help="Control a locally-running AIS-Server.")
console = Console()

DEFAULT_CONFIG_PATHS = ["/etc/ais-server/config.yaml",
                        str(Path.home() / ".config/ais-server.yaml")]
SESSION_PATH = Path.home() / ".aisctl" / "session"


def _base_url() -> str:
    for p in DEFAULT_CONFIG_PATHS:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    cfg = yaml.safe_load(fh) or {}
                w = cfg.get("web", {})
                port = int(w.get("port", 80))
                host = w.get("host") or "127.0.0.1"
                if host in ("0.0.0.0", "::"):
                    host = "127.0.0.1"
                return f"http://{host}:{port}"
            except Exception:  # noqa: BLE001
                pass
    return "http://127.0.0.1:80"


def _session() -> requests.Session:
    s = requests.Session()
    if SESSION_PATH.exists():
        try:
            data = json.loads(SESSION_PATH.read_text())
            for k, v in data.get("cookies", {}).items():
                s.cookies.set(k, v)
        except Exception:  # noqa: BLE001
            pass
    return s


def _save_session(s: requests.Session) -> None:
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(json.dumps(
        {"cookies": {c.name: c.value for c in s.cookies}}))
    try:
        os.chmod(SESSION_PATH, 0o600)
    except OSError:
        pass


def _get(path: str) -> dict:
    s = _session()
    r = s.get(_base_url() + path, timeout=10)
    if r.status_code == 401:
        console.print("[red]Not logged in – run `aisctl login` first[/red]")
        raise typer.Exit(1)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: Optional[dict] = None) -> dict:
    s = _session()
    r = s.post(_base_url() + path, json=body or {}, timeout=15)
    if r.status_code == 401:
        console.print("[red]Not logged in – run `aisctl login` first[/red]")
        raise typer.Exit(1)
    r.raise_for_status()
    return r.json() if r.headers.get("content-type", "").startswith(
        "application/json") else {}


def _patch(path: str, body: dict) -> dict:
    s = _session()
    r = s.patch(_base_url() + path, json=body, timeout=10)
    r.raise_for_status()
    return r.json()


def _delete(path: str) -> dict:
    s = _session()
    r = s.delete(_base_url() + path, timeout=10)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
@app.command()
def login(username: str = typer.Option(..., prompt=True),
          password: str = typer.Option(..., prompt=True, hide_input=True)):
    """Log in and cache the session cookie."""
    s = requests.Session()
    r = s.post(_base_url() + "/login",
               data={"username": username, "password": password},
               allow_redirects=False, timeout=10)
    if r.status_code in (301, 302, 303) and "login" not in (
            r.headers.get("Location", "") or ""):
        _save_session(s)
        console.print("[green]Logged in[/green]")
    else:
        console.print("[red]Login failed[/red]")
        raise typer.Exit(1)


@app.command()
def logout():
    """Forget the cached session."""
    if SESSION_PATH.exists():
        SESSION_PATH.unlink()
    console.print("Logged out.")


@app.command()
def status():
    """Show server status."""
    s = _get("/api/status")
    p = s["pipeline"]
    console.print(f"[bold]Uptime[/bold]:      {p['uptime_seconds']}s")
    console.print(f"[bold]msgs/sec[/bold]:    {p['msgs_per_sec']}")
    console.print(f"[bold]Unique MMSI[/bold]: {p['unique_mmsi']}")
    console.print(f"[bold]Dedup rate[/bold]:  {p['dedup']['dedup_rate']*100:.1f}%")
    console.print(f"[bold]Queue[/bold]:       {p['reorder']['queue_size']}")

    t = Table("Node", "State", "Msgs", "Invalid", "Last seen")
    for n in s["nodes"]:
        t.add_row(n["peer"], "on" if n["connected"] else "off",
                  str(n["messages"]), str(n["invalid"]),
                  f"{int(n['last_seen'])}")
    console.print(t)

    t = Table("Endpoint", "Target", "Status", "Sent", "Queue")
    for e in s["endpoints"]:
        t.add_row(e["name"], f"{e['host']}:{e['port']}",
                  "up" if e["connected"] else "down",
                  str(e["sent"]), str(e["queue_depth"]))
    console.print(t)


endpoints_app = typer.Typer(help="Manage forwarder endpoints.")
app.add_typer(endpoints_app, name="endpoints")


@endpoints_app.command("list")
def endpoints_list():
    data = _get("/api/endpoints")
    t = Table("ID", "Name", "Proto", "Host", "Port", "Enabled")
    for e in data:
        t.add_row(str(e["id"]), e["name"], e["protocol"],
                  e["host"], str(e["port"]), "yes" if e["enabled"] else "no")
    console.print(t)


@endpoints_app.command("add")
def endpoints_add(name: str, host: str, port: int,
                  protocol: str = "tcp",
                  enabled: bool = True):
    r = _post("/api/endpoints", {"name": name, "host": host, "port": port,
                                 "protocol": protocol, "enabled": enabled})
    console.print(r)


@endpoints_app.command("delete")
def endpoints_delete(ep_id: int):
    r = _delete(f"/api/endpoints/{ep_id}")
    console.print(r)


@endpoints_app.command("test")
def endpoints_test(ep_id: int):
    r = _post(f"/api/endpoints/{ep_id}/test")
    console.print(r)


@endpoints_app.command("enable")
def endpoints_enable(ep_id: int):
    console.print(_patch(f"/api/endpoints/{ep_id}", {"enabled": True}))


@endpoints_app.command("disable")
def endpoints_disable(ep_id: int):
    console.print(_patch(f"/api/endpoints/{ep_id}", {"enabled": False}))


# ---------------------------------------------------------------------------
wifi_app = typer.Typer(help="Manage Wi-Fi connection.")
app.add_typer(wifi_app, name="wifi")


@wifi_app.command("scan")
def wifi_scan_cmd():
    nets = _get("/api/wifi/scan")
    t = Table("SSID", "Signal", "Security")
    for n in nets:
        t.add_row(n["ssid"], f"{n['signal']}%", n["security"])
    console.print(t)


@wifi_app.command("connect")
def wifi_connect_cmd(ssid: str,
                     password: str = typer.Option("", prompt=True,
                                                  hide_input=True,
                                                  show_default=False)):
    r = _post("/api/wifi/connect", {"ssid": ssid, "password": password})
    console.print(r)


@wifi_app.command("current")
def wifi_current_cmd():
    console.print(_get("/api/wifi/current"))


# ---------------------------------------------------------------------------
@app.command()
def restart():
    """Restart the ais-server systemd service."""
    console.print(_post("/api/system/restart"))


@app.command()
def reboot():
    """Reboot the Raspberry Pi."""
    if not typer.confirm("Reboot the Pi now?"):
        raise typer.Exit()
    console.print(_post("/api/system/reboot"))


@app.command()
def backup(output: Path = typer.Argument("ais-backup.tar.gz")):
    """Download a backup archive."""
    s = _session()
    r = s.get(_base_url() + "/api/system/backup", timeout=30, stream=True)
    r.raise_for_status()
    with output.open("wb") as fh:
        for chunk in r.iter_content(chunk_size=8192):
            fh.write(chunk)
    console.print(f"[green]Saved to {output}[/green]")


if __name__ == "__main__":
    app()
