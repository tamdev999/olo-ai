"""olo/m3/pg0_lifecycle.py — Safe pg0 PostgreSQL instance lifecycle management.

Provides the authoritative implementation for stopping and cleaning up pg0
instances used by Hindsight integration tests and benchmarks.

Design principles (Opus release audit, fourth+fifth remediation):
- **Enumerate, don't guess.**  ``safe_pg0_delete_home`` and ``safe_pg0_stop_all``
  find EVERY instance by scanning the canonical pg0 layout
  (``HOME/.pg0/instances/*/``) rather than accepting a caller-supplied name.
  This eliminates N2/N7/N9 bugs where a wrong or guessed instance name caused
  the helper to miss live postmasters.
- **Verify, don't trust exit codes.**  ``pg0_stop`` checks the PID read from
  ``postmaster.pid`` against ``/proc/{pid}`` rather than relying on
  ``pg0 stop`` returning 0.  returncode=0 is necessary but not sufficient.
- **Fail closed.**  Any stop failure raises ``Pg0StillRunningError`` before
  any directory deletion.  ``except: pass`` around stop failures is prohibited.
- **Derived paths only.**  ``pg0_stop_and_clean`` derives the data directory
  from ``(home_dir, instance_name)`` via ``_pg0_instance_data_dir`` and never
  accepts a caller-supplied path.

No side effects at import time.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Bundled pg0 binary from the pinned Hindsight venv.
#: Override in tests by patching this module attribute.
_PG0_BIN = (
    "/root/olo_m3_hindsight_venv/lib/python3.14/"
    "site-packages/pg0/bin/pg0"
)

#: Poll interval when waiting for a postmaster to exit.
_EXIT_POLL_INTERVAL_S = 0.25

#: Default maximum wait time for the postmaster to exit after pg0 stop.
_EXIT_POLL_TIMEOUT_S = 20.0

#: User that owns pg0 processes and home directories in the gate environment.
_OLO_HS_USER = "olo_hs"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _pg0_instance_data_dir(home_dir: Path, instance_name: str) -> Path:
    """Return the pg0 data directory for an instance.

    pg0 0.17.0 layout: ``HOME/.pg0/instances/{instance_name}/data/``

    The ``instances/`` intermediate component was added in pg0 0.17.0.  Earlier
    code used ``HOME/.pg0/{instance_name}/data/`` which never matched a real
    running server, causing the safety checks (_read_postmaster_pid, /proc
    polling) to silently skip — the root cause of the N1-N7 audit findings.
    """
    return home_dir / ".pg0" / "instances" / instance_name / "data"


def _list_pg0_instances(home_dir: Path) -> list[str]:
    """Return every pg0 instance name under ``HOME/.pg0/instances/``.

    Enumerates the instances directory rather than accepting a caller-supplied
    name.  This is the core fix for N2/N7/N9: a wrong or guessed instance name
    cannot cause the helper to miss a live postmaster.

    Returns an empty list when no pg0 state is present (never raises).
    """
    instances_root = home_dir / ".pg0" / "instances"
    try:
        if not instances_root.exists():
            return []
        return [p.name for p in instances_root.iterdir() if p.is_dir()]
    except PermissionError:
        # Fallback: running as non-root, use sudo to list the directory.
        # This handles gate environments where HOME is owned by olo_hs.
        try:
            result = subprocess.run(
                ["sudo", "-u", _OLO_HS_USER, "ls", str(instances_root)],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                return [n.strip() for n in result.stdout.splitlines() if n.strip()]
        except Exception:
            pass
        return []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Postmaster state helpers
# ---------------------------------------------------------------------------

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
    """Poll until the postmaster PID disappears from /proc or timeout expires.

    Returns True if confirmed gone, False on timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(_EXIT_POLL_INTERVAL_S)
    return not _process_exists(pid)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class Pg0StillRunningError(RuntimeError):
    """Raised when a postmaster cannot be verified stopped.

    Any attempted data-directory or home-directory deletion is refused.
    Socket-directory cleanup is always safe.
    """


