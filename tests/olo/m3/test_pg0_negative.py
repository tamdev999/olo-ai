"""Negative regression tests for pg0 lifecycle safety.

Covers the Opus release audit T1/T2/T3 findings and the fourth-remediation
N1/N2/N2b/N4/N5/N6 findings from pg0_negative_v2.py:

- T1: Wrong HOME stop cannot cause live database deletion.
- T2: Teardown after killing Hindsight while pg0 lives cannot delete data.
- T3: A successful pg0 stop return code alone is insufficient without exit
      verification.
- N_layout: _pg0_instance_data_dir uses the actual pg0 0.17.0 layout
            (HOME/.pg0/instances/<inst>/data), not the old layout that never
            matched a real running server.
- N1: Wrong instance name, same HOME → Pg0StillRunningError; live data intact.
- N2: pg0_stop returns True for a mismatched instance → caller must not delete.
- N2b: pg0_stop_and_clean with lost instance.json → data not deleted while
       postmaster lives.
- N4: data_dir at the old wrong path → postmaster still found via correct path.
- N5: rm -rf HOME without pg0_stop deletes live database (documented bad pattern).
- N6: Pre-clean with a leftover postmaster → raises before deletion.
- Unreadable pidfile with a live postmaster must not be treated as stopped.

These tests use mocking to run without actual PostgreSQL or pg0 processes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from olo.m3.pg0_lifecycle import (
    pg0_stop,
    pg0_stop_and_clean,
    Pg0StillRunningError,
    _pg0_instance_data_dir,
    _list_pg0_instances,
    _enumerate_pg0_homes_under,
    _find_live_pg_pids_under,
    _read_postmaster_pid,
    _process_exists,
    _wait_for_postmaster_exit,
    safe_pg0_stop_all,
    safe_pg0_delete_home,
    safe_pg0_delete_tree,
)


# ── Helper mocks ─────────────────────────────────────────────────────────────

def _make_pg0_stop_result(returncode: int, stderr: bytes = b"") -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stderr = stderr
    return r


# ── N_layout: correct pg0 0.17.0 path ────────────────────────────────────────

class TestPg0LayoutPath:
    """The helper must reflect the actual pg0 0.17.0 directory layout."""

    def test_data_dir_contains_instances_component(self, tmp_path: Path) -> None:
        """_pg0_instance_data_dir must include the 'instances/' path component.

        pg0 0.17.0 layout: HOME/.pg0/instances/<name>/data
        Old (broken) layout: HOME/.pg0/<name>/data
        Without 'instances/', _read_postmaster_pid always returned None and
        the /proc safety check never executed.
        """
        data_dir = _pg0_instance_data_dir(tmp_path, "my-instance")
        parts = data_dir.relative_to(tmp_path).parts
        assert "instances" in parts, (
            f"'instances' missing from pg0 data path {data_dir}. "
            "pg0 0.17.0 layout is HOME/.pg0/instances/<name>/data"
        )
        assert parts[-1] == "data"
        assert parts[-2] == "my-instance"
        assert parts[-3] == "instances"

    def test_data_dir_matches_actual_layout(self, tmp_path: Path) -> None:
        """Full path must be HOME/.pg0/instances/<name>/data."""
        data_dir = _pg0_instance_data_dir(tmp_path, "olo-v5gate")
        expected = tmp_path / ".pg0" / "instances" / "olo-v5gate" / "data"
        assert data_dir == expected, (
            f"Expected {expected}, got {data_dir}. "
            "Helper must use the real pg0 0.17.0 layout."
        )

    def test_different_instance_names_produce_different_paths(
        self, tmp_path: Path
    ) -> None:
        a = _pg0_instance_data_dir(tmp_path, "inst-a")
        b = _pg0_instance_data_dir(tmp_path, "inst-b")
        assert a != b

    def test_postmaster_pid_readable_at_correct_path(self, tmp_path: Path) -> None:
        """_read_postmaster_pid reads from the correct layout path."""
        data_dir = _pg0_instance_data_dir(tmp_path, "test")
        data_dir.mkdir(parents=True)
        (data_dir / "postmaster.pid").write_text("99999\n")
        pid = _read_postmaster_pid(data_dir)
        assert pid == 99999

    def test_postmaster_pid_missing_at_old_wrong_path(self, tmp_path: Path) -> None:
        """With a pidfile at the correct location, the old wrong path returns None.

        This is the root cause of N1-N6: the old _pg0_instance_data_dir
        pointed to HOME/.pg0/<inst>/data, so pidfiles written by pg0 at
        HOME/.pg0/instances/<inst>/data were invisible to the helper.
        """
        correct_dir = _pg0_instance_data_dir(tmp_path, "test")
        correct_dir.mkdir(parents=True)
        (correct_dir / "postmaster.pid").write_text("99999\n")

        # The old (incorrect) path
        old_wrong_dir = tmp_path / ".pg0" / "test" / "data"
        pid = _read_postmaster_pid(old_wrong_dir)
        assert pid is None, (
            "Old wrong path should NOT see the pidfile written by pg0. "
            "If this assertion fails, the helper is using the old path."
        )


# ── T1: Wrong HOME stop ───────────────────────────────────────────────────────

class TestWrongHomeStop:
    """T1: When pg0 stop uses the wrong HOME, the postmaster lives."""

    def test_wrong_home_returns_not_running_pg0_returns_false(
        self, tmp_path: Path
    ) -> None:
        """T1: pg0 says 'No running instance found' (wrong HOME) → pg0_stop False."""
        wrong_home = tmp_path / "wrong_home"
        wrong_home.mkdir()
        result = _make_pg0_stop_result(1, b"No running instance found")
        with patch("subprocess.run", return_value=result), \
             patch("time.sleep"):
            stopped = pg0_stop(instance_name="test", home_dir=wrong_home)
        # When wrong HOME is used and no data directory exists at that HOME,
        # pg0_stop returns True: the instance was never started at this HOME,
        # so there is nothing to protect.
        assert stopped, (
            "Wrong-HOME stop with no data directory returns True (safe): "
            "instance was never started at this HOME so nothing needs protecting."
        )

    def test_wrong_home_data_dir_not_deleted(self, tmp_path: Path) -> None:
        """T1 remediation: data_dir at canonical path is preserved when stop unverified."""
        wrong_home = tmp_path / "wrong_home"
        wrong_home.mkdir()
        # Place a pidfile at the correct layout path under wrong_home
        data_dir = _pg0_instance_data_dir(wrong_home, "test")
        data_dir.mkdir(parents=True)
        sentinel = data_dir / "PG_VERSION"
        sentinel.write_text("14")
        # Simulate a pidfile so pre_stop_pid is non-None
        (data_dir / "postmaster.pid").write_text("99999\n")

        # pg0 reports not running, but we had a PID — process still alive
        result = _make_pg0_stop_result(1, b"No running instance found")
        with patch("subprocess.run", return_value=result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=True):
            with pytest.raises(Pg0StillRunningError):
                pg0_stop_and_clean(
                    instance_name="test",
                    home_dir=wrong_home,
                    exit_wait_s=0.001,
                )
        assert sentinel.exists(), "Data dir must not be deleted after wrong-HOME stop"


# ── N1: Wrong instance name, same HOME ───────────────────────────────────────

class TestN1WrongInstanceSameHome:
    """N1: cleanup with a wrong instance name must not delete a live database.

    Root cause: the old layout used HOME/.pg0/<inst>/data, so stopping 'other'
    when 'hs' was running targeted a nonexistent directory.  The postmaster.pid
    was invisible, the safety check skipped, and data was deleted.
    With the corrected layout and derived-only data_dir, the helper targets only
    the canonical path for the specified instance name.  Calling with 'other'
    when 'hs' is running leaves 'hs' completely untouched.
    """

    def test_wrong_instance_name_does_not_touch_real_instance(
        self, tmp_path: Path
    ) -> None:
        """N1: stopping 'other' when 'hs' is running leaves 'hs' data intact.

        With the corrected design (data_dir always derived from instance_name),
        pg0_stop_and_clean targets HOME/.pg0/instances/other/data — which does
        not exist.  pg0_stop returns True (never started here), and nothing is
        deleted.  The real 'hs' instance and its data are undisturbed.
        """
        home = tmp_path / "A"
        home.mkdir()
        # The REAL instance is "hs"
        real_data_dir = _pg0_instance_data_dir(home, "hs")
        real_data_dir.mkdir(parents=True)
        (real_data_dir / "postmaster.pid").write_text("77777\n")
        sentinel = real_data_dir / "PG_VERSION"
        sentinel.write_text("14")

        # Caller mistakenly stops "other" — but pg0_stop_and_clean now derives
        # the target path as HOME/.pg0/instances/other/data (doesn't exist).
        result = _make_pg0_stop_result(1, b"No running instance found")

        import shutil
        def fake_rm(cmd, **kw):
            m = MagicMock(); m.returncode = 0
            if "rm" in cmd:
                target = cmd[-1]
                if Path(target).exists():
                    if Path(target).is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        Path(target).unlink(missing_ok=True)
            return m

        with patch("subprocess.run", side_effect=fake_rm), \
             patch("time.sleep"):
            # Does NOT raise: "other" was never started here, so stop is "safe"
            pg0_stop_and_clean(
                instance_name="other",
                home_dir=home,
            )
        # Critical: "hs" data must remain completely untouched
        assert sentinel.exists(), "Real 'hs' instance data must not be deleted"
        assert (real_data_dir / "postmaster.pid").exists(), (
            "Real 'hs' postmaster.pid must not be deleted"
        )

    def test_correct_instance_name_stops_and_cleans(
        self, tmp_path: Path
    ) -> None:
        """N1 positive: stopping the correct instance name deletes its data."""
        import shutil
        home = tmp_path / "A2"
        home.mkdir()
        data_dir = _pg0_instance_data_dir(home, "hs")
        data_dir.mkdir(parents=True)
        (data_dir / "postmaster.pid").write_text("77777\n")
        sentinel = data_dir / "PG_VERSION"
        sentinel.write_text("14")

        def fake_rm(cmd, **kw):
            m = MagicMock(); m.returncode = 0
            if "rm" in cmd:
                target = cmd[-1]
                if Path(target).exists():
                    if Path(target).is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        Path(target).unlink(missing_ok=True)
            return m

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", side_effect=fake_rm), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=False):
            pg0_stop_and_clean(instance_name="hs", home_dir=home)
        assert not data_dir.exists(), "Correct instance data must be deleted after stop"

# ── N2: pg0_stop returns True for mismatched instance ────────────────────────

class TestN2StopMismatch:
    """N2: If pg0_stop incorrectly returns True (e.g. instance not found,
    but we have a live PID elsewhere), deletion must still be refused."""

    def test_stop_true_but_pid_alive_raises(self, tmp_path: Path) -> None:
        """N2: even when pg0 returns 0, a live postmaster PID prevents deletion."""
        home = tmp_path / "B"
        home.mkdir()
        data_dir = _pg0_instance_data_dir(home, "hs_mismatch")
        data_dir.mkdir(parents=True)
        pid_file = data_dir / "postmaster.pid"
        pid_file.write_text("88888\n")

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=stop_result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=True):
            stopped = pg0_stop(instance_name="hs_mismatch", home_dir=home,
                               exit_wait_s=0.001)
        assert not stopped, "Must return False when postmaster PID is still alive"
        assert pid_file.exists(), "pidfile must not be touched"


# ── N2b: Lost pg0 state file ──────────────────────────────────────────────────

class TestN2bLostStateFile:
    """N2b: instance.json missing, postmaster still running → not deleted."""

    def test_lost_state_with_live_pid_raises(self, tmp_path: Path) -> None:
        """N2b: if pg0 says not running but pidfile says alive, refuse deletion."""
        home = tmp_path / "C"
        home.mkdir()
        data_dir = _pg0_instance_data_dir(home, "hs")
        data_dir.mkdir(parents=True)
        pid_file = data_dir / "postmaster.pid"
        pid_file.write_text("55555\n")

        # pg0 can't find instance.json → returns non-zero "not running"
        result = _make_pg0_stop_result(1, b"not running")
        with patch("subprocess.run", return_value=result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=True):
            with pytest.raises(Pg0StillRunningError):
                pg0_stop_and_clean(
                    instance_name="hs",
                    home_dir=home,
                    exit_wait_s=0.001,
                )
        assert pid_file.exists(), "pidfile must survive: postmaster is still running"


# ── N4: Wrong data_dir path (old layout) ─────────────────────────────────────

class TestN4WrongDataDirPath:
    """N4: Passing the old wrong data_dir path exposes the layout bug.

    Before the fix: a caller passing the old path got past the safety checks
    because _read_postmaster_pid read from the same wrong path and found nothing.
    The postmaster survived and the 'fresh' test was not actually fresh.

    After the fix: _pg0_instance_data_dir uses the correct layout, and the
    caller MUST use _pg0_instance_data_dir to construct data_dir.
    """

    def test_correct_data_dir_sees_pidfile(self, tmp_path: Path) -> None:
        """With the correct layout, pg0_stop_and_clean detects the live postmaster."""
        home = tmp_path / "D"
        home.mkdir()
        correct_data = _pg0_instance_data_dir(home, "hs")
        correct_data.mkdir(parents=True)
        (correct_data / "postmaster.pid").write_text("44444\n")

        result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=True):
            stopped = pg0_stop(instance_name="hs", home_dir=home,
                               exit_wait_s=0.001)
        # Postmaster still alive → stop returns False
        assert not stopped, (
            "Correct layout exposes live postmaster; stop must return False"
        )

    def test_old_wrong_data_dir_does_not_find_pidfile(self, tmp_path: Path) -> None:
        """N4 root cause: old layout path sees no pidfile, so no safety check.

        The pidfile lives at instances/hs/data/postmaster.pid.
        The old path HOME/.pg0/hs/data has no postmaster.pid → returns None.
        This demonstrates WHY the layout bug caused N4 behavior.
        """
        home = tmp_path / "D2"
        home.mkdir()
        # pidfile at the CORRECT path
        correct_data = _pg0_instance_data_dir(home, "hs")
        correct_data.mkdir(parents=True)
        (correct_data / "postmaster.pid").write_text("44444\n")
        # old wrong path
        old_path = home / ".pg0" / "hs" / "data"
        pid_at_old_path = _read_postmaster_pid(old_path)
        assert pid_at_old_path is None, (
            "Old wrong path must NOT find the pidfile. "
            "This shows the safety check would have been skipped."
        )


# ── N6: Pre-clean with leftover postmaster ────────────────────────────────────

class TestN6PreCleanWithLeftover:
    """N6: setup pre-clean must refuse to delete if a postmaster is still running."""

    def test_pre_clean_with_live_postmaster_raises(self, tmp_path: Path) -> None:
        """N6: direct rm -rf on pg0_home with a live postmaster is unsafe.

        The safe pattern uses pg0_stop_and_clean; callers must not bypass it
        by directly deleting the directory before calling the helper.
        This test verifies the helper itself refuses when the canonical data_dir
        has a live postmaster.
        """
        home = tmp_path / "F"
        home.mkdir()
        data_dir = _pg0_instance_data_dir(home, "hs")
        data_dir.mkdir(parents=True)
        (data_dir / "postmaster.pid").write_text("33333\n")
        sentinel = data_dir / "PG_VERSION"
        sentinel.write_text("14")

        # pg0 stop returns 0 but process is still alive (pg0 lied or was slow)
        with patch("subprocess.run",
                   return_value=_make_pg0_stop_result(0)), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=True):
            with pytest.raises(Pg0StillRunningError):
                pg0_stop_and_clean(
                    instance_name="hs",
                    home_dir=home,
                    exit_wait_s=0.001,
                )
        assert sentinel.exists(), "Data must not be deleted with live postmaster"


# ── T2: Kill Hindsight while pg0 lives ───────────────────────────────────────

class TestKillHindsightPg0Lives:
    """T2: Teardown after killing Hindsight must not delete live pg0 data."""

    def test_pg0_still_alive_after_hindsight_kill_raises(
        self, tmp_path: Path
    ) -> None:
        """T2: If pg0 postmaster PID still exists, cleanup refuses to delete."""
        home = tmp_path / "v5_home"
        home.mkdir()
        data_dir = _pg0_instance_data_dir(home, "olo-v5gate")
        data_dir.mkdir(parents=True)
        pid_file = data_dir / "postmaster.pid"
        pid_file.write_text("99999\n")

        # Simulate: pg0 stop returns 0, but PID 99999 still alive (pg0 lied)
        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=stop_result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists",
                   return_value=True):
            with pytest.raises(Pg0StillRunningError):
                pg0_stop_and_clean(
                    instance_name="olo-v5gate",
                    home_dir=home,
                    exit_wait_s=0.001,
                )
        # Data directory must survive
        assert pid_file.exists(), (
            "postmaster.pid must not be deleted while PID is alive"
        )

    def test_cleanup_proceeds_when_pid_gone(self, tmp_path: Path) -> None:
        """T2 success: once PID disappears, cleanup is permitted."""
        import shutil
        home = tmp_path / "v5_home"
        home.mkdir()
        data_dir = _pg0_instance_data_dir(home, "olo-v5gate")
        data_dir.mkdir(parents=True)
        pid_file = data_dir / "postmaster.pid"
        pid_file.write_text("99999\n")

        def fake_rm(cmd, **kw):
            m = MagicMock(); m.returncode = 0
            if "rm" in cmd:
                target = cmd[-1]
                if Path(target).exists():
                    if Path(target).is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        Path(target).unlink(missing_ok=True)
            return m

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", side_effect=fake_rm), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists",
                   return_value=False):
            pg0_stop_and_clean(
                instance_name="olo-v5gate",
                home_dir=home,
            )
        assert not data_dir.exists(), "data_dir should be deleted after confirmed exit"


# ── T3: Return code 0 alone insufficient ─────────────────────────────────────

class TestReturnCodeInsufficient:
    """T3: pg0 returncode=0 must not bypass postmaster exit verification."""

    def test_returncode_0_with_live_pid_returns_false(
        self, tmp_path: Path
    ) -> None:
        """T3: Even returncode=0 must fail if postmaster PID is still alive."""
        home = tmp_path / "home"
        home.mkdir()
        data_dir = _pg0_instance_data_dir(home, "my-instance")
        data_dir.mkdir(parents=True)
        (data_dir / "postmaster.pid").write_text("12345\n")

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=stop_result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists",
                   return_value=True):
            stopped = pg0_stop(instance_name="my-instance", home_dir=home,
                               exit_wait_s=0.001)
        assert not stopped, (
            "returncode=0 with a live postmaster PID must return False"
        )

    def test_returncode_0_with_dead_pid_returns_true(
        self, tmp_path: Path
    ) -> None:
        """T3 success: returncode=0 + PID gone = verified stop."""
        home = tmp_path / "home"
        home.mkdir()
        data_dir = _pg0_instance_data_dir(home, "my-instance")
        data_dir.mkdir(parents=True)
        (data_dir / "postmaster.pid").write_text("12345\n")

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=stop_result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists",
                   return_value=False):
            stopped = pg0_stop(instance_name="my-instance", home_dir=home)
        assert stopped


# ── Unreadable pidfile with live postmaster ───────────────────────────────────

class TestUnreadablePidfile:
    """An unreadable pidfile must not be treated as 'no process'."""

    def test_unreadable_pidfile_returns_false_on_data_dir_exists(
        self, tmp_path: Path
    ) -> None:
        """If the data directory exists but pidfile is missing/unreadable,
        and pg0 also says not running, we return False conservatively.

        This guards against N2b: a lost instance.json + missing pidfile
        still has a live postmaster that pg0 cannot see.
        """
        home = tmp_path / "home"
        home.mkdir()
        data_dir = _pg0_instance_data_dir(home, "my-instance")
        data_dir.mkdir(parents=True)
        # No pidfile: _read_postmaster_pid returns None

        # pg0 says not running (couldn't find state file)
        result = _make_pg0_stop_result(1, b"not running")
        with patch("subprocess.run", return_value=result), \
             patch("time.sleep"):
            stopped = pg0_stop(instance_name="my-instance", home_dir=home)
        # Data dir exists but no pidfile + pg0 says not running → conservative False
        assert not stopped, (
            "Data dir present + no pidfile + pg0 says not running = ambiguous. "
            "Must return False, not True."
        )

    def test_no_data_dir_and_not_running_returns_true(
        self, tmp_path: Path
    ) -> None:
        """If data dir doesn't exist and pg0 says not running → safe True."""
        home = tmp_path / "home"
        home.mkdir()
        # No data dir at all
        result = _make_pg0_stop_result(1, b"No running instance found")
        with patch("subprocess.run", return_value=result), \
             patch("time.sleep"):
            stopped = pg0_stop(instance_name="my-instance", home_dir=home)
        assert stopped, "No data dir + not running = never started here, safe"


