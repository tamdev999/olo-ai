"""Unit tests for pg0_lifecycle.py safe stop-before-delete logic.

Opus audit §4.A: an unsuccessful stop must NEVER cause data deletion.
"""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from tests.olo.m3._pg0_lifecycle import pg0_stop, pg0_stop_and_clean, Pg0StillRunningError


class TestPg0StopBeforeDelete:
    """Prove that pg0_stop_and_clean cannot delete a live database."""

    def test_failed_stop_with_data_dir_raises(self, tmp_path: Path) -> None:
        """When pg0_stop returns False, raising Pg0StillRunningError prevents deletion."""
        data_dir = tmp_path / "pg0_data"
        data_dir.mkdir()
        sentinel = data_dir / "PG_VERSION"
        sentinel.write_text("14")

        # Simulate stop failure
        with patch("tests.olo.m3._pg0_lifecycle.pg0_stop", return_value=False):
            with pytest.raises(Pg0StillRunningError):
                pg0_stop_and_clean(
                    instance_name="test-instance",
                    home_dir=tmp_path,
                    data_dir=data_dir,
                )

        # Data directory must NOT have been deleted
        assert data_dir.exists(), "data_dir was deleted despite failed stop"
        assert sentinel.exists(), "PG_VERSION was deleted despite failed stop"

    def test_failed_stop_without_data_dir_cleans_socket_only(self, tmp_path: Path) -> None:
        """Failed stop + no data_dir request still cleans the socket dir safely."""
        socket_dir = tmp_path / "pg0_socket"
        socket_dir.mkdir()

        with patch("tests.olo.m3._pg0_lifecycle.pg0_stop", return_value=False):
            # Must NOT raise when no data_dir is requested
            pg0_stop_and_clean(
                instance_name="test-instance",
                home_dir=tmp_path,
                socket_dir=socket_dir,
                # data_dir intentionally NOT provided
            )

        # Socket dir is cleaned even on failed stop (safe: reconnectable)
        # Note: the actual rm -rf needs root/olo_hs, so we just verify no exception

    def test_successful_stop_allows_data_deletion(self, tmp_path: Path) -> None:
        """When stop succeeds, data_dir deletion is permitted."""
        data_dir = tmp_path / "pg0_data"
        data_dir.mkdir()
        sentinel = data_dir / "PG_VERSION"
        sentinel.write_text("14")

        # Mock sudo rm -rf to actually delete (use subprocess with real paths)
        def fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 0
            # Actually remove the directory for the test to work
            import shutil
            if "rm" in cmd and str(data_dir) in " ".join(str(c) for c in cmd):
                shutil.rmtree(str(data_dir), ignore_errors=True)
            return m

        with patch("tests.olo.m3._pg0_lifecycle.pg0_stop", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            pg0_stop_and_clean(
                instance_name="test-instance",
                home_dir=tmp_path,
                data_dir=data_dir,
            )

        assert not data_dir.exists(), "data_dir should be deleted after successful stop"