# ---------------------------------------------------------------------------
# Single-instance stop
# ---------------------------------------------------------------------------

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
    its own state file correctly.

    Returns True ONLY when:
    - ``pg0 stop`` returned 0, AND
    - the postmaster PID from ``postmaster.pid`` is confirmed gone via /proc.

    Returns False when:
    - pg0 reported the instance was not running at this HOME, OR
    - pg0 stop returned an error code, OR
    - the postmaster PID is still present after ``exit_wait_s``.

    ``settle_s`` waits after confirmed exit so the OS can reclaim sockets.
    """
    data_dir = _pg0_instance_data_dir(home_dir, instance_name)
    pre_stop_pid = _read_postmaster_pid(data_dir)

    result = subprocess.run(
        [
            "sudo", "-u", _OLO_HS_USER,
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
            if pre_stop_pid is not None:
                # Something was running (we read a PID) — verify via /proc.
                pid_gone = _wait_for_postmaster_exit(
                    data_dir, pre_stop_pid, timeout_s=exit_wait_s,
                )
                if settle_s > 0:
                    time.sleep(settle_s)
                return pid_gone
            else:
                # No PID found before stop.  Check if data dir exists.
                if data_dir.exists():
                    # Data dir present but no pidfile and pg0 says not running.
                    # Could be stale state or wrong HOME.  Be conservative.
                    return False
                else:
                    # Data directory absent — instance was never started here.
                    if settle_s > 0:
                        time.sleep(settle_s)
                    return True
        return False

    # returncode == 0: pg0 sent the shutdown signal — but the process may still be alive.
    if pre_stop_pid is not None:
        exited = _wait_for_postmaster_exit(data_dir, pre_stop_pid, timeout_s=exit_wait_s)
        if not exited:
            return False
    # else: no pidfile before stop — accept as stopped (no live PID to verify)

    if settle_s > 0:
        time.sleep(settle_s)
    return True


# ---------------------------------------------------------------------------
# Single-instance stop + canonical data-dir deletion
# ---------------------------------------------------------------------------

def pg0_stop_and_clean(
    *,
    instance_name: str,
    home_dir: Path,
    socket_dir: Optional[Path] = None,
    timeout_s: float = 15.0,
    settle_s: float = 1.0,
    exit_wait_s: float = _EXIT_POLL_TIMEOUT_S,
) -> None:
    """Stop one pg0 instance and delete its canonical data directory.

    The data directory is ALWAYS derived from the canonical layout
    (``_pg0_instance_data_dir(home_dir, instance_name)``); no caller-supplied
    path is accepted.

    Raises ``Pg0StillRunningError`` if stop cannot be verified.
    Socket directory is cleaned even on stop failure.
    """
    data_dir = _pg0_instance_data_dir(home_dir, instance_name)

    stopped = pg0_stop(
        instance_name=instance_name,
        home_dir=home_dir,
        timeout_s=timeout_s,
        settle_s=settle_s,
        exit_wait_s=exit_wait_s,
    )

    if not stopped:
        if socket_dir is not None:
            subprocess.run(
                ["sudo", "-u", _OLO_HS_USER, "rm", "-rf", str(socket_dir)],
                check=False, capture_output=True,
            )
        raise Pg0StillRunningError(
            f"pg0_stop_and_clean: instance {instance_name!r} in {home_dir} "
            f"did not stop within {exit_wait_s:.0f}s. "
            f"Refusing to delete {data_dir}."
        )

    for target in filter(None, [socket_dir, data_dir]):
        subprocess.run(
            ["sudo", "-u", _OLO_HS_USER, "rm", "-rf", str(target)],
            check=False, capture_output=True,
        )


# ---------------------------------------------------------------------------
# Multi-instance helpers
# ---------------------------------------------------------------------------

def safe_pg0_stop_all(
    home_dir: Path,
    *,
    timeout_s: float = 15.0,
    settle_s: float = 1.0,
    exit_wait_s: float = _EXIT_POLL_TIMEOUT_S,
) -> list[str]:
    """Stop ALL pg0 instances in ``home_dir`` without deleting the directory.

    Enumerates ``HOME/.pg0/instances/*/`` rather than accepting a name from
    the caller.  This eliminates N2/N7/N9 bugs where a guessed or mismatched
    instance name caused the helper to miss live postmasters.

    Returns the list of instance names that were stopped.

    Raises ``Pg0StillRunningError`` listing every instance that could not be
    verified stopped.  Caller MUST NOT delete any directory on this error.
    """
    instances = _list_pg0_instances(home_dir)
    stopped: list[str] = []
    errors: list[str] = []

    for inst in instances:
        ok = pg0_stop(
            instance_name=inst,
            home_dir=home_dir,
            timeout_s=timeout_s,
            settle_s=settle_s,
            exit_wait_s=exit_wait_s,
        )
        if ok:
            stopped.append(inst)
        else:
            errors.append(inst)

    if errors:
        raise Pg0StillRunningError(
            f"safe_pg0_stop_all: {len(errors)} pg0 instance(s) could not be "
            f"verified stopped in {home_dir}: {errors}. "
            "Refusing to allow directory deletion."
        )
    return stopped


def safe_pg0_delete_home(
    home_dir: Path,
    *,
    timeout_s: float = 15.0,
    settle_s: float = 1.0,
    exit_wait_s: float = _EXIT_POLL_TIMEOUT_S,
) -> None:
    """Stop ALL pg0 instances in ``home_dir``, then delete the entire directory.

    This is the ONLY safe way to delete a pg0 HOME directory.

    Algorithm:
    1. Enumerate every instance under ``HOME/.pg0/instances/*/``.
    2. Stop each instance with verified postmaster exit.
    3. If ANY instance cannot be verified stopped, raise ``Pg0StillRunningError``
       and do NOT delete anything.
    4. Only after ALL instances are confirmed gone, delete ``home_dir``.

    Eliminates:
    - N2: guessed/wrong instance name misses live postmaster.
    - N5: unconditional ``rm -rf HOME`` after a failed or skipped stop.
    - N6: raw pre-clean without stop when a previous run left a postmaster.
    - N7: ignored stop failure before ``rm -rf``.
    - N9: stopping one instance and then deleting a HOME containing another.

    Raises ``Pg0StillRunningError`` if any instance is still running.
    Does NOT raise on file-system errors during the final deletion.
    """
    # Stop all instances first — raises if any cannot be verified gone.
    safe_pg0_stop_all(
        home_dir,
        timeout_s=timeout_s,
        settle_s=settle_s,
        exit_wait_s=exit_wait_s,
    )

    # All instances confirmed stopped.  Safe to delete.
    subprocess.run(
        ["sudo", "-u", _OLO_HS_USER, "rm", "-rf", str(home_dir)],
        check=False, capture_output=True,
    )
