#!/usr/bin/env python3
"""Replay a file of NMEA AIS sentences to a TCP endpoint.

Useful for smoke-testing the server from a workstation – it acts like a
single AIS node.  Optionally replays through two sockets with a small time
skew to exercise the dedup + reorder pipeline.

    python tools/replay.py --host 127.0.0.1 --port 10110 samples.nmea
    python tools/replay.py --duplicates 2 --skew-ms 500 samples.nmea
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from pathlib import Path


def sender(host: str, port: int, lines: list[str], rate: float,
           skew_ms: int, label: str) -> None:
    time.sleep(skew_ms / 1000.0)
    with socket.create_connection((host, port), timeout=10) as s:
        print(f"[{label}] connected to {host}:{port} – sending {len(lines)} lines")
        for line in lines:
            s.sendall((line.rstrip() + "\r\n").encode("ascii", "ignore"))
            if rate > 0:
                time.sleep(1.0 / rate)
        print(f"[{label}] done")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", type=Path, help="NMEA file (one sentence per line)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=10110)
    ap.add_argument("--rate", type=float, default=20.0,
                    help="Sentences per second per sender (0 = as fast as possible)")
    ap.add_argument("--duplicates", type=int, default=1,
                    help="Number of parallel senders (simulates N nodes)")
    ap.add_argument("--skew-ms", type=int, default=250,
                    help="Stagger between senders (milliseconds)")
    args = ap.parse_args()

    lines = [ln for ln in args.file.read_text(encoding="utf-8", errors="ignore"
                                              ).splitlines() if ln.strip()]
    if not lines:
        print("No lines to send", file=sys.stderr)
        return 1
    threads = []
    for i in range(args.duplicates):
        t = threading.Thread(
            target=sender,
            args=(args.host, args.port, lines, args.rate,
                  args.skew_ms * i, f"node-{i+1}"),
            daemon=True,
        )
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
