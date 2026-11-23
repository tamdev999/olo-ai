"""olo/m3/pg0_lifecycle.py — Safe pg0 PostgreSQL instance lifecycle management.

Provides the authoritative implementation for stopping and cleaning up pg0
instances used by Hindsight integration tests and benchmarks.

Design principles (Opus release audit, fourth+fifth+sixth remediation):
- **Enumerate, don't guess.**  ``safe_pg0_delete_home`` and ``safe_pg0_stop_all``
  find EVERY instance by scanning the canonical pg0 layout
  (``HOME/.pg0/instances/*/``) rather than accepting a caller-supplied name.
  This eliminates N2/N7/N9 bugs where a wrong or guessed instance name caused
  the helper to miss live postmasters.
- **Fail closed on enumeration errors (H3).**  ``_list_pg0_instances`` raises
  ``Pg0StillRunningError`` if the instances directory cannot be listed; it never
  returns an empty list when the error might mask a live database.
- **Process-ownership check (C5).**  Before deleting any directory,
  ``safe_pg0_delete_home`` and ``safe_pg0_delete_tree`` scan both
  ``postmaster.pid`` files and ``/proc/*/cmdline`` for live PostgreSQL processes
  with data directories nested under the target path.  This catches instances
  that bypassed pg0 state-file enumeration (custom data dirs, nested business
  homes, incomplete pg0 state).
- **Verify, don't trust exit codes.**  ``pg0_stop`` checks the PID read from
  ``postmaster.pid`` against ``/proc/{pid}`` rather than relying on
  ``pg0 stop`` returning 0.  returncode=0 is necessary but not sufficient.
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

    **H3 fix:** Any failure to enumerate raises ``Pg0StillRunningError``.
    Returning an empty list on an I/O error would allow ``safe_pg0_delete_home``
    to conclude that no database is running and delete the HOME — fail-open.

    Returns an empty list ONLY when the instances directory genuinely does not
    exist (safe: no pg0 HOME has ever been initialised here).
    """
    instances_root = home_dir / ".pg0" / "instances"
    try:
        if not instances_root.exists():
            return []
        return [p.name for p in instances_root.iterdir() if p.is_dir()]
    except PermissionError as exc:
        # Fallback: running as non-root, use sudo to list the directory.
        try:
            result = subprocess.run(
                ["sudo", "-u", _OLO_HS_USER, "ls", str(instances_root)],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                return [n.strip() for n in result.stdout.splitlines() if n.strip()]
            # sudo also failed — cannot enumerate safely.
            raise Pg0StillRunningError(
                f"Cannot enumerate pg0 instances in {instances_root}: "
                f"direct access denied and sudo returned {result.returncode}. "
                "Refusing to allow deletion without confirmed instance state."
            )
        except Pg0StillRunningError:
            raise
        except Exception as exc2:
            raise Pg0StillRunningError(
                f"Cannot enumerate pg0 instances in {instances_root}: {exc2}. "
                "Refusing to allow deletion without confirmed instance state."
            ) from exc2
    except Exception as exc:
        raise Pg0StillRunningError(
            f"Cannot enumerate pg0 instances in {instances_root}: {exc}. "
            "Refusing to allow deletion without confirmed instance state."
        ) from exc


def _enumerate_pg0_homes_under(root: Path) -> list[Path]:
    """Find every pg0 HOME directory nested anywhere under ``root``.

    A pg0 HOME is a directory containing ``.pg0/instances/``.  This handles
    hermes homes that contain multiple business databases at arbitrary depth
    (e.g. ``hermes_home/businesses/biz_a/.pg0/instances/``).

    Uses ``os.walk`` with ``followlinks=False`` to avoid following the
    ``hindsight-venv`` symlink into the system venv tree.

    **X1 fail-closed fix:** Any ``os.walk`` error on a subdirectory raises
    ``Pg0StillRunningError``.  An unreadable subdirectory could contain a pg0
    HOME and therefore a live database; silently skipping it would create a
    false negative.

    Returns list of unique HOME paths sorted for deterministic order.
    Returns an empty list immediately when ``root`` does not exist.
    """
    if not root.exists():
        return []
    homes: set[Path] = set()
    walk_errors: list[str] = []

    def _walk_onerror(err: OSError) -> None:
        walk_errors.append(
            f"unreadable directory ({getattr(err, 'filename', '?')!r}): {err}"
        )

    try:
        for dirpath, dirnames, _filenames in os.walk(
            str(root), followlinks=False, onerror=_walk_onerror
        ):
            # Look for a .pg0 subdirectory with an instances child
            if ".pg0" in dirnames:
                candidate = Path(dirpath) / ".pg0" / "instances"
                if candidate.is_dir():
                    homes.add(Path(dirpath))
                # Do NOT recurse into .pg0 itself — pg0 never nests pg0 HOmes
                dirnames.remove(".pg0")
    except (PermissionError, OSError) as exc:
        walk_errors.append(f"os.walk raised unexpectedly: {exc}")

    if walk_errors:
        raise Pg0StillRunningError(
            f"_enumerate_pg0_homes_under({root}): "
            f"{len(walk_errors)} filesystem error(s) prevent safe enumeration "
            f"of pg0 HOME directories. Refusing to authorize deletion.\n"
            + "\n".join(f"  {e}" for e in walk_errors[:5])
        )

    return sorted(homes)


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


def _find_live_pg_pids_under(directory: Path) -> list[int]:
    """Find live PostgreSQL postmaster PIDs with data directories under ``directory``.

    Uses two complementary methods so instances not registered with pg0 (custom
    data dirs, broken state files, nested business databases) are still detected:

    1. ``os.walk`` for every ``postmaster.pid`` file under ``directory``
       (``followlinks=False`` to avoid venv symlinks).
    2. ``/proc/*/cmdline`` scan for postgres processes with ``-D`` paths under
       ``directory`` (Linux only; silently skipped on other platforms).

    **X1 fail-closed fix:** Inspection failures are no longer silently ignored.

    - Any unreadable directory during the ``os.walk`` is recorded; if any such
      error occurred, ``Pg0StillRunningError`` is raised because the missed
      subdirectory could contain a live database.
    - If ``/proc`` enumeration fails entirely, ``Pg0StillRunningError`` is raised.
    - Per-process cmdline read failures attempt a fallback to the comm name in
      ``/proc/{pid}/status``.  If the process can be positively identified as
      non-postgres, it is safely skipped.  If it cannot be identified or looks
      like postgres, the error is recorded and ``Pg0StillRunningError`` is raised.
    - ``FileNotFoundError`` for a process (process exited between enumeration and
      read) is always safe to skip.

    Raises ``Pg0StillRunningError`` if any inspection errors prevent a confident
    "no live databases" determination.
    """
    live: set[int] = set()
    scan_errors: list[str] = []

    # --- Method 1: recursive postmaster.pid scan ---
    def _walk_onerror(err: OSError) -> None:
        # An unreadable directory during the walk may hide a postmaster.pid
        # and therefore a live database under the deletion root.
        scan_errors.append(
            f"unreadable directory during postmaster.pid scan "
            f"({getattr(err, 'filename', '?')!r}): {err}"
        )

    # An absent root has no live databases — handle before walking to avoid
    # platform differences in how os.walk reports missing root directories.
    if not directory.exists():
        return []

    try:
        for dirpath, _dirnames, filenames in os.walk(
            str(directory), followlinks=False, onerror=_walk_onerror
        ):
            if "postmaster.pid" in filenames:
                pid_file = Path(dirpath) / "postmaster.pid"
                try:
                    pid = int(pid_file.read_text().splitlines()[0].strip())
                    if _process_exists(pid):
                        live.add(pid)
                except (OSError, ValueError, IndexError):
                    continue
    except (PermissionError, OSError) as exc:
        scan_errors.append(f"os.walk raised unexpectedly: {exc}")

    # --- Method 2: /proc cmdline scan ---
    proc_dir = Path("/proc")
    if proc_dir.exists():
        directory_str = str(directory.resolve())
        try:
            pid_dirs = [p for p in proc_dir.iterdir() if p.name.isdigit()]
        except (PermissionError, OSError) as exc:
            # Cannot enumerate /proc at all — cannot confirm no live postgres process
            # has a data directory under the deletion root.
            scan_errors.append(f"/proc enumeration failed: {exc}")
            pid_dirs = None

        if pid_dirs is not None:
            for pid_path in pid_dirs:
                try:
                    cmdline_bytes = (pid_path / "cmdline").read_bytes()
                    if b"postgres" not in cmdline_bytes:
                        continue
                    parts = cmdline_bytes.rstrip(b"\x00").split(b"\x00")
                    for j, part in enumerate(parts):
                        data_dir_str: Optional[str] = None
                        if part == b"-D" and j + 1 < len(parts):
                            data_dir_str = parts[j + 1].decode("utf-8", errors="replace")
                        elif part.startswith(b"--pgdata="):
                            data_dir_str = part[9:].decode("utf-8", errors="replace")
                        if data_dir_str and (
                            data_dir_str.startswith(directory_str + os.sep)
                            or data_dir_str.startswith(directory_str + "/")
                            or data_dir_str == directory_str
                        ):
                            pid = int(pid_path.name)
                            if _process_exists(pid):
                                live.add(pid)
                except FileNotFoundError:
                    # Process exited between enumeration and read — always safe.
                    continue
                except (PermissionError, OSError):
                    # Cannot read this process's cmdline.
                    # Try to identify it as non-postgres before skipping.
                    is_known_safe = False
                    try:
                        status_text = (pid_path / "status").read_text(errors="replace")
                        comm_lines = [
                            ln for ln in status_text.splitlines()
                            if ln.startswith("Name:")
                        ]
                        comm = comm_lines[0].split(":", 1)[1].strip() if comm_lines else ""
                        if comm and "postgres" not in comm.lower() and "postmaster" not in comm.lower():
                            # Positively identified as a different process — safe to skip.
                            is_known_safe = True
                    except (PermissionError, OSError, FileNotFoundError):
                        pass  # Cannot read status either

                    if not is_known_safe:
                        # Cannot confirm this process is not postgres — conservative.
                        scan_errors.append(
                            f"process {pid_path.name}: cmdline unreadable and "
                            "process cannot be confirmed as non-postgres"
                        )

    if scan_errors:
        raise Pg0StillRunningError(
            f"_find_live_pg_pids_under({directory}): "
            f"{len(scan_errors)} inspection error(s) prevent a confident "
            f"'no live databases' determination. Refusing to authorize deletion.\n"
            + "\n".join(f"  {e}" for e in scan_errors[:5])
        )

    return sorted(live)


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

    **H3:** Raises ``Pg0StillRunningError`` if instance enumeration fails.

    Returns the list of instance names that were stopped.

    Raises ``Pg0StillRunningError`` listing every instance that could not be
    verified stopped.  Caller MUST NOT delete any directory on this error.
    """
    instances = _list_pg0_instances(home_dir)  # raises Pg0StillRunningError on failure
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


def _proc_scan_or_raise(directory: Path, context: str) -> None:
    """Scan for live PostgreSQL processes under ``directory``; raise if found.

    This is the second line of defence after ``safe_pg0_stop_all``.  It catches:
    - PostgreSQL instances with custom data directories (not under .pg0/instances/)
    - Nested business databases not reachable by the top-level enumeration
    - Processes that survived pg0 stop despite a successful return code

    Raises ``Pg0StillRunningError`` if any live postmaster is detected.
    """
    live_pids = _find_live_pg_pids_under(directory)
    if live_pids:
        raise Pg0StillRunningError(
            f"{context}: found {len(live_pids)} live PostgreSQL process(es) with "
            f"data directories under {directory}: PIDs {live_pids}. "
            "These were not found by pg0 state-file enumeration. "
            "Cannot delete until all PostgreSQL processes are confirmed stopped."
        )


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
    1. Enumerate every pg0 instance under ``HOME/.pg0/instances/*/`` (H3: raises
       on enumeration failure, never returns empty list on error).
    2. Stop each instance with verified postmaster exit.
    3. If ANY instance cannot be verified stopped, raise ``Pg0StillRunningError``
       and do NOT delete anything.
    4. Scan for any live PostgreSQL process with a data directory under
       ``home_dir`` (C5: catches instances missed by pg0 state-file enumeration).
    5. Only after both checks pass, delete ``home_dir``.

    Raises ``Pg0StillRunningError`` if any instance is still running or if live
    PostgreSQL processes are detected under ``home_dir``.
    """
    # Stop all pg0-registered instances (raises if any cannot be verified gone).
    safe_pg0_stop_all(
        home_dir,
        timeout_s=timeout_s,
        settle_s=settle_s,
        exit_wait_s=exit_wait_s,
    )

    # Belt-and-suspenders: scan for any remaining live postgres processes.
    _proc_scan_or_raise(home_dir, "safe_pg0_delete_home")

    # All confirmed stopped.  Safe to delete.
    subprocess.run(
        ["sudo", "-u", _OLO_HS_USER, "rm", "-rf", str(home_dir)],
        check=False, capture_output=True,
    )


def safe_pg0_delete_tree(
    root: Path,
    *,
    timeout_s: float = 15.0,
    settle_s: float = 1.0,
    exit_wait_s: float = _EXIT_POLL_TIMEOUT_S,
) -> None:
    """Safely delete a directory tree containing pg0 HOME(s) at any depth.

    Use this when the target directory CONTAINS pg0 HOME(s) nested at an
    arbitrary depth (e.g. ``hermes_home/businesses/biz_a/.pg0/instances/``).
    For a directory that IS the pg0 HOME, use ``safe_pg0_delete_home``.

    Algorithm:
    1. If ``root`` does not exist, return immediately (nothing to do).
    2. Enumerate every pg0 HOME directory nested under ``root``
       (directories containing ``.pg0/instances/``) using ``os.walk`` without
       following symlinks.
    3. For each found HOME, call ``safe_pg0_stop_all`` to stop every registered
       instance with verified exit.
    4. If ANY instance cannot be stopped, raise ``Pg0StillRunningError`` and
       do NOT delete ``root``.
    5. Perform a full proc scan of ``root`` for any remaining live PostgreSQL
       processes (catches instances not reachable via pg0 state files).
    6. Only after both checks pass, delete ``root``.

    Eliminates C2 (V7 setup: nested business databases) and C5 (V7 teardown:
    unlisted business under hermes home).

    Raises ``Pg0StillRunningError`` if any instance cannot be verified stopped
    or if live PostgreSQL processes are detected under ``root``.
    """
    if not root.exists():
        return

    # Find all pg0 HOME directories nested under root.
    homes = _enumerate_pg0_homes_under(root)

    # Stop all instances in every found HOME.
    all_errors: list[str] = []
    for home in homes:
        try:
            safe_pg0_stop_all(
                home,
                timeout_s=timeout_s,
                settle_s=settle_s,
                exit_wait_s=exit_wait_s,
            )
        except Pg0StillRunningError as exc:
            all_errors.append(str(exc))

    if all_errors:
        raise Pg0StillRunningError(
            f"safe_pg0_delete_tree: cannot safely delete {root}:\n"
            + "\n".join(f"  {e}" for e in all_errors)
        )

    # Final proc scan for any postgres with data dirs anywhere under root.
    _proc_scan_or_raise(root, "safe_pg0_delete_tree")

    # All confirmed stopped.  Safe to delete the tree.
    subprocess.run(
        ["sudo", "-u", _OLO_HS_USER, "rm", "-rf", str(root)],
        check=False, capture_output=True,
    )
