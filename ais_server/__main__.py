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
import logging.handlers
import os
import signal
import sys
import threading
import time
from pathlib import Path

from .config import load_config
from .db import Database
from .events import EventBus
from .forwarder import ForwarderManager
from .ingest import TcpIngest
from .pipeline import Pipeline
from .supervisor import SupervisedThread, Watchdog

log = logging.getLogger(__name__)

_STOP = threading.Event()


def _setup_logging(cfg: dict) -> None:
    level = getattr(logging, str(cfg["logging"]["level"]).upper(), logging.INFO)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    root = logging.getLogger()
    root.setLevel(level)
    # Clear default handlers (systemd captures stdout already).
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter(fmt))
    root.addHandler(stream)

    log_path = Path(cfg["logging"]["path"]) / "ais-server.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter(fmt))
        root.addHandler(fh)
    except PermissionError:
        log.warning("Cannot write %s – file logging disabled", log_path)


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
    t_ingest = SupervisedThread("ingest", ingest.serve_forever)
    t_output = SupervisedThread("output", pipeline.output_loop)
    t_sync   = SupervisedThread("endpoint-sync",
                                lambda: _endpoint_sync_loop(db, forwarder))
    watchdog = Watchdog(interval=10.0)

    t_ingest.start()
    t_output.start()
    t_sync.start()
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
        watchdog.stop()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
