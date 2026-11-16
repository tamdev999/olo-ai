"""Phase B â€” required live integration test against real Google APIs.

Â§9.6: runs only when OLO_GOOGLE_TEST_HOME points at a home directory
containing valid credentials for account alias "livetest".

This test is skipped automatically in Phase A.  Run it after BBC has:
  1. Created a dedicated Google Cloud project.
  2. Enabled Gmail API and Google Calendar API.
  3. Set up an OAuth consent screen (External, Testing status).
  4. Added the test account as a test user.
  5. Created a Desktop app OAuth client.
  6. Run: hermes olo google connect --account livetest --client-secret-file <f>

Invocation (Phase B):
    OLO_GOOGLE_TEST_HOME=/path/to/isolated/home \\
    python -m pytest -m integration tests/olo/test_google_live.py -v
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Skip the entire module unless OLO_GOOGLE_TEST_HOME is set and credentials exist
_TEST_HOME = os.environ.get("OLO_GOOGLE_TEST_HOME", "")
_SKIP_REASON = "OLO_GOOGLE_TEST_HOME not set or livetest credentials absent (Phase B only)"


def _has_live_creds() -> bool:
    if not _TEST_HOME:
        return False
    try:
        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = _TEST_HOME
        from olo.google.credentials import load_credentials
        load_credentials("livetest")
        return True
    except Exception:
        return False
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        elif old is not None:
            os.environ["HERMES_HOME"] = old


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def require_live_creds():
    if not _has_live_creds():
        pytest.skip(_SKIP_REASON)


# â”€â”€ Round 0: Bootstrap â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_round0_bootstrap_gmail():
    """Bootstrap: Gmail ingests nothing on a fresh account."""
    result = subprocess.run(
        [sys.executable, "-m", "olo.runner",
         "--source", "gmail", "--option", "account=livetest"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "HERMES_HOME": _TEST_HOME},
        cwd=str(Path(__file__).parent.parent.parent),
    )
    # Exit 0 (succeeded) or partial â€” both fine for bootstrap
    assert result.returncode == 0, f"bootstrap failed: {result.stderr}"
    assert result.stdout == "", f"bootstrap must produce no stdout: {result.stdout!r}"


def test_round0_bootstrap_gcal():
    """Bootstrap: Calendar ingests events in lookback window (possibly 0)."""
    result = subprocess.run(
        [sys.executable, "-m", "olo.runner",
         "--source", "gcal", "--option", "account=livetest"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "HERMES_HOME": _TEST_HOME},
        cwd=str(Path(__file__).parent.parent.parent),
    )
    assert result.returncode == 0, f"bootstrap failed: {result.stderr}"


# â”€â”€ Round 1: Create data, then sync â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.dependency(name="round1")
def test_round1_instructions(capsys):
    """Remind BBC to create test data before running round1_sync."""
    print(
        "\n=== Phase B Round 1 ===\n"
        "Before running test_round1_gmail_sync, BBC must have:\n"
        "  - Sent â‰¥6 emails TO the test account\n"
        "  - Created 3 single events and 1 recurring event on the primary calendar\n"
        "Then run the sync tests.\n"
    )


@pytest.mark.dependency(depends=["round1"])
def test_round1_gmail_sync():
    """After BBC creates test emails, verify they appear as signals."""
    result = subprocess.run(
        [sys.executable, "-m", "olo.runner",
         "--source", "gmail", "--option", "account=livetest", "--limit", "2"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "HERMES_HOME": _TEST_HOME},
        cwd=str(Path(__file__).parent.parent.parent),
    )
    assert result.returncode == 0, f"gmail sync failed: {result.stderr}"

    # Count signals
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = _TEST_HOME
    try:
        from olo.store import connect, count_signals, migrate
        from olo.paths import db_path
        db = db_path()
        conn = connect(db)
        migrate(conn)
        n = count_signals("gmail_livetest", conn)
        conn.close()
        assert n >= 6, f"expected â‰¥6 signals, got {n}"
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


@pytest.mark.dependency(depends=["round1"])
def test_round1_gcal_sync():
    """After BBC creates calendar events, verify they appear as signals."""
    result = subprocess.run(
        [sys.executable, "-m", "olo.runner",
         "--source", "gcal", "--option", "account=livetest"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "HERMES_HOME": _TEST_HOME},
        cwd=str(Path(__file__).parent.parent.parent),
    )
    assert result.returncode == 0, f"gcal sync failed: {result.stderr}"

    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = _TEST_HOME
    try:
        from olo.store import connect, count_signals, migrate
        from olo.paths import db_path
        db = db_path()
        conn = connect(db)
        migrate(conn)
        n = count_signals("gcal_livetest", conn)
        conn.close()
        assert n >= 4, f"expected â‰¥4 calendar signals, got {n}"
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


# â”€â”€ Round 2: Mutations â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.mark.dependency(name="round2", depends=["round1"])
def test_round2_instructions(capsys):
    """Remind BBC to mutate test data before running round2 sync tests."""
    print(
        "\n=== Phase B Round 2 ===\n"
        "Before running test_round2_gmail_sync, BBC must have:\n"
        "  - Archived 1 message (remove INBOX)\n"
        "  - Marked 1 message read (remove UNREAD)\n"
        "  - Starred 1 message (add STARRED)\n"
        "  - Deleted 1 message permanently\n"
        "  - Edited the summary of 1 calendar event\n"
        "  - Cancelled 1 calendar event\n"
    )


@pytest.mark.dependency(depends=["round2"])
def test_round2_gmail_label_changes():
    """After BBC mutates messages, verify label change signals appear."""
    result = subprocess.run(
        [sys.executable, "-m", "olo.runner",
         "--source", "gmail", "--option", "account=livetest"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "HERMES_HOME": _TEST_HOME},
        cwd=str(Path(__file__).parent.parent.parent),
    )
    assert result.returncode == 0, f"gmail sync failed: {result.stderr}"

    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = _TEST_HOME
    try:
        from olo.store import connect, migrate
        from olo.paths import db_path
        import sqlite3
        db = db_path()
        conn = connect(db)
        migrate(conn)
        # Expect at least some label change signals
        label_sigs = conn.execute(
            "SELECT count(*) AS n FROM signals WHERE source='gmail_livetest' "
            "AND kind LIKE 'email.labels%'"
        ).fetchone()["n"]
        conn.close()
        assert label_sigs >= 1, f"expected label change signals, got {label_sigs}"
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


# â”€â”€ Final check: no message body in DB â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_no_body_marker_in_db():
    """The body marker phrase OLO-BODY-MARKER must not appear in olo.db."""
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = _TEST_HOME
    try:
        from olo.paths import db_path
        db = db_path()
        if not db.exists():
            pytest.skip("DB not found")
        raw = db.read_bytes()
        assert b"OLO-BODY-MARKER" not in raw, (
            "Body marker phrase found in olo.db â€” message body was stored!"
        )
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


# â”€â”€ Failure paths â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_live_history_expired():
    """Fabricated old historyId â†’ 404 handling."""
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = _TEST_HOME
    try:
        from olo.store import connect, migrate
        from olo.paths import db_path
        import sqlite3
        db = db_path()
        conn = connect(db)
        migrate(conn)
        # Set a very old historyId
        conn.execute(
            "INSERT INTO sync_checkpoints(source, cursor, updated_at, run_id) "
            "VALUES ('gmail_livetest', '1', '2026-01-01T00:00:00.000000Z', 'fake-run-id') "
            "ON CONFLICT(source) DO UPDATE SET cursor='1'",
        )
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old

    result = subprocess.run(
        [sys.executable, "-m", "olo.runner",
         "--source", "gmail", "--option", "account=livetest"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "HERMES_HOME": _TEST_HOME},
        cwd=str(Path(__file__).parent.parent.parent),
    )
    # Must exit 1 (failed run) with the history-expired message
    assert result.returncode == 1
    assert "gmail history" in result.stderr.lower() or "expired" in result.stderr.lower()