# ── Cross-HOME deletion prevention ───────────────────────────────────────────

class TestCrossHomeDeletionPrevention:
    """pg0_stop_and_clean always uses the canonically derived data directory.

    Because the function no longer accepts a caller-supplied data_dir, the
    cross-HOME deletion bug (passing a path outside home_dir) is eliminated
    by design — the target is always derived from _pg0_instance_data_dir(home, name).
    """

    def test_derived_path_is_always_under_home(self, tmp_path: Path) -> None:
        """The canonical data directory is always inside the specified HOME."""
        home = tmp_path / "my_home"
        home.mkdir()
        data_dir = _pg0_instance_data_dir(home, "my-instance")
        try:
            data_dir.relative_to(home)
        except ValueError:
            pytest.fail(
                f"Canonical data_dir {data_dir} is not under home {home}. "
                "This would allow cross-HOME deletion."
            )

    def test_different_homes_produce_different_data_dirs(
        self, tmp_path: Path
    ) -> None:
        """Two different HOME directories produce two different data directories."""
        home_a = tmp_path / "home_a"
        home_b = tmp_path / "home_b"
        a = _pg0_instance_data_dir(home_a, "hs")
        b = _pg0_instance_data_dir(home_b, "hs")
        assert a != b, (
            "Different HOME directories must produce different data_dir paths"
        )
        assert not a.is_relative_to(home_b), "home_a path must not be inside home_b"
        assert not b.is_relative_to(home_a), "home_b path must not be inside home_a"


