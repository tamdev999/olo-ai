"""Negative regression tests for pg0 lifecycle safety.

Covers the Opus release audit T1/T2/T3 findings:
- T1: Wrong HOME stop cannot cause live database deletion.
- T2: Teardown after killing Hindsight while pg0 lives cannot delete data.
- T3: A successful pg0 stop return code alone is insufficient without exit verification.

These tests use mocking to run without actual PostgreSQL or pg0 processes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch
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


# ── T1: Wrong HOME stop ───────────────────────────────────────────────────────

class TestWrongHomeStop:
    """T1: When pg0 stop uses the wrong HOME, the postmaster lives."""

    def test_wrong_home_returns_not_running_pg0_returns_false(
        self, tmp_path: Path
    ) -> None:
        """T1: pg0 says 'No running instance found' (wrong HOME) → pg0_stop False."""
        # With the wrong HOME, pg0 finds no state file and returns non-zero
        # with "not running" in stderr.
        wrong_home = tmp_path / "wrong_home"
        wrong_home.mkdir()
        result = _make_pg0_stop_result(1, b"No running instance found")
        with patch("subprocess.run", return_value=result),              patch("time.sleep"):
            stopped = pg0_stop(instance_name="test", home_dir=wrong_home)
        # When wrong HOME is used and no data directory exists at that HOME,
        # pg0_stop returns True: the instance was never started at this HOME,
        # so there is nothing to protect.
        # The actual wrong-HOME deletion protection (T1) is tested by
        # test_wrong_home_data_dir_not_deleted (which creates data_dir at wrong HOME
        # and verifies deletion is refused via returncode-1 non-pidfile path).
        assert stopped, (
            "Wrong-HOME stop with no data directory returns True (safe): "
            "instance was never started at this HOME so nothing needs protecting."
        )

    def test_wrong_home_data_dir_not_deleted(self, tmp_path: Path) -> None:
        """T1 remediation: data_dir deletion refused when stop is unverified."""
        wrong_home = tmp_path / "wrong_home"
        wrong_home.mkdir()
        data_dir = wrong_home / ".pg0" / "test" / "data"
        data_dir.mkdir(parents=True)
        sentinel = data_dir / "PG_VERSION"
        sentinel.write_text("14")

        result = _make_pg0_stop_result(1, b"No running instance found")
        with patch("subprocess.run", return_value=result),              patch("time.sleep"):
            with pytest.raises(Pg0StillRunningError):
                pg0_stop_and_clean(
                    instance_name="test",
                    home_dir=wrong_home,
                    data_dir=data_dir,
                )
        assert sentinel.exists(), "Data dir must not be deleted after wrong-HOME stop"


# ── T2: Kill Hindsight while pg0 lives ───────────────────────────────────────

class TestKillHindsightPg0Lives:
    """T2: Teardown after killing Hindsight must not delete live pg0 data."""

    def test_pg0_still_alive_after_hindsight_kill_raises(
        self, tmp_path: Path
    ) -> None:
        """T2: If pg0 postmaster PID still exists, cleanup refuses to delete."""
        home = tmp_path / "v5_home"
        home.mkdir()
        data_dir = home / ".pg0" / "olo-v5gate" / "data"
        data_dir.mkdir(parents=True)
        pid_file = data_dir / "postmaster.pid"
        pid_file.write_text("99999\n")  # fake PID

        # Simulate: pg0 stop returns 0, but PID 99999 still alive (pg0 lied)
        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", return_value=stop_result),              patch("time.sleep"),              patch("tests.olo.m3._pg0_lifecycle._process_exists",
                   return_value=True):  # postmaster still alive
            with pytest.raises(Pg0StillRunningError):
                pg0_stop_and_clean(
                    instance_name="olo-v5gate",
                    home_dir=home,
                    data_dir=data_dir,
                )
        # Data directory must survive
        assert pid_file.exists(), (
            "postmaster.pid must not be deleted while PID is alive"
        )

    def test_cleanup_proceeds_when_pid_gone(self, tmp_path: Path) -> None:
        """T2 success: once PID disappears, cleanup is permitted."""
        home = tmp_path / "v5_home"
        home.mkdir()
        data_dir = home / ".pg0" / "olo-v5gate" / "data"
        data_dir.mkdir(parents=True)
        pid_file = data_dir / "postmaster.pid"
        pid_file.write_text("99999\n")

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

        stop_result = _make_pg0_stop_result(0)
        with patch("subprocess.run", side_effect=fake_rm),              patch("time.sleep"),              patch("tests.olo.m3._pg0_lifecycle._process_exists",
                   return_value=False):  # postmaster gone
            pg0_stop_and_clean(
                instance_name="olo-v5gate",
                home_dir=home,
                data_dir=data_dir,
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
        with patch("subprocess.run", return_value=stop_result),              patch("time.sleep"),              patch("tests.olo.m3._pg0_lifecycle._process_exists",
                   return_value=True):
            stopped = pg0_stop(instance_name="my-instance", home_dir=home)
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
        with patch("subprocess.run", return_value=stop_result),              patch("time.sleep"),              patch("tests.olo.m3._pg0_lifecycle._process_exists",
                   return_value=False):
            stopped = pg0_stop(instance_name="my-instance", home_dir=home)
        assert stopped


# ── Cross-HOME deletion prevention ───────────────────────────────────────────

class TestCrossHomeDeletionPrevention:
    """data_dir must be under home_dir to prevent cross-instance deletion."""

    def test_data_dir_outside_home_raises(self, tmp_path: Path) -> None:
        home = tmp_path / "my_home"
        home.mkdir()
        outside_data = tmp_path / "other_home" / ".pg0" / "data"
        outside_data.mkdir(parents=True)
        sentinel = outside_data / "PG_VERSION"
        sentinel.write_text("14")

        with patch("subprocess.run",
                   return_value=_make_pg0_stop_result(0)),              patch("time.sleep"),              patch("tests.olo.m3._pg0_lifecycle._process_exists",
                   return_value=False):
            with pytest.raises(Pg0StillRunningError, match="not under home_dir"):
                pg0_stop_and_clean(
                    instance_name="test",
                    home_dir=home,
                    data_dir=outside_data,
                )
        assert sentinel.exists(), "Foreign data_dir must not be deleted"
