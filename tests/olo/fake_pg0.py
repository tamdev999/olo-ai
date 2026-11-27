"""A stand-in for the embedded PostgreSQL (pg0) postmaster Hindsight starts.

Like the real one it runs detached from the server that started it, works
from its data directory and records itself in ``postmaster.pid`` (first line:
its pid); SIGINT (fast shutdown) or SIGTERM ends it and removes the file.

    python fake_pg0.py <data dir>
"""
from __future__ import annotations

import os
import signal
import sys
from pathlib import Path


def main() -> None:
    data = Path(sys.argv[1])
    data.mkdir(parents=True, exist_ok=True)
    os.chdir(data)
    pidfile = data / "postmaster.pid"
    pidfile.write_text(f"{os.getpid()}\n{data}\n", encoding="utf-8")

    def shutdown(*_) -> None:
        pidfile.unlink(missing_ok=True)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
