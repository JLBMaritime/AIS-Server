"""Flask application factory + Socket.IO setup.

A single factory returns ``(flask_app, socketio)``.  ``__main__`` imports
this, injects the live Pipeline / Forwarder / EventBus, and calls
``socketio.run()``.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict

from flask import Flask
from flask_login import LoginManager
from flask_socketio import SocketIO

from ..db import Database
from ..events import EventBus
from ..forwarder import ForwarderManager
from ..pipeline import Pipeline

log = logging.getLogger(__name__)


def create_app(cfg: Dict[str, Any], db: Database, pipeline: Pipeline,
               forwarder: ForwarderManager, events: EventBus
               ) -> tuple[Flask, SocketIO]:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config.update(
        SECRET_KEY=cfg["web"]["secret_key"],
        SESSION_COOKIE_SECURE=bool(cfg["web"].get("tls_cert")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(
            minutes=int(cfg["security"]["session_minutes"])),
        # Share live objects via app.config so blueprints can use current_app.
        CFG=cfg, DB=db, PIPELINE=pipeline,
        FORWARDER=forwarder, EVENTS=events,
        APP_NAME="JLBMaritime AIS-Server",
        PRIMARY_COLOR="#1c2346",
        SECONDARY_COLOR="#137dc5",
        BACKGROUND_COLOR="#252a34",
    )

    # ------------------------------------------------------------------
    login_manager = LoginManager(app)
    login_manager.login_view = "auth.login"

    @login_manager.user_loader
    def _load_user(uid: str):
        try:
            return db.get_user_by_id(int(uid))
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    from .auth import bp as auth_bp
    from .views import bp as views_bp
    from .api import bp as api_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    # ------------------------------------------------------------------
    @app.context_processor
    def _inject_globals():
        return {
            "app_name": app.config["APP_NAME"],
            "primary_color": app.config["PRIMARY_COLOR"],
            "secondary_color": app.config["SECONDARY_COLOR"],
            "background_color": app.config["BACKGROUND_COLOR"],
        }

    # ------------------------------------------------------------------
    # Python 3.13 currently breaks eventlet's threading monkey-patch, so we
    # use the plain "threading" async mode.  WebSocket transport still works
    # thanks to simple-websocket; long-polling is the fallback.
    socketio = SocketIO(app, cors_allowed_origins="*",
                        async_mode="threading", ping_interval=15,
                        ping_timeout=30)

    from .sockets import register_sockets
    register_sockets(socketio, events)

    return app, socketio
