"""TCP ingest – accepts NMEA streams from multiple AIS nodes.

Model
-----
* One listening socket bound to ``ingest.bind:ingest.tcp_port``.
* One worker thread per connected TCP session (max ``ingest.max_clients``).
* Each worker reads CR/LF delimited lines, normalises them, and hands every
  valid AIS sentence to the ``pipeline`` callback.
* ``NodeRegistry`` aggregates stats **per logical node**.  A logical node is
  keyed by:

    1. the ``s:<id>`` field in a leading ``\\…\\`` tag-block (preferred –
       stable across IP changes / Tailscale roaming), OR
    2. the peer **host IP** (so reconnects from the same node don't spam
       the Nodes page with duplicate rows).

* TCP keepalive is enabled on every accepted socket so dead peers are
  evicted in ~2 minutes even if they never sent a FIN.

All exceptions are contained per-connection so one misbehaving node cannot
take the listener down.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from .nmea import parse

log = logging.getLogger(__name__)

# TCP keepalive tunables — 60 s idle before first probe, then 5 probes
# every 20 s (≈ 160 s total to evict a dead peer).
_KEEPIDLE = 60
_KEEPINTVL = 20
_KEEPCNT = 5


@dataclass
class NodeInfo:
    """One *logical* node – may aggregate multiple TCP sessions."""
    key: str                       # primary key: source_id or host
    host: str                      # last-known peer IP
    source_id: Optional[str]       # tag-block s:… if any
    first_seen: float
    last_seen: float = 0.0
    connected_at: float = 0.0
    messages: int = 0
    invalid: int = 0
    bytes_rx: int = 0
    sessions: int = 0              # total sessions since start
    active_sessions: int = 0       # currently open
    last_peer: str = ""            # "ip:port" of most-recent session
    # Multi-part AIS reassembly buffer, keyed by message_id.  Lives on the
    # logical node so fragments crossing a quick reconnect still match.
    multipart: Dict[str, str] = field(default_factory=dict)

    @property
    def connected(self) -> bool:
        return self.active_sessions > 0


class NodeRegistry:
    """Thread-safe registry of nodes keyed by source-id or host IP."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nodes: Dict[str, NodeInfo] = {}

    # ------------------------------------------------------------------
    def on_session_start(self, peer: str, host: str,
                         source_id: Optional[str] = None) -> NodeInfo:
        key = source_id or host
        now = time.time()
        with self._lock:
            info = self._nodes.get(key)
            if info is None:
                info = NodeInfo(
                    key=key, host=host, source_id=source_id,
                    first_seen=now, last_seen=now, connected_at=now,
                )
                self._nodes[key] = info
            info.last_peer = peer
            info.host = host
            info.sessions += 1
            info.active_sessions += 1
            info.connected_at = now
            info.last_seen = now
            return info

    def on_session_end(self, key: str) -> None:
        with self._lock:
            info = self._nodes.get(key)
            if info and info.active_sessions > 0:
                info.active_sessions -= 1

    def set_source_id(self, old_key: str, source_id: str) -> NodeInfo:
        """Upgrade a host-keyed node to a source-id-keyed node when the
        first tag-block arrives mid-session.  Preserves counters."""
        with self._lock:
            if old_key == source_id or source_id in self._nodes:
                # Already using source_id key (or one exists) – just return it.
                return self._nodes.get(source_id) or self._nodes[old_key]
            info = self._nodes.pop(old_key, None)
            if info is None:
                return self._nodes.setdefault(
                    source_id,
                    NodeInfo(key=source_id, host="", source_id=source_id,
                             first_seen=time.time(), last_seen=time.time()))
            info.key = source_id
            info.source_id = source_id
            self._nodes[source_id] = info
            return info

    def touch(self, key: str, nbytes: int, msg: bool,
              invalid: bool = False) -> None:
        with self._lock:
            info = self._nodes.get(key)
            if not info:
                return
            info.last_seen = time.time()
            info.bytes_rx += nbytes
            if msg:
                info.messages += 1
            if invalid:
                info.invalid += 1

    def snapshot(self) -> list[dict]:
        with self._lock:
            out = []
            for n in self._nodes.values():
                out.append({
                    # Back-compat: "peer" is what the old UI expected; now it
                    # shows the logical key (source-id or IP).
                    "peer": n.key,
                    "host": n.host,
                    "source_id": n.source_id,
                    "last_peer": n.last_peer,
                    "first_seen": n.first_seen,
                    "connected_at": n.connected_at,
                    "last_seen": n.last_seen,
                    "messages": n.messages,
                    "invalid": n.invalid,
                    "bytes_rx": n.bytes_rx,
                    "sessions": n.sessions,
                    "active_sessions": n.active_sessions,
                    "connected": n.connected,
                })
        return sorted(out, key=lambda x: (not x["connected"], -x["last_seen"]))


# ---------------------------------------------------------------------------
# Tag-block helper
# ---------------------------------------------------------------------------
def _strip_tag_block(line: str) -> Tuple[str, Optional[str]]:
    """Return (sentence_without_tagblock, source_id_or_None).

    NMEA 4.10 tag blocks look like:  ``\\s:GIB-01,c:1714063200*5C\\!AIVDM,...``
    """
    if not line.startswith("\\"):
        return line, None
    end = line.find("\\", 1)
    if end < 0:
        return line, None
    block = line[1:end]
    body = line[end + 1:]
    # Strip the "*HH" checksum on the block itself, if present.
    if "*" in block:
        block = block.rsplit("*", 1)[0]
    source_id: Optional[str] = None
    for field in block.split(","):
        if field.startswith("s:"):
            source_id = field[2:].strip() or None
            break
    return body, source_id


