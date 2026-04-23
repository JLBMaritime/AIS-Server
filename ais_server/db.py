"""SQLite persistence.

Stores:

* ``users``       – login credentials (bcrypt) + must_change_password flag.
* ``endpoints``   – forwarder endpoints (name, protocol, host, port, enabled).
* ``kv``          – free-form key/value store (audit trail, misc settings).

SQLite in WAL mode is more than fast enough for this workload (a few writes
per second at peak, mostly reads) and needs zero external services.

**None of these tables are used in the hot NMEA path.**  The hot path is
100 % in-memory – the DB is only touched when a human makes a change in the
UI/CLI or when a service starts up.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import bcrypt


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    username                TEXT    UNIQUE NOT NULL,
    pw_hash                 TEXT    NOT NULL,
    must_change_password    INTEGER NOT NULL DEFAULT 1,
    created_at              INTEGER NOT NULL,
    last_login              INTEGER
);

CREATE TABLE IF NOT EXISTS endpoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    UNIQUE NOT NULL,
    protocol    TEXT    NOT NULL DEFAULT 'tcp',   -- tcp | udp | http
    host        TEXT    NOT NULL,
    port        INTEGER NOT NULL,
    path        TEXT,                             -- for http
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

DEFAULT_USERNAME = "JLBMaritime"
DEFAULT_PASSWORD = "Admin"


@dataclass
class User:
    id: int
    username: str = ""
    pw_hash: str = ""
    must_change_password: bool = True
    created_at: int = 0
    last_login: Optional[int] = None

    # Flask-Login interface
    @property
    def is_authenticated(self) -> bool: return True
    @property
    def is_active(self) -> bool: return True
    @property
    def is_anonymous(self) -> bool: return False
    def get_id(self) -> str: return str(self.id)


@dataclass
class Endpoint:
    id: int
    name: str
    protocol: str
    host: str
    port: int
    path: Optional[str]
    enabled: bool
    created_at: int
    updated_at: int


class Database:
    def __init__(self, path: str, bcrypt_rounds: int = 12) -> None:
        self.path = path
        self.bcrypt_rounds = bcrypt_rounds
        self._lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    def seed_default_user(self) -> None:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM users")
            if cur.fetchone()["n"] > 0:
                return
            self.create_user(DEFAULT_USERNAME, DEFAULT_PASSWORD,
                             must_change_password=True)

    def create_user(self, username: str, password: str,
                    must_change_password: bool = False) -> None:
        pw_hash = bcrypt.hashpw(password.encode("utf-8"),
                                bcrypt.gensalt(self.bcrypt_rounds)).decode()
        with self._lock:
            self._conn.execute(
                "INSERT INTO users (username, pw_hash, must_change_password, created_at)"
                " VALUES (?, ?, ?, ?)",
                (username, pw_hash, 1 if must_change_password else 0, int(time.time())),
            )

    def _row_to_user(self, row: sqlite3.Row) -> User:
        return User(
            id=row["id"], username=row["username"], pw_hash=row["pw_hash"],
            must_change_password=bool(row["must_change_password"]),
            created_at=row["created_at"], last_login=row["last_login"],
        )

    def get_user_by_id(self, uid: int) -> Optional[User]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_name(self, username: str) -> Optional[User]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return self._row_to_user(row) if row else None

    def verify_password(self, username: str, password: str) -> Optional[User]:
        user = self.get_user_by_name(username)
        if not user:
            return None
        try:
            if bcrypt.checkpw(password.encode("utf-8"),
                              user.pw_hash.encode("utf-8")):
                with self._lock:
                    self._conn.execute(
                        "UPDATE users SET last_login=? WHERE id=?",
                        (int(time.time()), user.id))
                return user
        except ValueError:
            return None
        return None

    def set_password(self, username: str, new_password: str,
                     clear_must_change: bool = True) -> bool:
        pw_hash = bcrypt.hashpw(new_password.encode("utf-8"),
                                bcrypt.gensalt(self.bcrypt_rounds)).decode()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET pw_hash=?, "
                "  must_change_password = CASE WHEN ? THEN 0 "
                "       ELSE must_change_password END "
                "WHERE username=?",
                (pw_hash, 1 if clear_must_change else 0, username),
            )
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------
    def _row_to_endpoint(self, row: sqlite3.Row) -> Endpoint:
        return Endpoint(
            id=row["id"], name=row["name"], protocol=row["protocol"],
            host=row["host"], port=row["port"], path=row["path"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def list_endpoints(self, enabled_only: bool = False) -> List[Endpoint]:
        q = "SELECT * FROM endpoints"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY name"
        with self._lock:
            rows = self._conn.execute(q).fetchall()
        return [self._row_to_endpoint(r) for r in rows]

    def get_endpoint(self, ep_id: int) -> Optional[Endpoint]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM endpoints WHERE id=?", (ep_id,)).fetchone()
        return self._row_to_endpoint(row) if row else None

    def add_endpoint(self, name: str, host: str, port: int,
                     protocol: str = "tcp", path: Optional[str] = None,
                     enabled: bool = True) -> int:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO endpoints (name, protocol, host, port, path, enabled,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, protocol, host, port, path, 1 if enabled else 0, now, now),
            )
        return int(cur.lastrowid)

    def update_endpoint(self, ep_id: int, **kwargs: Any) -> bool:
        allowed = {"name", "protocol", "host", "port", "path", "enabled"}
        sets = []
        vals: List[Any] = []
        for k, v in kwargs.items():
            if k not in allowed:
                continue
            if k == "enabled":
                v = 1 if v else 0
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return False
        sets.append("updated_at=?")
        vals.append(int(time.time()))
        vals.append(ep_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE endpoints SET {', '.join(sets)} WHERE id=?", vals)
        return cur.rowcount > 0

    def delete_endpoint(self, ep_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM endpoints WHERE id=?", (ep_id,))
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # KV
    # ------------------------------------------------------------------
    def kv_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, value),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    def as_dict_endpoints(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": e.id, "name": e.name, "protocol": e.protocol,
                "host": e.host, "port": e.port, "path": e.path,
                "enabled": e.enabled,
            }
            for e in self.list_endpoints()
        ]
