"""Unit tests for pg0_lifecycle.py safe stop-before-delete logic.

Opus audit §4.A: an unsuccessful stop must NEVER cause data deletion.
Fourth remediation: pg0_stop_and_clean no longer accepts a caller-supplied
data_dir.  It always derives the target from _pg0_instance_data_dir(home, name).
"""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from tests.olo.m3._pg0_lifecycle import (
    pg0_stop, pg0_stop_and_clean, Pg0StillRunningError, _pg0_instance_data_dir,
)


class TestPg0StopBeforeDelete:
    """Prove that pg0_stop_and_clean cannot delete a live database."""

    def test_failed_stop_raises_and_data_survives(self, tmp_path: Path) -> None:
        """When pg0_stop returns False, Pg0StillRunningError prevents deletion."""
        # Create the canonical data directory for this instance
        data_dir = _pg0_instance_data_dir(tmp_path, "test-instance")
        data_dir.mkdir(parents=True)
        sentinel = data_dir / "PG_VERSION"
        sentinel.write_text("14")

        # Simulate stop failure
        with patch("tests.olo.m3._pg0_lifecycle.pg0_stop", return_value=False):
            with pytest.raises(Pg0StillRunningError):
                pg0_stop_and_clean(
                    instance_name="test-instance",
                    home_dir=tmp_path,
                )

        # Data directory must NOT have been deleted
        assert data_dir.exists(), "data_dir was deleted despite failed stop"
        assert sentinel.exists(), "PG_VERSION was deleted despite failed stop"

    def test_failed_stop_cleans_socket_only(self, tmp_path: Path) -> None:
        """Failed stop still raises; socket dir cleanup is a side effect."""
        socket_dir = tmp_path / "pg0_socket"
        socket_dir.mkdir()

        with patch("tests.olo.m3._pg0_lifecycle.pg0_stop", return_value=False):
            # Raises even with socket_dir provided — stop failure is fatal
            with pytest.raises(Pg0StillRunningError):
                pg0_stop_and_clean(
                    instance_name="test-instance",
                    home_dir=tmp_path,
                    socket_dir=socket_dir,
                )

    def test_successful_stop_deletes_canonical_data_dir(self, tmp_path: Path) -> None:
        """When stop succeeds, the canonical data_dir is deleted."""
        # Create the canonical data directory
        data_dir = _pg0_instance_data_dir(tmp_path, "test-instance")
        data_dir.mkdir(parents=True)
        sentinel = data_dir / "PG_VERSION"
        sentinel.write_text("14")

        # Mock sudo rm -rf to actually delete the directory for the test
        def fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 0
            import shutil
            if "rm" in cmd and str(data_dir) in " ".join(str(c) for c in cmd):
                shutil.rmtree(str(data_dir), ignore_errors=True)
            return m

        with patch("tests.olo.m3._pg0_lifecycle.pg0_stop", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            pg0_stop_and_clean(
                instance_name="test-instance",
                home_dir=tmp_path,
            )

        assert not data_dir.exists(), (
            "canonical data_dir should be deleted after successful stop"
        )

    def test_data_dir_always_derived_from_canonical_layout(
        self, tmp_path: Path
    ) -> None:
        """The deleted path is always HOME/.pg0/instances/<name>/data."""
        deleted_paths: list[str] = []

        def fake_run(cmd, **kw):
            m = MagicMock(); m.returncode = 0
            if "rm" in cmd:
                deleted_paths.append(cmd[-1])
            return m

        with patch("tests.olo.m3._pg0_lifecycle.pg0_stop", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            pg0_stop_and_clean(
                instance_name="my-inst",
                home_dir=tmp_path,
            )

        expected = str(_pg0_instance_data_dir(tmp_path, "my-inst"))
        assert expected in deleted_paths, (
            f"Expected {expected!r} to be deleted, got {deleted_paths}. "
            "pg0_stop_and_clean must use canonical layout path."
        )
