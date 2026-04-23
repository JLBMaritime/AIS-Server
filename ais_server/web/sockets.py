"""Socket.IO live feeds (incoming data / outgoing data / status ticks)."""
from __future__ import annotations

import logging
import threading

from flask_socketio import SocketIO

from ..events import EventBus

log = logging.getLogger(__name__)


def register_sockets(sio: SocketIO, events: EventBus) -> None:
    # ------------------------------------------------------------------
    # Bridge pipeline events -> Socket.IO rooms.
    # Subscribers run in the pipeline threads – we dispatch to Socket.IO's
    # own thread-safe emit, which uses the background eventlet hub.
    # ------------------------------------------------------------------
    def _on_incoming(payload):
        try:
            sio.emit("incoming", payload, namespace="/live")
        except Exception:  # noqa: BLE001
            log.debug("socketio emit(incoming) failed", exc_info=True)

    def _on_outgoing(payload):
        try:
            sio.emit("outgoing", payload, namespace="/live")
        except Exception:  # noqa: BLE001
            log.debug("socketio emit(outgoing) failed", exc_info=True)

    events.subscribe("incoming", _on_incoming)
    events.subscribe("outgoing", _on_outgoing)

    @sio.on("connect", namespace="/live")
    def _on_connect(auth=None):
        log.debug("socketio client connected")

    @sio.on("disconnect", namespace="/live")
    def _on_disconnect():
        log.debug("socketio client disconnected")
