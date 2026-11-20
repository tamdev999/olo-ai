"""Regression tests for olo.store.connect() permission behaviour.

Proves three invariants:
  1. connect() raises ValueError when the parent directory is world-writable
     and sticky (e.g. /tmp) — Olo must never be placed there.
  2. connect() chmods a NEW directory to 0o700 on POSIX.
  3. connect() leaves an EXISTING directory's permissions untouched.

These tests run offline (no Hindsight, no network).
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from olo.store import connect


# Skip on Windows — POSIX permissions are not a concern there.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only test")


class TestConnectPermissions:
    """olo.store.connect() must never chmod pre-existing directories."""

    def test_refuses_sticky_world_writable_parent(self, tmp_path: Path) -> None:
        """NC / regression: connect() must raise ValueError for /tmp-like parents.

        Root cause of the crash bug: connect() was unconditionally chmod-ing
        its parent directory to 0o700, which clobbered /tmp when
        OLO_DB_PATH=/tmp/gate_olo.db was set.

        This test creates a directory with sticky+world-writable permissions
        and verifies that connect() refuses to place a DB there.
        """
        sticky_dir = tmp_path / "sticky"
        sticky_dir.mkdir()
        # Apply sticky + world-writable (same as /tmp default on Linux)
        os.chmod(str(sticky_dir), 0o1777)

        db_path = sticky_dir / "olo.db"
        with pytest.raises(ValueError, match="world-writable sticky"):
            connect(db_path)

        # The directory permissions must be unchanged
        mode = sticky_dir.stat().st_mode & 0o7777
        assert mode == 0o1777, (
            f"NC FAIL: connect() changed permissions of the sticky directory "
            f"from 0o1777 to {oct(mode)}"
        )

    def test_new_directory_gets_0700(self, tmp_path: Path) -> None:
        """connect() creates a new parent directory at 0o700."""
        db_path = tmp_path / "new_subdir" / "olo.db"
        assert not db_path.parent.exists(), "precondition: dir must not exist yet"

        conn = connect(db_path)
        conn.close()

        parent_mode = db_path.parent.stat().st_mode & 0o777
        assert parent_mode == 0o700, (
            f"Expected new directory at 0o700, got {oct(parent_mode)}"
        )

    def test_existing_directory_permissions_unchanged(self, tmp_path: Path) -> None:
        """connect() does NOT change permissions on a pre-existing directory.

        A directory the application already owns (not sticky+world-writable)
        should be left exactly as found.  Callers may manage their own
        directory mode.
        """
        owned_dir = tmp_path / "owned"
        owned_dir.mkdir(mode=0o750)
        os.chmod(str(owned_dir), 0o750)  # ensure the umask didn't interfere

        db_path = owned_dir / "olo.db"
        conn = connect(db_path)
        conn.close()

        parent_mode = owned_dir.stat().st_mode & 0o777
        assert parent_mode == 0o750, (
            f"connect() changed an existing directory's permissions: "
            f"expected 0o750, got {oct(parent_mode)}"
        )

    def test_db_file_permissions_are_600(self, tmp_path: Path) -> None:
        """The DB file itself is created at mode 0o600 regardless of umask."""
        db_path = tmp_path / "sub" / "olo.db"
        conn = connect(db_path)
        conn.close()

        file_mode = db_path.stat().st_mode & 0o777
        assert file_mode == 0o600, (
            f"Expected DB file at 0o600, got {oct(file_mode)}"
        )

    def test_negative_control_non_sticky_world_writable_ok(
        self, tmp_path: Path
    ) -> None:
        """NC: a world-writable directory WITHOUT sticky bit is NOT refused.

        The check is specifically for sticky+world-writable (like /tmp), not
        for any world-writable directory.  A per-test directory created by a
        build system might be world-writable without the sticky bit.
        """
        # World-writable but NOT sticky
        world_dir = tmp_path / "world_w"
        world_dir.mkdir()
        os.chmod(str(world_dir), 0o777)  # no sticky bit

        db_path = world_dir / "olo.db"
        # Should NOT raise
        conn = connect(db_path)
        conn.close()
        assert db_path.exists()