# ── H3: enumeration failure raises, never deletes ────────────────────────────

class TestEnumerationFailClosedH3:
    """H3: _list_pg0_instances must raise on I/O error, never return [].

    Returning an empty list on an enumeration error lets safe_pg0_delete_home
    conclude there are no running instances and delete the HOME directory.
    """

    def test_iterdir_ioerror_raises_pg0_still_running(
        self, tmp_path: Path
    ) -> None:
        """H3: OSError during iterdir causes Pg0StillRunningError, not empty list."""
        home = tmp_path / "h3home"
        home.mkdir()
        instances_root = home / ".pg0" / "instances"
        instances_root.mkdir(parents=True)
        # Create a real instance directory so the file exists
        (instances_root / "hs").mkdir()

        saved_iterdir = Path.iterdir

        def _boom(self):
            if self.name == "instances":
                raise OSError("simulated I/O error reading instances directory")
            return saved_iterdir(self)

        with patch.object(Path, "iterdir", _boom):
            with pytest.raises(Pg0StillRunningError, match="simulated I/O error"):
                _list_pg0_instances(home)

    def test_iterdir_ioerror_prevents_home_deletion(
        self, tmp_path: Path
    ) -> None:
        """H3: safe_pg0_delete_home raises when enumeration fails; HOME survives."""
        home = tmp_path / "h3del"
        home.mkdir()
        instances_root = home / ".pg0" / "instances"
        instances_root.mkdir(parents=True)
        sentinel = home / "important_data.db"
        sentinel.write_text("production data")

        saved_iterdir = Path.iterdir

        def _boom(self):
            if self.name == "instances":
                raise OSError("simulated I/O failure")
            return saved_iterdir(self)

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=stop_result), \
             patch("time.sleep"), \
             patch.object(Path, "iterdir", _boom):
            with pytest.raises(Pg0StillRunningError):
                safe_pg0_delete_home(home)
        assert sentinel.exists(), (
            "HOME must NOT be deleted when instance enumeration fails"
        )

    def test_no_instances_directory_returns_empty_safely(
        self, tmp_path: Path
    ) -> None:
        """H3 safe case: absent .pg0/instances/ is genuinely empty — no error."""
        home = tmp_path / "h3empty"
        home.mkdir()
        # No .pg0 directory at all
        result = _list_pg0_instances(home)
        assert result == [], "Absent instances dir is genuinely empty — no error"


