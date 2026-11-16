"""Olo AI — path helpers resolved per call.

HX001: never hardcode ~/.hermes; always call get_hermes_home() at use time.
No side effects at import time.
"""
from __future__ import annotations

import os
from pathlib import Path

from hermes_constants import get_hermes_home


def olo_home() -> Path:
    """Return the Olo data directory inside the active Hermes home.

    Created with mode 0o700 on first call when it does not exist.
    Restricts an existing directory to 0o700 on POSIX if it is wider.
    """
    path = get_hermes_home() / "olo"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def db_path() -> Path:
    """Return the path to the Olo SQLite database.

    The parent directory (olo_home()) is created with mode 0o700.
    The file itself is created with mode 0o600 by the store layer.
    """
    return olo_home() / "olo.db"
