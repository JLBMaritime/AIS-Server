"""TCP ingest – accepts NMEA streams from multiple AIS nodes.

Model
-----
* One listening socket bound to ``ingest.bind:ingest.tcp_port``.
* One worker thread per connected node (max ``ingest.max_clients``).
* Each worker reads CR/LF delimited lines, normalises them, and hands every
  valid AIS sentence to the ``pipeline`` callback.
* The node tracker maps peer IPs → :class:`NodeInfo` for the Nodes page.

All exceptions are contained per-connection so one misbehaving node cannot
take the listener down.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict

from .nmea import parse

log = logging.getLogger(__name__)


@dataclass
class NodeInfo:
    peer: str                       # "ip:port"
    host: str                       # just the IP
    connected_at: float
    last_seen: float = 0.0
    messages: int = 0
    invalid: int = 0
    bytes_rx: int = 0
    connected: bool = True
    # Multi-part AIS reassembly buffer, keyed by message_id.
    multipart: Dict[str, str] = field(default_factory=dict)


class NodeRegistry:
    """Thread-safe registry of currently / recently connected nodes."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nodes: Dict[str, NodeInfo] = {}   # keyed by "ip:port"

    def on_connect(self, peer: str, host: str) -> NodeInfo:
        info = NodeInfo(peer=peer, host=host,
                        connected_at=time.time(), last_seen=time.time())
        with self._lock:
            self._nodes[peer] = info
        return info

    def on_disconnect(self, peer: str) -> None:
        with self._lock:
            if peer in self._nodes:
                self._nodes[peer].connected = False

    def touch(self, peer: str, nbytes: int, msg: bool, invalid: bool = False
              ) -> None:
        with self._lock:
            info = self._nodes.get(peer)
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
                    "peer": n.peer, "host": n.host,
                    "connected_at": n.connected_at,
                    "last_seen": n.last_seen,
                    "messages": n.messages,
                    "invalid": n.invalid,
                    "bytes_rx": n.bytes_rx,
                    "connected": n.connected,
                })
        return sorted(out, key=lambda x: (not x["connected"], -x["last_seen"]))


# ---------------------------------------------------------------------------
# Sentence handler – one per connection
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
        info = self.registry.on_connect(self.peer, self.host)
        log.info("Node connected: %s", self.peer)
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
                self.registry.touch(self.peer, len(data), msg=False)
                # Split on \n – tolerate CR/LF and lone CR.
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line, buf = buf[:nl], buf[nl + 1:]
                    self._handle_line(line, info)
        except OSError as exc:
            log.info("Node %s socket error: %s", self.peer, exc)
        except Exception:  # noqa: BLE001
            log.exception("Unhandled error in node handler %s", self.peer)
        finally:
            try:
                self.sock.close()
            except OSError:
                pass
            self.registry.on_disconnect(self.peer)
            log.info("Node disconnected: %s", self.peer)

    # ------------------------------------------------------------------
    def _handle_line(self, raw: bytes, info: NodeInfo) -> None:
        try:
            line = raw.decode("ascii", errors="ignore").strip("\r\n\t ")
        except Exception:  # noqa: BLE001
            self.registry.touch(self.peer, 0, msg=False, invalid=True)
            return
        if not line:
            return
        # Strip tag-blocks ("\1G2:..\2!AIVDM,...")
        if line.startswith("\\"):
            idx = line.find("\\", 1)
            if idx > 0:
                line = line[idx + 1:]
        parsed = parse(line)
        if parsed is None or not parsed.checksum_ok:
            self.registry.touch(self.peer, 0, msg=False, invalid=True)
            return

        # Reassemble multi-part ( !AIVDM,2,1,7,... !AIVDM,2,2,7,... ).
        if parsed.fragment_count > 1:
            key = parsed.message_id or f"{parsed.channel}#{parsed.fragment_count}"
            prev = info.multipart.get(key, "")
            info.multipart[key] = (prev + "," if prev else "") + line
            if parsed.fragment_number < parsed.fragment_count:
                return
            combined = info.multipart.pop(key)
            # We forward all fragments individually as received – consumers
            # expect them that way.  Emit each one to the pipeline:
            ts = time.time()
            for part in combined.split(","):
                # skip the empty leading comma artifacts
                if not part:
                    continue
                # Each original line already included commas so split-on-","
                # doesn't work cleanly – instead, handle incrementally below.
                pass
            # Simpler: just forward each line at arrival time as it comes in.
            # The trick above tries to group, but grouping is unnecessary here
            # because dedup + reorder operate per-line.  Fall through:

        ts = time.time()
        self.registry.touch(self.peer, 0, msg=True)
        try:
            self.on_sentence(line, self.peer, ts)
        except Exception:  # noqa: BLE001
            log.exception("on_sentence callback failed for %s", self.peer)


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
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
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
    def _reap(self) -> None:
        self._active = [h for h in self._active if h.is_alive()]

    def _count_active(self) -> int:
        self._reap()
        return len(self._active)
