"""Entrypoint: ``python -m ais_server``.

Wires everything together and runs forever:

1. Load config, set up logging.
2. Open SQLite, seed default user if needed.
3. Build ForwarderManager and load endpoints from DB.
4. Build Pipeline (dedup / reorder / node-registry).
5. Start TCP ingest listener + output loop + endpoint-sync loop, each under
   SupervisedThread so they restart automatically on any exception.
6. Start the auxiliary housekeeping threads: ``netcheck`` (Wi-Fi self-heal),
   ``diskcheck`` (low-disk warning), and ``maintenance`` (DB WAL checkpoint /
   VACUUM / inactive-node prune).
7. Start the Flask + Socket.IO web app (blocking, runs in main thread in
   ``threading`` async mode).  The Watchdog thread pings systemd every 10 s.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from .config import load_config
from .db import Database
from .diskcheck import DiskCheck
from .events import EventBus
from .forwarder import ForwarderManager
from .ingest import TcpIngest
from .netcheck import NetCheck
from .pipeline import Pipeline
from .supervisor import SupervisedThread, Watchdog

log = logging.getLogger(__name__)

_STOP = threading.Event()


def _setup_logging(cfg: dict) -> None:
    """Stdout-only logging.

    systemd captures stdout into the journal; ``journalctl -u ais-server``
    is the single source of truth.  We deliberately do **not** attach a
    RotatingFileHandler here – running both Python rotation *and* systemd's
    ``StandardOutput=append:`` *and* a logrotate.d entry against the same
    file caused stale-FD writes (see commit history for the 24-hour-freeze
    investigation).
    """
    level = getattr(logging, str(cfg["logging"]["level"]).upper(), logging.INFO)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter(fmt))
    root.addHandler(stream)


def _install_signal_handlers() -> None:
    def _term(signum, _frame):
        log.info("Signal %s received – stopping", signum)
        _STOP.set()
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    # SIGHUP => config reload (handled by systemd ExecReload).
    try:
        signal.signal(signal.SIGHUP, _term)  # simplest: restart via systemd
    except AttributeError:
        pass  # Windows


def _endpoint_sync_loop(db: Database, forwarder: ForwarderManager) -> None:
    """Re-sync endpoint workers every 2 seconds so UI edits take effect fast."""
    while not _STOP.is_set():
        try:
            forwarder.sync(db.list_endpoints())
        except Exception:  # noqa: BLE001
            log.exception("Endpoint sync failed")
        time.sleep(2.0)


def _maintenance_loop(cfg: dict, db: Database, pipeline: Pipeline) -> None:
    """Background hygiene: WAL checkpoint, VACUUM, inactive-node prune.

    All actions are best-effort and any failure is logged and swallowed so
    a transient SQLite hiccup never crashes the supervisor (which would
    restart this loop anyway, but spammy crash logs help no-one).
    """
    m = cfg.get("maintenance", {}) or {}
    ckpt_iv  = float(m.get("wal_checkpoint_interval", 3600))
    vac_iv   = float(m.get("vacuum_interval", 7 * 24 * 3600))
    prune_iv = float(m.get("node_prune_interval", 3600))
    node_max = float(m.get("node_max_age", 30 * 24 * 3600))

    # Sleep "interval" between ticks, but never less than 30 s, and use the
    # shortest of the three so each task can fire on its own schedule.
    tick = max(30.0, min(t for t in (ckpt_iv, vac_iv, prune_iv) if t > 0)
               if any(t > 0 for t in (ckpt_iv, vac_iv, prune_iv)) else 3600)

    last_ckpt = 0.0
    last_vac  = 0.0
    last_prune = 0.0
    log.info("maintenance: starting (ckpt=%.0fs vacuum=%.0fs prune=%.0fs)",
             ckpt_iv, vac_iv, prune_iv)
    while not _STOP.is_set():
        now = time.time()
        if ckpt_iv > 0 and (now - last_ckpt) >= ckpt_iv:
            try:
                stats = db.checkpoint("TRUNCATE")
                log.info("maintenance: WAL checkpoint(TRUNCATE) busy=%d log=%d "
                         "ckpt=%d", stats["busy"], stats["log"],
                         stats["checkpointed"])
            except Exception:  # noqa: BLE001
                log.exception("maintenance: WAL checkpoint failed")
            last_ckpt = now
        if vac_iv > 0 and (now - last_vac) >= vac_iv:
            try:
                db.vacuum()
                log.info("maintenance: VACUUM complete (db=%d bytes)",
                         db.size_info().get("db", 0))
            except Exception:  # noqa: BLE001
                log.exception("maintenance: VACUUM failed")
            last_vac = now
        if prune_iv > 0 and (now - last_prune) >= prune_iv and node_max > 0:
            try:
                pipeline.nodes.prune_inactive(node_max)
            except Exception:  # noqa: BLE001
                log.exception("maintenance: prune_inactive failed")
            last_prune = now
        # Use the stop-event so SIGTERM unblocks us promptly.
        if _STOP.wait(tick):
            return


def main() -> int:
    cfg = load_config()
    _setup_logging(cfg)
    _install_signal_handlers()
    log.info("AIS-Server starting – config=%s",
             os.environ.get("AIS_SERVER_CONFIG", "/etc/ais-server/config.yaml"))

    # --- persistence ---------------------------------------------------
    db = Database(cfg["paths"]["db"], bcrypt_rounds=cfg["security"]["bcrypt_rounds"])
    db.seed_default_user()

    # --- pipeline ------------------------------------------------------
    events = EventBus()
    forwarder = ForwarderManager(queue_size=int(cfg["forwarder"]["queue_size"]),
                                 events=events)
    pipeline = Pipeline(cfg, events, forwarder)

    # Initial endpoint boot-up.
    forwarder.sync(db.list_endpoints())

    # --- ingest --------------------------------------------------------
    ingest = TcpIngest(
        bind=cfg["ingest"]["bind"],
        port=int(cfg["ingest"]["tcp_port"]),
        max_clients=int(cfg["ingest"]["max_clients"]),
        idle_timeout=int(cfg["ingest"]["idle_timeout"]),
        registry=pipeline.nodes,
        on_sentence=pipeline.on_sentence,
    )

    # --- supervised threads -------------------------------------------
    nc_cfg = cfg.get("netcheck", {}) or {}
    netcheck = NetCheck(
        interval=float(nc_cfg.get("interval", 30)),
        fail_threshold=int(nc_cfg.get("fail_threshold", 3)),
        timeout=float(nc_cfg.get("timeout", 4)),
        host=nc_cfg.get("host") or None,
        port=int(nc_cfg.get("port", 53)),
        interface=nc_cfg.get("interface", "wlan0"),
        auto_recover=bool(nc_cfg.get("auto_recover", True)),
    )

    dc_cfg = cfg.get("diskcheck", {}) or {}
    diskcheck = DiskCheck(
        data_path=str(dc_cfg.get("data_path") or "/var"),
        db_path=cfg["paths"]["db"],
        interval=float(dc_cfg.get("interval", 60)),
        low_water_mb=int(dc_cfg.get("low_water_mb", 500)),
        warn_water_mb=int(dc_cfg.get("warn_water_mb", 1500)),
    )

    t_ingest   = SupervisedThread("ingest", ingest.serve_forever)
    t_output   = SupervisedThread("output", pipeline.output_loop)
    t_sync     = SupervisedThread("endpoint-sync",
                                  lambda: _endpoint_sync_loop(db, forwarder))
    t_netcheck = SupervisedThread("netcheck", netcheck.run)
    t_disk     = SupervisedThread("diskcheck", diskcheck.run)
    t_maint    = SupervisedThread("maintenance",
                                  lambda: _maintenance_loop(cfg, db, pipeline))
    watchdog = Watchdog(interval=10.0)

    t_ingest.start()
    t_output.start()
    t_sync.start()
    t_netcheck.start()
    t_disk.start()
    t_maint.start()
    watchdog.start()

    # --- web app (runs in main thread) --------------------------------
    from .web.app import create_app  # local import to avoid circulars
    flask_app, socketio = create_app(cfg, db, pipeline, forwarder, events,
                                     diskcheck=diskcheck)

    host = cfg["web"]["host"]
    port = int(cfg["web"]["port"])
    log.info("Web UI listening on %s:%d", host, port)

    # Run until STOP.  socketio.run blocks; stop by sending SIGTERM.
    try:
        socketio.run(flask_app, host=host, port=port,
                     allow_unsafe_werkzeug=True, use_reloader=False,
                     log_output=False)
    except Exception:  # noqa: BLE001
        log.exception("Web app crashed – exiting")
        return 1
    finally:
        log.info("Shutting down…")
        _STOP.set()
        ingest.stop()
        forwarder.stop_all()
        netcheck.stop()
        diskcheck.stop()
        watchdog.stop()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