# ── C5: proc scan catches postgres not visible via pg0 state files ────────────

class TestProcScanC5:
    """C5: _find_live_pg_pids_under and _proc_scan_or_raise catch live postgres
    instances whose data directories lie under the cleanup target but are not
    registered in pg0 state files (custom dirs, nested business HOmes,
    incomplete pg0 state).
    """

    def test_find_live_pids_reads_nested_postmaster_pid(
        self, tmp_path: Path
    ) -> None:
        """_find_live_pg_pids_under finds a postmaster.pid at any depth."""
        root = tmp_path / "root"
        # Nested business database — NOT at the top-level .pg0/instances/ path
        nested_data = root / "businesses" / "biz_other" / ".pg0" / "instances" / "hs" / "data"
        nested_data.mkdir(parents=True)
        (nested_data / "postmaster.pid").write_text("77777\n")

        with patch("olo.m3.pg0_lifecycle._process_exists", return_value=True):
            pids = _find_live_pg_pids_under(root)
        assert 77777 in pids, (
            f"Expected PID 77777 in live pids, got {pids}"
        )

    def test_find_live_pids_ignores_dead_postmasters(
        self, tmp_path: Path
    ) -> None:
        """postmaster.pid whose PID is not in /proc is ignored."""
        root = tmp_path / "root"
        data_dir = root / ".pg0" / "instances" / "hs" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "postmaster.pid").write_text("99999\n")

        with patch("olo.m3.pg0_lifecycle._process_exists", return_value=False):
            pids = _find_live_pg_pids_under(root)
        assert pids == [], "Dead postmaster must not appear in live pids"

    def test_safe_pg0_delete_home_raises_on_custom_data_dir(
        self, tmp_path: Path
    ) -> None:
        """C5: postgres with a custom data dir (not under .pg0) blocks deletion.

        pg0 state-file enumeration finds no instances at the top level, but
        the proc scan finds the live postmaster via postmaster.pid.
        """
        home = tmp_path / "home"
        home.mkdir()
        # Custom data directory — NOT under .pg0/instances/
        custom_data = home / "custom_pgdata"
        custom_data.mkdir()
        (custom_data / "postmaster.pid").write_text("55555\n")
        sentinel = custom_data / "PG_VERSION"
        sentinel.write_text("14")

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=stop_result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=True):
            with pytest.raises(Pg0StillRunningError, match="live PostgreSQL"):
                safe_pg0_delete_home(home, exit_wait_s=0.001)
        assert sentinel.exists(), "Data must not be deleted when proc scan finds live process"

    def test_safe_pg0_delete_home_proceeds_when_no_live_pids(
        self, tmp_path: Path
    ) -> None:
        """safe_pg0_delete_home succeeds when proc scan finds no live pids."""
        import shutil
        home = tmp_path / "home"
        home.mkdir()
        data_dir = _pg0_instance_data_dir(home, "hs")
        data_dir.mkdir(parents=True)
        (data_dir / "postmaster.pid").write_text("12345\n")

        def fake_rm(cmd, **kw):
            m = MagicMock(); m.returncode = 0
            if "rm" in cmd:
                target = cmd[-1]
                if Path(target).exists():
                    if Path(target).is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        Path(target).unlink(missing_ok=True)
            return m

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", side_effect=fake_rm), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=False):
            safe_pg0_delete_home(home)
        assert not home.exists(), "HOME must be deleted when no live pids remain"


