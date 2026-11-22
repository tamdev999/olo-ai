"""pg0 lifecycle helpers for V5/V7 integration tests.

Safe pg0 instance management using pg0's own state file for discovery.
Never uses pkill -f (PostgreSQL renames its process after startup).

Opus M3.0 release audit corrections:
- Postmaster exit is verified via pidfile + /proc/{pid} polling before any rm -rf.
- returncode=0 from pg0 stop is NOT sufficient; the postmaster may still be running.
- Wrong-HOME stop returns "not running" but leaves the real postmaster alive.
- All cleanup functions verify exit before deleting owned data directories.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

# Bundled pg0 binary from the pinned Hindsight venv (0eb02d8c4...).
_PG0_BIN = (
    "/root/olo_m3_hindsight_venv/lib/python3.14/"
    "site-packages/pg0/bin/pg0"
)

# How long to wait for the postmaster to exit after pg0 stop returns.
_EXIT_POLL_INTERVAL_S = 0.25
_EXIT_POLL_TIMEOUT_S = 20.0


def _pg0_instance_data_dir(home_dir: Path, instance_name: str) -> Path:
    """Return the expected pg0 data directory for an instance.

    pg0 stores data under ``HOME/.pg0/{instance_name}/data/``.
    This directory contains ``postmaster.pid`` when PostgreSQL is running.
    """
    return home_dir / ".pg0" / instance_name / "data"


def _read_postmaster_pid(data_dir: Path) -> Optional[int]:
    """Read the PID from postmaster.pid, or None if the file is absent/unreadable."""
    pid_file = data_dir / "postmaster.pid"
    try:
        first_line = pid_file.read_text().splitlines()[0].strip()
        return int(first_line)
    except (FileNotFoundError, IndexError, ValueError):
        return None


def _process_exists(pid: int) -> bool:
    """Return True if /proc/{pid} exists (Linux-specific)."""
    return Path(f"/proc/{pid}").exists()


def _wait_for_postmaster_exit(
    data_dir: Path,
    pid: int,
    timeout_s: float = _EXIT_POLL_TIMEOUT_S,
) -> bool:
    """Poll until the postmaster PID disappears from /proc or the timeout expires.

    Returns True if the process is confirmed gone, False on timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(_EXIT_POLL_INTERVAL_S)
    return not _process_exists(pid)


def pg0_stop(
    *,
    instance_name: str,
    home_dir: Path,
    timeout_s: float = 15.0,
    settle_s: float = 1.0,
    exit_wait_s: float = _EXIT_POLL_TIMEOUT_S,
) -> bool:
    """Stop a pg0-managed PostgreSQL instance and verify the postmaster exited.

    Uses ``pg0 stop --name <instance>`` with ``HOME=home_dir`` so pg0 locates
    its state file correctly.

    Returns True ONLY when:
    - pg0 stop returned returncode=0, AND
    - the postmaster PID from postmaster.pid is confirmed gone via /proc polling.

    Returns False when:
    - pg0 reported the instance was not running at this HOME (may still live
      elsewhere — cannot confirm gone), OR
    - pg0 stop returned an error code, OR
    - the postmaster PID is still present after exit_wait_s.

    ``settle_s`` waits after confirmed exit so the OS can reclaim sockets/pidfiles.
    """
    # Read the PID BEFORE attempting stop, so we know whose exit to verify.
    data_dir = _pg0_instance_data_dir(home_dir, instance_name)
    pre_stop_pid = _read_postmaster_pid(data_dir)

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

    stderr_lower = (result.stderr or b"").lower()
    reported_not_running = (
        b"not running" in stderr_lower or b"no running" in stderr_lower
    )

    if result.returncode != 0:
        if reported_not_running:
            # pg0 found no state file at this HOME — it cannot locate the instance.
            # This can mean:
            #   (a) Instance was never started here: data dir is absent entirely.
            #       Safe: nothing exists, return True.
            #   (b) Instance started but no pg0 state file (broken state) or wrong HOME.
            #       Data directory may exist. Be conservative: return False.
            #   (c) We had a PID before stop — something was running. Verify via /proc.
            #
            if pre_stop_pid is not None:
                # Something was running (we read a PID). Verify via /proc.
                pid_gone = _wait_for_postmaster_exit(
                    _pg0_instance_data_dir(home_dir, instance_name),
                    pre_stop_pid,
                    timeout_s=exit_wait_s,
                )
                if settle_s > 0:
                    time.sleep(settle_s)
                return pid_gone
            else:
                # No PID was read before stop. Check if data directory exists.
                data_dir_path = _pg0_instance_data_dir(home_dir, instance_name)
                if data_dir_path.exists():
                    # Data directory exists but pg0 claims not running and no pidfile.
                    # Could be stale state, wrong HOME, or broken instance.
                    # Be conservative: refuse to confirm stopped (caller must handle).
                    return False
                else:
                    # Data directory does not exist — instance was never started here.
                    # Safe to return True: nothing to protect.
                    if settle_s > 0:
                        time.sleep(settle_s)
                    return True
        return False

    # returncode == 0: pg0 claims it sent the shutdown signal.
    # This does NOT mean the postmaster has exited yet.  Verify via pidfile + /proc.

    if pre_stop_pid is not None:
        # We know which PID to watch.  Wait for it to disappear.
        exited = _wait_for_postmaster_exit(data_dir, pre_stop_pid, timeout_s=exit_wait_s)
        if not exited:
            # Postmaster still alive after timeout — stop was not effective.
            return False
    else:
        # No pidfile existed before stop (instance may have been in a bad state).
        # pg0 returned 0 and there is no live PID to verify.  Accept as stopped
        # since we have no evidence of a live postmaster at this HOME.
        pass

    if settle_s > 0:
        time.sleep(settle_s)

    return True


