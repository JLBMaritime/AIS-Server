"""JSON API consumed by the front-end and by ``aisctl``."""
from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import time
from pathlib import Path

from flask import (Blueprint, current_app, jsonify, request, send_file)
from flask_login import current_user, login_required

from .. import wifi as wifi_mod
from ..forwarder import ForwarderManager

bp = Blueprint("api", __name__)


# ---------------------------------------------------------------------------
# Status / stats
# ---------------------------------------------------------------------------
@bp.get("/status")
@login_required
def status():
    pipeline = current_app.config["PIPELINE"]
    forwarder = current_app.config["FORWARDER"]
    return jsonify({
        "pipeline": pipeline.stats(),
        "nodes":    pipeline.nodes.snapshot(),
        "endpoints": forwarder.stats(),
    })


@bp.get("/healthz")
def healthz():
    # Unauthenticated: minimal liveness + readiness info.
    pipeline = current_app.config["PIPELINE"]
    return jsonify({"ok": True, "uptime": pipeline.stats()["uptime_seconds"]})


# ---------------------------------------------------------------------------
# Endpoints CRUD
# ---------------------------------------------------------------------------
@bp.get("/endpoints")
@login_required
def endpoints_list():
    db = current_app.config["DB"]
    return jsonify(db.as_dict_endpoints())


@bp.post("/endpoints")
@login_required
def endpoints_add():
    db = current_app.config["DB"]
    data = request.get_json(silent=True) or {}
    try:
        ep_id = db.add_endpoint(
            name=data["name"].strip(),
            host=data["host"].strip(),
            port=int(data["port"]),
            protocol=(data.get("protocol") or "tcp").lower(),
            path=data.get("path"),
            enabled=bool(data.get("enabled", True)),
        )
    except KeyError as k:
        return jsonify({"ok": False, "error": f"missing field: {k}"}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "id": ep_id})


@bp.patch("/endpoints/<int:ep_id>")
@login_required
def endpoints_update(ep_id: int):
    db = current_app.config["DB"]
    data = request.get_json(silent=True) or {}
    if "port" in data:
        try:
            data["port"] = int(data["port"])
        except ValueError:
            return jsonify({"ok": False, "error": "bad port"}), 400
    ok = db.update_endpoint(ep_id, **data)
    return jsonify({"ok": ok})


@bp.delete("/endpoints/<int:ep_id>")
@login_required
def endpoints_delete(ep_id: int):
    db = current_app.config["DB"]
    ok = db.delete_endpoint(ep_id)
    return jsonify({"ok": ok})


@bp.post("/endpoints/<int:ep_id>/test")
@login_required
def endpoints_test(ep_id: int):
    db = current_app.config["DB"]
    ep = db.get_endpoint(ep_id)
    if not ep:
        return jsonify({"ok": False, "error": "not found"}), 404
    ok, msg = ForwarderManager.test_endpoint(ep)
    return jsonify({"ok": ok, "message": msg})


# ---------------------------------------------------------------------------
# Wi-Fi
# ---------------------------------------------------------------------------
@bp.get("/wifi/scan")
@login_required
def wifi_scan():
    return jsonify(wifi_mod.scan())


@bp.get("/wifi/current")
@login_required
def wifi_current():
    return jsonify(wifi_mod.current() or {})


@bp.get("/wifi/saved")
@login_required
def wifi_saved():
    return jsonify(wifi_mod.saved())


@bp.post("/wifi/connect")
@login_required
def wifi_connect():
    data = request.get_json(silent=True) or {}
    ssid = (data.get("ssid") or "").strip()
    password = data.get("password")
    if not ssid:
        return jsonify({"ok": False, "error": "ssid required"}), 400
    ok, msg = wifi_mod.connect(ssid, password)
    return jsonify({"ok": ok, "message": msg})


@bp.post("/wifi/forget")
@login_required
def wifi_forget():
    data = request.get_json(silent=True) or {}
    ssid = (data.get("ssid") or "").strip()
    if not ssid:
        return jsonify({"ok": False, "error": "ssid required"}), 400
    ok, msg = wifi_mod.forget(ssid)
    return jsonify({"ok": ok, "message": msg})


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
@bp.post("/system/change-password")
@login_required
def change_password():
    db = current_app.config["DB"]
    data = request.get_json(silent=True) or {}
    cur = data.get("current_password") or ""
    new = data.get("new_password") or ""
    if not db.verify_password(current_user.username, cur):
        return jsonify({"ok": False, "error": "wrong current password"}), 400
    if len(new) < 8:
        return jsonify({"ok": False, "error": "min 8 chars"}), 400
    db.set_password(current_user.username, new, clear_must_change=True)
    return jsonify({"ok": True})


@bp.get("/system/backup")
@login_required
def backup():
    cfg = current_app.config["CFG"]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in (cfg["paths"]["db"], "/etc/ais-server/config.yaml"):
            if Path(p).exists():
                tar.add(p, arcname=Path(p).name)
    buf.seek(0)
    fname = f"ais-server-backup-{int(time.time())}.tar.gz"
    return send_file(buf, mimetype="application/gzip",
                     as_attachment=True, download_name=fname)


@bp.post("/system/restart")
@login_required
def system_restart():
    # Use subprocess so we don't kill ourselves before the response is sent.
    subprocess.Popen(
        ["sh", "-c", "sleep 1 && systemctl restart ais-server"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return jsonify({"ok": True, "message": "restarting"})


@bp.post("/system/reboot")
@login_required
def system_reboot():
    subprocess.Popen(
        ["sh", "-c", "sleep 2 && shutdown -r now"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return jsonify({"ok": True, "message": "rebooting"})


# ---------------------------------------------------------------------------
# Recent buffers (used when a page first loads, before Socket.IO kicks in).
# ---------------------------------------------------------------------------
@bp.get("/recent/incoming")
@login_required
def recent_incoming():
    events = current_app.config["EVENTS"]
    n = min(int(request.args.get("n", 100)), 500)
    return jsonify(events.recent("incoming", n))


@bp.get("/recent/outgoing")
@login_required
def recent_outgoing():
    events = current_app.config["EVENTS"]
    n = min(int(request.args.get("n", 100)), 500)
    return jsonify(events.recent("outgoing", n))
