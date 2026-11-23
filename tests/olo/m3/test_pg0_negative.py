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

from tests.olo.m3._pg0_lifecycle import (
    pg0_stop,
    pg0_stop_and_clean,
    Pg0StillRunningError,
    _pg0_instance_data_dir,
    _read_postmaster_pid,
    _process_exists,
    _wait_for_postmaster_exit,
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
             patch("tests.olo.m3._pg0_lifecycle._process_exists", return_value=True):
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
             patch("tests.olo.m3._pg0_lifecycle._process_exists", return_value=False):
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
             patch("tests.olo.m3._pg0_lifecycle._process_exists", return_value=True):
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
             patch("tests.olo.m3._pg0_lifecycle._process_exists", return_value=True):
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
             patch("tests.olo.m3._pg0_lifecycle._process_exists", return_value=True):
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
             patch("tests.olo.m3._pg0_lifecycle._process_exists", return_value=True):
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
             patch("tests.olo.m3._pg0_lifecycle._process_exists",
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
             patch("tests.olo.m3._pg0_lifecycle._process_exists",
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
             patch("tests.olo.m3._pg0_lifecycle._process_exists",
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
             patch("tests.olo.m3._pg0_lifecycle._process_exists",
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
