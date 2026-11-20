"""Regression tests for olo.store.connect() permission behaviour.

The security invariants tested here (Opus corrections C5):

  1. Sticky + world-writable parent (e.g. /tmp): refused with ValueError.
  2. New directory Olo creates: 0o700.
  3. Existing directory owned by this process: chmod'd to 0o700
     (restores the M1/M2 property: Olo always secures its own DB directory).
  4. Directory owned by a different uid: refused with ValueError.
  5. World-writable without sticky bit: not refused (allowed, then secured).
  6. DB file: 0o600 regardless of umask.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest import mock

import pytest

from olo.store import connect


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only test")


class TestConnectPermissions:
    """olo.store.connect() permission contract (Opus C5)."""

    # ── Refused cases ─────────────────────────────────────────────────────────

    def test_refuses_sticky_world_writable_parent(self, tmp_path: Path) -> None:
        """Sticky + world-writable parent (e.g. /tmp) must be refused."""
        sticky_dir = tmp_path / "sticky"
        sticky_dir.mkdir()
        os.chmod(str(sticky_dir), 0o1777)

        with pytest.raises(ValueError, match="world-writable sticky"):
            connect(sticky_dir / "olo.db")

        # Permissions must not have changed
        assert sticky_dir.stat().st_mode & 0o7777 == 0o1777

    def test_refuses_foreign_owned_directory(self, tmp_path: Path) -> None:
        """Directory owned by a different uid must be refused (no foreign chmod)."""
        foreign_dir = tmp_path / "foreign"
        foreign_dir.mkdir(mode=0o755)

        # Simulate a foreign-owned directory by patching geteuid
        different_uid = os.geteuid() + 1
        with mock.patch("os.geteuid", return_value=different_uid):
            with pytest.raises(ValueError, match="owned by uid"):
                connect(foreign_dir / "olo.db")

        # Directory permissions must be unchanged
        assert foreign_dir.stat().st_mode & 0o777 == 0o755

    # ── Permitted and secured cases ───────────────────────────────────────────

    def test_new_directory_gets_0700(self, tmp_path: Path) -> None:
        """A new directory created by Olo must be at 0o700."""
        db_path = tmp_path / "new_subdir" / "olo.db"
        assert not db_path.parent.exists()

        conn = connect(db_path)
        conn.close()

        assert db_path.parent.stat().st_mode & 0o777 == 0o700

    def test_existing_owned_directory_secured_to_0700(self, tmp_path: Path) -> None:
        """An existing directory owned by this process must be chmod'd to 0o700.

        Restores the M1/M2 invariant: Olo always enforces 0700 on its own DB
        directory.  A pre-existing 0o755 Hermes-managed directory (e.g.
        ~/.hermes/olo/) must be secured when Olo first opens a DB in it.
        """
        owned_dir = tmp_path / "owned"
        owned_dir.mkdir(mode=0o755)
        os.chmod(str(owned_dir), 0o755)  # ensure umask didn't interfere

        conn = connect(owned_dir / "olo.db")
        conn.close()

        # Must be secured to 0700, not left at 0755
        assert owned_dir.stat().st_mode & 0o777 == 0o700, (
            "connect() must chmod an Olo-owned existing directory to 0o700"
        )

    def test_world_writable_no_sticky_bit_permitted(self, tmp_path: Path) -> None:
        """World-writable directory WITHOUT sticky bit: permitted, then secured."""
        world_dir = tmp_path / "world_w"
        world_dir.mkdir()
        os.chmod(str(world_dir), 0o777)  # no sticky bit

        conn = connect(world_dir / "olo.db")
        conn.close()

        # Must have been created and secured (0700, not refused)
        assert (world_dir / "olo.db").exists()
        assert world_dir.stat().st_mode & 0o777 == 0o700

    def test_db_file_permissions_are_600(self, tmp_path: Path) -> None:
        """DB file is created at 0o600."""
        db_path = tmp_path / "sub" / "olo.db"
        conn = connect(db_path)
        conn.close()

        assert db_path.stat().st_mode & 0o777 == 0o600

    # ── Negative controls ─────────────────────────────────────────────────────

    def test_nc_sticky_no_world_write_not_refused(self, tmp_path: Path) -> None:
        """Sticky WITHOUT world-writable is not refused (e.g. 0o1700 root-only sticky)."""
        sticky_no_ww = tmp_path / "sticky_private"
        sticky_no_ww.mkdir(mode=0o1700)
        os.chmod(str(sticky_no_ww), 0o1700)

        conn = connect(sticky_no_ww / "olo.db")
        conn.close()
        assert (sticky_no_ww / "olo.db").exists()