# ---------------------------------------------------------------------------
# Sentence handler – one per TCP session
# ---------------------------------------------------------------------------
class _ConnectionHandler(threading.Thread):
    def __init__(self, sock: socket.socket, peer: tuple,
                 registry: NodeRegistry,
                 on_sentence: Callable[[str, str, float], None],
                 idle_timeout: int) -> None:
        super().__init__(name=f"node-{peer[0]}:{peer[1]}", daemon=True)
        self.sock = sock
        self.peer = f"{peer[0]}:{peer[1]}"
        self.host = peer[0]
        self.registry = registry
        self.on_sentence = on_sentence
        self.idle_timeout = idle_timeout

    def run(self) -> None:
        # Start with host-keyed identity; upgrade to source-id if tag-blocks
        # arrive on this session.
        key = self.host
        info = self.registry.on_session_start(self.peer, self.host)
        log.info("Node session opened: peer=%s key=%s (sessions=%d)",
                 self.peer, info.key, info.sessions)
        buf = b""
        try:
            self.sock.settimeout(self.idle_timeout)
            while True:
                try:
                    data = self.sock.recv(4096)
                except socket.timeout:
                    log.warning("Node %s idle > %ds – closing", self.peer,
                                self.idle_timeout)
                    break
                if not data:
                    break
                buf += data
                self.registry.touch(key, len(data), msg=False)
                # Split on \n – tolerate CR/LF and lone CR.
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line_bytes, buf = buf[:nl], buf[nl + 1:]
                    key = self._handle_line(line_bytes, info, key)
        except OSError as exc:
            log.info("Node %s socket error: %s", self.peer, exc)
        except Exception:  # noqa: BLE001
            log.exception("Unhandled error in node handler %s", self.peer)
        finally:
            try:
                self.sock.close()
            except OSError:
                pass
            self.registry.on_session_end(key)
            log.info("Node session closed: peer=%s key=%s", self.peer, key)

    # ------------------------------------------------------------------
    def _handle_line(self, raw: bytes, info: NodeInfo, key: str) -> str:
        try:
            line = raw.decode("ascii", errors="ignore").strip("\r\n\t ")
        except Exception:  # noqa: BLE001
            self.registry.touch(key, 0, msg=False, invalid=True)
            return key
        if not line:
            return key

        sentence, source_id = _strip_tag_block(line)
        if source_id and source_id != info.source_id:
            # First time we've seen a source-id on this session – upgrade
            # the registry entry so stats survive future reconnects.
            info = self.registry.set_source_id(key, source_id)
            key = source_id

        parsed = parse(sentence)
        if parsed is None or not parsed.checksum_ok:
            self.registry.touch(key, 0, msg=False, invalid=True)
            return key

        ts = time.time()
        self.registry.touch(key, 0, msg=True)
        try:
            # Give the pipeline the logical key (source-id or host) as
            # "peer", so the live-data view groups by node rather than
            # ephemeral port.
            self.on_sentence(sentence, key, ts)
        except Exception:  # noqa: BLE001
            log.exception("on_sentence callback failed for %s", key)
        return key


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------
class TcpIngest:
    def __init__(self, bind: str, port: int, max_clients: int, idle_timeout: int,
                 registry: NodeRegistry,
                 on_sentence: Callable[[str, str, float], None]) -> None:
        self.bind = bind
        self.port = port
        self.max_clients = max_clients
        self.idle_timeout = idle_timeout
        self.registry = registry
        self.on_sentence = on_sentence
        self._server_sock: socket.socket | None = None
        self._active: list[_ConnectionHandler] = []
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def serve_forever(self) -> None:
        """Blocking serve loop (intended to be run by SupervisedThread)."""
        self._stop.clear()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.bind, self.port))
        s.listen(128)
        s.settimeout(1.0)
        self._server_sock = s
        log.info("Ingest listening on %s:%d", self.bind, self.port)
        try:
            while not self._stop.is_set():
                try:
                    client, peer = s.accept()
                except socket.timeout:
                    self._reap()
                    continue
                if self._count_active() >= self.max_clients:
                    log.warning("Rejecting %s – max_clients reached", peer)
                    try:
                        client.close()
                    except OSError:
                        pass
                    continue
                self._configure_client_socket(client)
                handler = _ConnectionHandler(
                    client, peer, self.registry, self.on_sentence,
                    self.idle_timeout,
                )
                handler.start()
                self._active.append(handler)
                self._reap()
        finally:
            try:
                s.close()
            except OSError:
                pass
            self._server_sock = None

    def stop(self) -> None:
        self._stop.set()
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    @staticmethod
    def _configure_client_socket(sock: socket.socket) -> None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # Linux-specific keepalive tuning (harmless no-op elsewhere).
        for name, val in (
            ("TCP_KEEPIDLE",  _KEEPIDLE),
            ("TCP_KEEPINTVL", _KEEPINTVL),
            ("TCP_KEEPCNT",   _KEEPCNT),
        ):
            opt = getattr(socket, name, None)
            if opt is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, val)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    def _reap(self) -> None:
        self._active = [h for h in self._active if h.is_alive()]

    def _count_active(self) -> int:
        self._reap()
        return len(self._active)
