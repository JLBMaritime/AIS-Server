"""Configuration loader.

Reads /etc/ais-server/config.yaml (overridable with the ``AIS_SERVER_CONFIG``
env var) and exposes a single ``load_config()`` function that returns a plain
dict.  A minimal defaults dict is merged in so a sparse / partial config file
never crashes the service.
"""
from __future__ import annotations

import copy
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Dict

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/etc/ais-server/config.yaml"

DEFAULTS: Dict[str, Any] = {
    "ingest":   {"tcp_port": 10110, "bind": "0.0.0.0", "max_clients": 64, "idle_timeout": 120},
    "dedup":    {"ttl_seconds": 30, "max_entries": 200_000},
    "reorder":  {"hold_ms": 2000, "max_queue": 50_000},
    "forwarder":{"default_protocol": "tcp", "queue_size": 10_000,
                 "max_retries": 10, "backoff_initial": 1.0},
    "web":      {"host": "0.0.0.0", "port": 80,
                 "tls_cert": None, "tls_key": None, "secret_key": ""},
    "security": {"force_password_change_on_first_login": True,
                 "bcrypt_rounds": 12, "session_minutes": 240},
    "logging":  {"path": "/var/log/ais-server", "level": "INFO"},
    "paths":    {"db":     "/var/lib/ais-server/ais.db",
                 "secret": "/var/lib/ais-server/secret_key",
                 "backup": "/var/lib/ais-server/backups"},
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _ensure_secret_key(cfg: Dict[str, Any]) -> None:
    """If no secret_key is configured, generate one and persist it."""
    if cfg["web"].get("secret_key"):
        return
    secret_path = Path(cfg["paths"]["secret"])
    try:
        if secret_path.exists():
            cfg["web"]["secret_key"] = secret_path.read_text(encoding="utf-8").strip()
            return
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(48)
        secret_path.write_text(key, encoding="utf-8")
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            pass
        cfg["web"]["secret_key"] = key
    except PermissionError:
        # Developer mode (no /var/lib access) – fall back to ephemeral key.
        log.warning("Cannot write %s – using ephemeral secret_key", secret_path)
        cfg["web"]["secret_key"] = secrets.token_urlsafe(48)


def load_config(path: str | None = None) -> Dict[str, Any]:
    path = path or os.environ.get("AIS_SERVER_CONFIG") or DEFAULT_CONFIG_PATH
    cfg_path = Path(path)

    file_cfg: Dict[str, Any] = {}
    if cfg_path.exists():
        try:
            with cfg_path.open("r", encoding="utf-8") as fh:
                file_cfg = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            log.error("Invalid YAML in %s: %s – falling back to defaults", path, exc)
            file_cfg = {}
    else:
        log.warning("Config file %s not found – using built-in defaults", path)

    cfg = _deep_merge(DEFAULTS, file_cfg)
    _ensure_secret_key(cfg)

    # Ensure log directory exists (best-effort).
    try:
        Path(cfg["logging"]["path"]).mkdir(parents=True, exist_ok=True)
    except PermissionError:
        pass
    try:
        Path(cfg["paths"]["db"]).parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        pass

    return cfg
