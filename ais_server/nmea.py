"""NMEA 0183 AIS sentence helpers.

Only the bits we actually need for the server:

* ``validate_checksum()`` – returns ``True`` if the trailing ``*HH`` matches.
* ``canonicalise()``      – trimmed, upper-cased checksum, used as the dedup
                            hashing input so CRLF / whitespace differences
                            between nodes don't break dedup.
* ``parse()``             – light structural parse (type / part / payload).
* ``extract_timestamp()`` – best-effort UTC second for Type 4 (base station)
                            sentences; returns ``None`` otherwise so the
                            reorder layer can fall back to arrival time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

AIS_RE = re.compile(r"^!(AIVDM|AIVDO|BSVDM|ABVDM|ANVDM|ARVDM|AXVDM|AIVDO),")


def validate_checksum(sentence: str) -> bool:
    try:
        if "*" not in sentence:
            return False
        body, checksum = sentence.rsplit("*", 1)
        body = body.lstrip("!$")
        cs = 0
        for ch in body:
            cs ^= ord(ch)
        return cs == int(checksum[:2], 16)
    except (ValueError, IndexError):
        return False


def canonicalise(sentence: str) -> str:
    """Return a canonical form used as the dedup hash input.

    * Trim whitespace / CR / LF.
    * Upper-case the checksum hex.
    * Drop anything after the two-char checksum (some gateways append tags).
    """
    s = sentence.strip()
    if "*" in s:
        body, rest = s.rsplit("*", 1)
        cs = rest[:2].upper()
        return f"{body}*{cs}"
    return s


@dataclass(frozen=True)
class NmeaParse:
    sentence_type: str   # "!AIVDM" etc.
    fragment_count: int
    fragment_number: int
    message_id: str      # "" or "1"
    channel: str         # "A"/"B"
    payload: str
    fill_bits: int
    checksum_ok: bool


def parse(sentence: str) -> Optional[NmeaParse]:
    try:
        if not AIS_RE.match(sentence):
            return None
        checksum_ok = validate_checksum(sentence)
        head = sentence.split("*", 1)[0]
        parts = head.split(",")
        if len(parts) < 7:
            return None
        return NmeaParse(
            sentence_type=parts[0],
            fragment_count=int(parts[1]),
            fragment_number=int(parts[2]),
            message_id=parts[3],
            channel=parts[4],
            payload=parts[5],
            fill_bits=int(parts[6]) if parts[6].isdigit() else 0,
            checksum_ok=checksum_ok,
        )
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Minimal 6-bit payload decoder – just enough to read MMSI / msg type /
# a Type-4 UTC timestamp for chronological ordering.
# ---------------------------------------------------------------------------
def _bits(payload: str) -> str:
    out = []
    for ch in payload:
        v = ord(ch) - 48
        if v > 40:
            v -= 8
        out.append(format(v & 0x3F, "06b"))
    return "".join(out)


def extract_mmsi_and_type(payload: str) -> tuple[Optional[int], Optional[int]]:
    try:
        b = _bits(payload)
        if len(b) < 38:
            return None, None
        return int(b[8:38], 2), int(b[0:6], 2)
    except ValueError:
        return None, None


def extract_timestamp(payload: str) -> Optional[float]:
    """Return a UNIX timestamp for Type 4 / 11 (base station) sentences.

    Falls back to ``None`` for all other message types – the reorder layer
    will then use the server arrival time, which is what every production AIS
    system does.
    """
    try:
        b = _bits(payload)
        if len(b) < 6:
            return None
        msg_type = int(b[0:6], 2)
        if msg_type not in (4, 11) or len(b) < 80:
            return None
        year   = int(b[38:52], 2)
        month  = int(b[52:56], 2)
        day    = int(b[56:61], 2)
        hour   = int(b[61:66], 2)
        minute = int(b[66:72], 2)
        second = int(b[72:78], 2)
        if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24
                and minute < 60 and second < 60 and year >= 1970):
            return None
        return datetime(year, month, day, hour, minute, second,
                        tzinfo=timezone.utc).timestamp()
    except (ValueError, OverflowError):
        return None