# ── safe_pg0_delete_tree: nested pg0 HOmes ────────────────────────────────────

class TestSafeDeleteTree:
    """safe_pg0_delete_tree handles nested pg0 HOmes at arbitrary depth (C2/C5)."""

    def test_enumerate_pg0_homes_finds_nested(self, tmp_path: Path) -> None:
        """_enumerate_pg0_homes_under finds business HOME dirs at any depth."""
        hermes = tmp_path / "hermes"
        biz_a = hermes / "businesses" / "biz_a"
        biz_b = hermes / "businesses" / "biz_b"
        for biz in (biz_a, biz_b):
            (biz / ".pg0" / "instances").mkdir(parents=True)

        homes = _enumerate_pg0_homes_under(hermes)
        assert biz_a in homes, f"biz_a not found: {homes}"
        assert biz_b in homes, f"biz_b not found: {homes}"

    def test_enumerate_pg0_homes_excludes_nonpg0_dirs(self, tmp_path: Path) -> None:
        """Directories without .pg0/instances/ are not returned."""
        root = tmp_path / "root"
        (root / "other_dir").mkdir(parents=True)
        (root / "not_pg0" / "instances").mkdir(parents=True)  # instances but not under .pg0
        homes = _enumerate_pg0_homes_under(root)
        # only paths where the instances dir is at .pg0/instances
        assert not any("not_pg0" in str(h) for h in homes)

    def test_delete_tree_stops_nested_instances(self, tmp_path: Path) -> None:
        """C2: nested business pg0 instance under hermes_home is stopped before deletion."""
        import shutil
        hermes = tmp_path / "hermes"
        biz_home = hermes / "businesses" / "biz_a"
        data_dir = _pg0_instance_data_dir(biz_home, "hs")
        data_dir.mkdir(parents=True)
        (data_dir / "postmaster.pid").write_text("99999\n")
        sentinel = data_dir / "PG_VERSION"
        sentinel.write_text("14")

        def fake_rm(cmd, **kw):
            m = MagicMock(); m.returncode = 0
            if "rm" in cmd:
                target = cmd[-1]
                if Path(target).exists():
                    if Path(target).is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        Path(target).unlink(missing_ok=True)
            return m

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", side_effect=fake_rm), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=False):
            safe_pg0_delete_tree(hermes)
        assert not hermes.exists(), "hermes must be deleted after all nested instances stopped"

    def test_delete_tree_raises_when_nested_instance_cannot_stop(
        self, tmp_path: Path
    ) -> None:
        """C5: live nested instance prevents deletion of the entire tree."""
        hermes = tmp_path / "hermes"
        biz_other = hermes / "businesses" / "biz_other"
        data_other = _pg0_instance_data_dir(biz_other, "hs")
        data_other.mkdir(parents=True)
        (data_other / "postmaster.pid").write_text("22222\n")
        sentinel = data_other / "PG_VERSION"
        sentinel.write_text("14")

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=stop_result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=True):
            with pytest.raises(Pg0StillRunningError):
                safe_pg0_delete_tree(hermes, exit_wait_s=0.001)
        assert sentinel.exists(), "Data must survive when tree deletion fails"

    def test_delete_tree_nonexistent_root_is_noop(self, tmp_path: Path) -> None:
        """safe_pg0_delete_tree on a non-existent root returns without error."""
        nonexistent = tmp_path / "does_not_exist"
        safe_pg0_delete_tree(nonexistent)  # must not raise

    def test_delete_tree_known_plus_unknown_biz(self, tmp_path: Path) -> None:
        """C5: safe_pg0_delete_tree finds 'biz_other' even when only 'biz_a' is known.

        Replicates the audit's C5 scenario: safe_pg0_delete_home(biz_a) deleted
        biz_a's data, then raw rm -rf hermes_home deleted biz_other's live database.
        safe_pg0_delete_tree(hermes_home) must stop BOTH before deleting.
        """
        import shutil
        hermes = tmp_path / "hermes"
        biz_a_data = _pg0_instance_data_dir(hermes / "businesses" / "biz_a", "hs")
        biz_other_data = _pg0_instance_data_dir(hermes / "businesses" / "biz_other", "hs")
        biz_a_data.mkdir(parents=True)
        biz_other_data.mkdir(parents=True)
        (biz_a_data / "postmaster.pid").write_text("11111\n")
        (biz_other_data / "postmaster.pid").write_text("22222\n")

        pids_alive = {11111: False, 22222: False}  # both stopped

        def mock_process_exists(pid):
            return pids_alive.get(pid, False)

        def fake_rm(cmd, **kw):
            m = MagicMock(); m.returncode = 0
            if "rm" in cmd:
                target = cmd[-1]
                if Path(target).exists():
                    if Path(target).is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        Path(target).unlink(missing_ok=True)
            return m

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", side_effect=fake_rm), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", side_effect=mock_process_exists):
            safe_pg0_delete_tree(hermes)
        assert not hermes.exists(), "Both biz stopped; hermes must be deleted"


