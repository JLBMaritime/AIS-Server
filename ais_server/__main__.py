"""Entrypoint: ``python -m ais_server``.

Wires everything together and runs forever:

1. Load config, set up logging.
2. Open SQLite, seed default user if needed.
3. Build ForwarderManager and load endpoints from DB.
4. Build Pipeline (dedup / reorder / node-registry).
5. Start TCP ingest listener + output loop + endpoint-sync loop, each under
   SupervisedThread so they restart automatically on any exception.
6. Start the Flask + Socket.IO web app (blocking, runs in main thread in
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

    t_ingest   = SupervisedThread("ingest", ingest.serve_forever)
    t_output   = SupervisedThread("output", pipeline.output_loop)
    t_sync     = SupervisedThread("endpoint-sync",
                                  lambda: _endpoint_sync_loop(db, forwarder))
    t_netcheck = SupervisedThread("netcheck", netcheck.run)
    watchdog = Watchdog(interval=10.0)

    t_ingest.start()
    t_output.start()
    t_sync.start()
    t_netcheck.start()
    watchdog.start()

    # --- web app (runs in main thread) --------------------------------
    from .web.app import create_app  # local import to avoid circulars
    flask_app, socketio = create_app(cfg, db, pipeline, forwarder, events)

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
        watchdog.stop()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
