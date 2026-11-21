"""pg0 lifecycle helpers for V5/V7 integration tests.

Safe pg0 instance management that uses pg0's own state file for discovery.
Never uses pkill -f (PostgreSQL renames its process after startup, making
name-pattern kills unreliable).

Opus M3.0 stability audit — pg0 lifecycle corrections.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

# Bundled pg0 binary from the pinned Hindsight venv (0eb02d8c4...).
_PG0_BIN = (
    "/root/olo_m3_hindsight_venv/lib/python3.14/"
    "site-packages/pg0/bin/pg0"
)


def pg0_stop(
    *,
    instance_name: str,
    home_dir: Path,
    timeout_s: float = 15.0,
    settle_s: float = 2.0,
) -> bool:
    """Stop a pg0-managed PostgreSQL instance by name.

    Uses ``pg0 stop --name <instance>`` with ``HOME=home_dir`` so pg0 locates
    its state file correctly.  Returns True when the instance was stopped or
    was not running; False on unexpected failure.

    ``settle_s`` waits after the stop command returns so the OS can reclaim
    the socket and pid file before the next start attempt.
    """
    result = subprocess.run(
        [
            "sudo", "-u", "olo_hs",
            "env", f"HOME={home_dir}",
            _PG0_BIN, "stop", "--name", instance_name,
        ],
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    if settle_s > 0:
        time.sleep(settle_s)
    # Success: returncode 0 OR stderr says not running
    stderr_lower = (result.stderr or b"").lower()
    return result.returncode == 0 or b"not running" in stderr_lower or b"no running" in stderr_lower


def pg0_stop_and_clean(
    *,
    instance_name: str,
    home_dir: Path,
    data_dir: Path | None = None,
    socket_dir: Path | None = None,
    timeout_s: float = 15.0,
    settle_s: float = 2.0,
) -> None:
    """Stop a pg0 instance and optionally remove its data/socket directories.

    Per Opus's requirements: never delete instance files while the postmaster
    is alive.  Stop first, verify exit, then clean up.

    ``data_dir``   — wipe pg0 data directory (use for fresh restarts only;
                     never wipe during a crash-recovery test).
    ``socket_dir`` — wipe Unix socket directory; always safe to remove and
                     recreate between attempts.
    """
    pg0_stop(
        instance_name=instance_name,
        home_dir=home_dir,
        timeout_s=timeout_s,
        settle_s=settle_s,
    )

    for target in filter(None, [socket_dir, data_dir]):
        subprocess.run(
            ["sudo", "-u", "olo_hs", "rm", "-rf", str(target)],
            check=False, capture_output=True,
        )