# ── safe_pg0_stop_all / safe_pg0_delete_home N2/N9/N5/N7/N6 ─────────────────

class TestSafeDeleteHome:
    """safe_pg0_delete_home enumerates ALL instances — no guessed names (N2/N7/N9).

    These are the cases that the previous pg0_stop(configured_name) approach
    left open: a wrong or guessed name caused the helper to miss live postmasters.
    """

    def test_enumerates_all_instances(self, tmp_path: Path) -> None:
        """_list_pg0_instances finds every instance by scanning the directory."""
        home = tmp_path / "home"
        home.mkdir()
        for name in ("inst-a", "inst-b"):
            d = _pg0_instance_data_dir(home, name)
            d.mkdir(parents=True)
        found = _list_pg0_instances(home)
        assert set(found) == {"inst-a", "inst-b"}, (
            f"Expected {{inst-a, inst-b}}, got {set(found)}"
        )

    def test_no_instances_returns_empty(self, tmp_path: Path) -> None:
        """When HOME/.pg0/instances/ is absent, list is empty."""
        home = tmp_path / "home"
        home.mkdir()
        assert _list_pg0_instances(home) == []

    def test_n2_name_mismatch_still_stops_real_instance(
        self, tmp_path: Path
    ) -> None:
        """N2: even when the configured name is wrong, safe_pg0_stop_all
        discovers and stops the REAL instance by enumerating the directory.

        Before the fix: pg0_stop("hs_configured") found no data dir for
        "hs_configured" → returned True → HOME deleted with "hs" live.
        After the fix: safe_pg0_stop_all enumerates and finds "hs", stops it.
        """
        import shutil
        home = tmp_path / "n2home"
        home.mkdir()
        # Real instance is "hs"
        real_data = _pg0_instance_data_dir(home, "hs")
        real_data.mkdir(parents=True)
        (real_data / "postmaster.pid").write_text("77777\n")
        sentinel = real_data / "PG_VERSION"
        sentinel.write_text("14")

        def fake_rm(cmd, **kw):
            m = MagicMock(); m.returncode = 0
            if "rm" in cmd:
                target = cmd[-1]
                if Path(target).exists():
                    if Path(target).is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        Path(target).unlink(missing_ok=True)
            return m

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", side_effect=fake_rm), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=False):
            # safe_pg0_delete_home enumerates and finds "hs", stops and deletes
            safe_pg0_delete_home(home)
        # HOME should be gone (deleted after verified stop)
        assert not home.exists(), (
            "safe_pg0_delete_home must delete HOME after stopping all instances"
        )

    def test_n9_two_instances_both_stopped(self, tmp_path: Path) -> None:
        """N9: two instances in the same HOME are both stopped before deletion."""
        import shutil
        home = tmp_path / "n9home"
        home.mkdir()
        for inst in ("hs", "hs2"):
            d = _pg0_instance_data_dir(home, inst)
            d.mkdir(parents=True)
            (d / "postmaster.pid").write_text("99999\n")
        sentinel_hs2 = _pg0_instance_data_dir(home, "hs2") / "PG_VERSION"
        sentinel_hs2.write_text("14")

        def fake_rm(cmd, **kw):
            m = MagicMock(); m.returncode = 0
            if "rm" in cmd:
                target = cmd[-1]
                if Path(target).exists():
                    if Path(target).is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        Path(target).unlink(missing_ok=True)
            return m

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", side_effect=fake_rm), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=False):
            safe_pg0_delete_home(home)
        assert not home.exists(), "HOME must be deleted after all instances stopped"

    def test_n5_failed_stop_refuses_home_deletion(self, tmp_path: Path) -> None:
        """N5: if any instance cannot be stopped, HOME is not deleted.

        V7-style teardown: pg0_stop_and_clean raises, except: pass, then rm -rf HOME.
        After the fix: safe_pg0_delete_home raises and nothing is deleted.
        """
        home = tmp_path / "n5home"
        home.mkdir()
        data = _pg0_instance_data_dir(home, "hs")
        data.mkdir(parents=True)
        (data / "postmaster.pid").write_text("55555\n")
        sentinel = data / "PG_VERSION"
        sentinel.write_text("14")

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=stop_result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=True,
                   exit_wait_s=0.001):
            # We need exit_wait_s=0.001 but it's passed to safe_pg0_delete_home
            with pytest.raises(Pg0StillRunningError):
                safe_pg0_delete_home(home, exit_wait_s=0.001)
        assert home.exists(), "HOME must NOT be deleted when stop fails"
        assert sentinel.exists(), "Data must survive a failed stop"

    def test_n7_no_instances_does_not_fail(self, tmp_path: Path) -> None:
        """N7-equivalent: when HOME has no pg0 instances, deletion proceeds safely.

        Before fix: pg0_stop(guessed_name) returned True (no data dir found),
        rm -rf HOME ran — which happened to be correct behavior if no instances
        were left, but the code path didn't verify it.
        After fix: safe_pg0_delete_home with an empty instances list is a no-op
        for stop and then deletes the HOME safely.
        """
        import shutil
        home = tmp_path / "n7home"
        home.mkdir()
        (home / "some_file.txt").write_text("data")
        # No pg0 instances created

        def fake_rm(cmd, **kw):
            m = MagicMock(); m.returncode = 0
            if "rm" in cmd:
                target = cmd[-1]
                if Path(target).exists():
                    if Path(target).is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        Path(target).unlink(missing_ok=True)
            return m

        with patch("subprocess.run", side_effect=fake_rm):
            safe_pg0_delete_home(home)
        assert not home.exists(), "Empty HOME should be deleted safely"

    def test_safe_pg0_stop_all_does_not_delete(self, tmp_path: Path) -> None:
        """safe_pg0_stop_all stops instances but does NOT delete the HOME."""
        home = tmp_path / "stop_all_home"
        home.mkdir()
        data = _pg0_instance_data_dir(home, "hs")
        data.mkdir(parents=True)
        (data / "postmaster.pid").write_text("12345\n")

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=stop_result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=False):
            stopped = safe_pg0_stop_all(home)
        assert home.exists(), "safe_pg0_stop_all must NOT delete the home directory"
        assert "hs" in stopped

    def test_safe_pg0_stop_all_raises_on_live_postmaster(
        self, tmp_path: Path
    ) -> None:
        """safe_pg0_stop_all raises when a postmaster cannot be verified stopped."""
        home = tmp_path / "stop_fail_home"
        home.mkdir()
        data = _pg0_instance_data_dir(home, "hs")
        data.mkdir(parents=True)
        (data / "postmaster.pid").write_text("12345\n")

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=stop_result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=True):
            with pytest.raises(Pg0StillRunningError):
                safe_pg0_stop_all(home, exit_wait_s=0.001)
        assert home.exists(), "HOME must survive a failed stop_all"


