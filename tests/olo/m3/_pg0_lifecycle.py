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
    stderr_lower = (result.stderr or b"").lower()

    # "not running" or similar means pg0 didn't find a state file at this HOME.
    # This is NOT sufficient to prove the postmaster is gone -- it only proves
    # pg0 can't locate it at the given HOME. A wrong-HOME stop would say
    # "No running instance found" while the postmaster lives under a different HOME.
    reported_not_running = (
        b"not running" in stderr_lower or b"no running" in stderr_lower
    )

    if result.returncode == 0:
        # pg0 claims success (found and stopped the instance)
        return True

    if reported_not_running:
        # pg0 didn't find a state file at this HOME.
        # We CANNOT confirm the postmaster is gone -- we can only confirm
        # pg0 has no record of it at this specific HOME directory.
        # Return False to indicate "stop was not verified" rather than
        # falsely claiming success.
        return False

    # Other failure
    return False


class Pg0StillRunningError(RuntimeError):
    """Raised when pg0_stop_and_clean cannot verify that PostgreSQL exited.

    Per Opus audit: an unsuccessful stop must never be treated as success when
    the cleanup would delete a live database.  ``pg0_stop_and_clean`` raises
    this if ``pg0_stop`` returns False — meaning the postmaster may still be
    alive — and the caller requested ``data_dir`` deletion.

    It is always safe to wipe the socket directory (reconnectable) even on a
    failed stop.  Deleting the data directory on a live instance is never safe.
    """


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

    Per Opus audit §4.A: an unsuccessful stop must NEVER cause data deletion.
    Stop is verified before any data-directory wipe.

    ``data_dir``   — wipe pg0 data directory (use for fresh restarts only;
                     never wipe during a crash-recovery test).
    ``socket_dir`` — wipe Unix socket directory; safe to remove and recreate
                     between attempts even if stop partially failed.

    Raises
    ------
    Pg0StillRunningError
        If ``pg0_stop`` returns False (instance may still be alive) AND
        ``data_dir`` deletion was requested.  Callers that only clean the
        socket directory do not get this exception.
    """
    stopped = pg0_stop(
        instance_name=instance_name,
        home_dir=home_dir,
        timeout_s=timeout_s,
        settle_s=settle_s,
    )

    if not stopped and data_dir is not None:
        # Refuse to delete a live database.  Wipe only the socket dir so the
        # caller can retry starting on a fresh socket path.
        if socket_dir is not None:
            subprocess.run(
                ["sudo", "-u", "olo_hs", "rm", "-rf", str(socket_dir)],
                check=False, capture_output=True,
            )
        raise Pg0StillRunningError(
            f"pg0_stop_and_clean: failed to verify that pg0 instance "
            f"{instance_name!r} exited (HOME={home_dir}).  Refusing to "
            f"delete data_dir={data_dir} while the postmaster may still be "
            f"alive.  Socket directory was cleaned if provided."
        )

    for target in filter(None, [socket_dir, data_dir]):
        subprocess.run(
            ["sudo", "-u", "olo_hs", "rm", "-rf", str(target)],
            check=False, capture_output=True,
        )