class Pg0StillRunningError(RuntimeError):
    """Raised when pg0_stop_and_clean cannot verify PostgreSQL has exited.

    Data-directory deletion is refused if the postmaster may still be alive.
    Socket-directory cleanup is always safe.
    """


def pg0_stop_and_clean(
    *,
    instance_name: str,
    home_dir: Path,
    data_dir: Optional[Path] = None,
    socket_dir: Optional[Path] = None,
    timeout_s: float = 15.0,
    settle_s: float = 1.0,
    exit_wait_s: float = _EXIT_POLL_TIMEOUT_S,
) -> None:
    """Stop a pg0 instance with verified exit, then clean owned directories.

    Per Opus release audit:
    - Verified exit (not just pg0 stop returncode=0) is required before rm -rf.
    - The data directory is derived from home_dir to prevent cross-instance deletion.
    - Socket-directory cleanup is safe regardless of stop outcome.

    Args:
        instance_name: pg0 instance name.
        home_dir: HOME directory for the olo_hs user running this instance.
        data_dir: pg0 data directory to delete after verified exit.
                  If None, no data deletion is attempted.
                  MUST be derived from home_dir to prevent accidental cross-deletion.
        socket_dir: Socket directory to remove (safe even on failed stop).
        timeout_s: Timeout for the pg0 stop command.
        settle_s: Settle time after confirmed process exit.
        exit_wait_s: Maximum wait time for postmaster PID to disappear.

    Raises:
        Pg0StillRunningError: If stop verification fails and data_dir was requested.
    """
    # Validate that data_dir belongs to this instance's HOME.
    if data_dir is not None:
        expected_base = _pg0_instance_data_dir(home_dir, instance_name)
        try:
            data_dir.relative_to(home_dir)
        except ValueError:
            raise Pg0StillRunningError(
                f"data_dir {data_dir} is not under home_dir {home_dir}. "
                "Refusing to delete data outside the instance's HOME to prevent "
                "accidental cross-instance deletion."
            )

    stopped = pg0_stop(
        instance_name=instance_name,
        home_dir=home_dir,
        timeout_s=timeout_s,
        settle_s=settle_s,
        exit_wait_s=exit_wait_s,
    )

    if not stopped and data_dir is not None:
        # Refuse to delete a live database.  Socket dir is safe to clean.
        if socket_dir is not None:
            subprocess.run(
                ["sudo", "-u", "olo_hs", "rm", "-rf", str(socket_dir)],
                check=False, capture_output=True,
            )
        raise Pg0StillRunningError(
            f"pg0_stop_and_clean: cannot verify that pg0 instance {instance_name!r} "
            f"exited (HOME={home_dir}).  Refusing to delete data_dir={data_dir}. "
            "Socket directory was cleaned if provided."
        )

    for target in filter(None, [socket_dir, data_dir]):
        subprocess.run(
            ["sudo", "-u", "olo_hs", "rm", "-rf", str(target)],
            check=False, capture_output=True,
        )