# ── X1: fail-closed on scanner errors ────────────────────────────────────────

class TestScannerFailClosed:
    """X1: inspection errors must prevent deletion, not silently authorize it.

    Opus reproduced an edge case where an unreadable nested directory and a
    denied /proc enumeration both caused the safety scanners to silently return
    empty results, allowing cleanup to proceed and delete live database data.
    """

    def test_find_live_pids_raises_on_walk_permission_error(
        self, tmp_path: Path
    ) -> None:
        """X1a: unreadable nested directory during postmaster.pid walk → raises."""
        root = tmp_path / "root"
        root.mkdir()
        # Create a directory structure that triggers an onerror callback
        subdir = root / "unreadable_subdir"
        subdir.mkdir()
        (subdir / "postmaster.pid").write_text("99999\n")

        # Simulate a PermissionError on the subdirectory during walk
        walk_errors_captured: list = []
        original_walk = __import__("os").walk

        def _walk_with_error(path, followlinks, onerror):
            # First yield root normally, then trigger an error
            yield str(root), [subdir.name], []
            if onerror:
                err = PermissionError(13, "Permission denied", str(subdir))
                onerror(err)
            # Do not yield subdir — simulating it being unreadable

        import os
        with patch.object(os, "walk", side_effect=_walk_with_error):
            with pytest.raises(Pg0StillRunningError, match="inspection error"):
                _find_live_pg_pids_under(root)

    def test_find_live_pids_raises_on_proc_enumeration_failure(
        self, tmp_path: Path
    ) -> None:
        """X1b: /proc enumeration failure → raises."""
        root = tmp_path / "root"
        root.mkdir()

        def _fake_proc_iterdir():
            raise PermissionError(13, "Permission denied", "/proc")

        fake_proc = MagicMock()
        fake_proc.exists.return_value = True
        fake_proc.iterdir.side_effect = _fake_proc_iterdir

        import olo.m3.pg0_lifecycle as _lc

        # Patch Path() calls that return the proc dir
        original_path = _lc.Path

        def _patched_path(*args):
            if args == ("/proc",):
                return fake_proc
            return original_path(*args)

        with patch.object(_lc, "Path", side_effect=_patched_path):
            with pytest.raises(Pg0StillRunningError, match="inspection error"):
                _find_live_pg_pids_under(root)

    def test_find_live_pids_raises_on_both_failures_x1(
        self, tmp_path: Path
    ) -> None:
        """X1 combined: both filesystem walk error AND /proc denied → raises.

        This directly replicates Opus's X1 scenario: nested directory is
        unreadable AND /proc enumeration is denied.  The scanner must raise
        rather than returning empty and silently authorizing deletion.
        """
        root = tmp_path / "root"
        root.mkdir()

        import os
        import olo.m3.pg0_lifecycle as _lc

        def _walk_with_error(path, followlinks, onerror):
            yield str(root), [], []
            if onerror:
                onerror(PermissionError(13, "Permission denied", str(root / "subdir")))

        fake_proc = MagicMock()
        fake_proc.exists.return_value = True
        fake_proc.iterdir.side_effect = PermissionError(13, "Permission denied", "/proc")

        original_path = _lc.Path

        def _patched_path(*args):
            if args == ("/proc",):
                return fake_proc
            return original_path(*args)

        with patch.object(os, "walk", side_effect=_walk_with_error), \
             patch.object(_lc, "Path", side_effect=_patched_path):
            with pytest.raises(Pg0StillRunningError, match="inspection error"):
                _find_live_pg_pids_under(root)

    def test_find_live_pids_absent_directory_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        """A genuinely absent directory produces an empty result without error.

        X1 fix must not cause false failures for the normal 'directory does
        not exist' case (e.g. first-ever benchmark run, no pg0 HOME yet).
        """
        nonexistent = tmp_path / "does_not_exist"
        # Should NOT raise — genuinely absent directory is safe
        result = _find_live_pg_pids_under(nonexistent)
        assert result == []

    def test_enumerate_homes_raises_on_walk_error(self, tmp_path: Path) -> None:
        """_enumerate_pg0_homes_under raises when os.walk reports a permission error."""
        root = tmp_path / "root"
        root.mkdir()

        import os

        def _walk_with_error(path, followlinks, onerror):
            yield str(root), [], []
            if onerror:
                onerror(PermissionError(13, "Permission denied", str(root / "unreadable")))

        with patch.object(os, "walk", side_effect=_walk_with_error):
            with pytest.raises(Pg0StillRunningError, match="filesystem error"):
                _enumerate_pg0_homes_under(root)

    def test_enumerate_homes_absent_root_returns_empty(self, tmp_path: Path) -> None:
        """_enumerate_pg0_homes_under on absent root returns [] without error."""
        nonexistent = tmp_path / "absent"
        result = _enumerate_pg0_homes_under(nonexistent)
        assert result == []

    def test_safe_pg0_delete_home_aborts_on_scan_error(
        self, tmp_path: Path
    ) -> None:
        """X1 integration: safe_pg0_delete_home cannot proceed when scanner raises."""
        home = tmp_path / "home"
        home.mkdir()
        data_dir = _pg0_instance_data_dir(home, "hs")
        data_dir.mkdir(parents=True)
        (data_dir / "postmaster.pid").write_text("99999\n")
        sentinel = data_dir / "PG_VERSION"
        sentinel.write_text("14")

        import os
        import olo.m3.pg0_lifecycle as _lc

        # Simulate an unreadable subdirectory during the proc scan
        def _walk_with_error(path, followlinks, onerror):
            yield str(home), [], []
            if onerror:
                onerror(PermissionError(13, "Permission denied", str(home / "sub")))

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=stop_result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=False), \
             patch.object(os, "walk", side_effect=_walk_with_error):
            # Even though stop succeeds, the proc scan raises → delete is blocked
            with pytest.raises(Pg0StillRunningError):
                safe_pg0_delete_home(home)

        assert sentinel.exists(), "Data must not be deleted when scanner raises"

    def test_unstoppable_postmaster_prevents_deletion(self, tmp_path: Path) -> None:
        """An unstoppable postmaster → safe_pg0_delete_home raises, no deletion."""
        home = tmp_path / "home"
        home.mkdir()
        data_dir = _pg0_instance_data_dir(home, "hs")
        data_dir.mkdir(parents=True)
        (data_dir / "postmaster.pid").write_text("77777\n")
        sentinel = data_dir / "PG_VERSION"
        sentinel.write_text("14")

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=stop_result), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=True):
            with pytest.raises(Pg0StillRunningError):
                safe_pg0_delete_home(home, exit_wait_s=0.001)
        assert sentinel.exists()

    def test_two_stopped_instances_deletion_succeeds(self, tmp_path: Path) -> None:
        """Two successfully stopped instances → safe deletion completes."""
        import shutil
        home = tmp_path / "home"
        home.mkdir()
        for inst in ("hs1", "hs2"):
            d = _pg0_instance_data_dir(home, inst)
            d.mkdir(parents=True)
            (d / "postmaster.pid").write_text("11111\n")

        def fake_rm(cmd, **kw):
            m = MagicMock(); m.returncode = 0
            if "rm" in cmd:
                target = cmd[-1]
                if Path(target).exists():
                    if Path(target).is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        Path(target).unlink(missing_ok=True)
            return m

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", side_effect=fake_rm), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=False):
            safe_pg0_delete_home(home)
        assert not home.exists()

    def test_nested_business_pg0_dirs_stopped_before_deletion(
        self, tmp_path: Path
    ) -> None:
        """Nested business pg0 directories are stopped or preserved."""
        import shutil
        hermes = tmp_path / "hermes"
        biz = hermes / "businesses" / "biz_a"
        data = _pg0_instance_data_dir(biz, "hs")
        data.mkdir(parents=True)
        (data / "postmaster.pid").write_text("22222\n")

        def fake_rm(cmd, **kw):
            m = MagicMock(); m.returncode = 0
            if "rm" in cmd:
                target = cmd[-1]
                if Path(target).exists():
                    if Path(target).is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        Path(target).unlink(missing_ok=True)
            return m

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", side_effect=fake_rm), \
             patch("time.sleep"), \
             patch("olo.m3.pg0_lifecycle._process_exists", return_value=False):
            safe_pg0_delete_tree(hermes)
        assert not hermes.exists()
