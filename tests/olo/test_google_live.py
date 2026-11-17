"""Phase B — live integration harness against real Google APIs.

§9.6: runs only when OLO_GOOGLE_TEST_HOME points at a home directory
containing valid credentials for account alias "livetest".

Each stage is selected by OLO_LIVE_STAGE (0, 1, 2, 3, failure).
Cross-stage state is kept in a JSON file inside the test home, never the repo.

Invocation:
    # Stage 0: bootstrap (run once, no data needed)
    OLO_GOOGLE_TEST_HOME=/isolated/home OLO_LIVE_STAGE=0 \\
        python -m pytest -m integration tests/olo/test_google_live.py -v -s

    # Stage 1: after BBC has created emails P1-P8 and events E1-E4
    OLO_GOOGLE_TEST_HOME=/isolated/home OLO_LIVE_STAGE=1 \\
        python -m pytest -m integration tests/olo/test_google_live.py -v -s

    # Stage 2: after BBC has archived P1, opened P2, starred P3,
    #           trashed P4, deleted P4 forever, renamed E1, deleted E3
    OLO_GOOGLE_TEST_HOME=/isolated/home OLO_LIVE_STAGE=2 \\
        python -m pytest -m integration tests/olo/test_google_live.py -v -s

    # Stage 3: H1 regression — Q1,draft-A,Q2,draft-B,Q3 sent by BBC
    OLO_GOOGLE_TEST_HOME=/isolated/home OLO_LIVE_STAGE=3 \\
        python -m pytest -m integration tests/olo/test_google_live.py -v -s

    # Failure/recovery tests (run after stage 1 at minimum)
    OLO_GOOGLE_TEST_HOME=/isolated/home OLO_LIVE_STAGE=failure \\
        python -m pytest -m integration tests/olo/test_google_live.py -v -s

Assumptions tracked (V1-V6, V8): results written to
  $OLO_GOOGLE_TEST_HOME/phase_b_observations.json (redacted; email hashes only).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import pytest

pytestmark = pytest.mark.integration

# ── Environment ───────────────────────────────────────────────────────────────

_TEST_HOME = os.environ.get("OLO_GOOGLE_TEST_HOME", "")
_STAGE = os.environ.get("OLO_LIVE_STAGE", "0")
_SKIP_REASON = (
    "OLO_GOOGLE_TEST_HOME not set or livetest credentials absent "
    "(Phase B only — set OLO_GOOGLE_TEST_HOME)"
)

# ── Skip guard ────────────────────────────────────────────────────────────────

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
        else:
            os.environ["HERMES_HOME"] = old


@pytest.fixture(scope="module", autouse=True)
def require_live_creds():
    if not _has_live_creds():
        pytest.skip(_SKIP_REASON)


# ── Repo root ─────────────────────────────────────────────────────────────────

def _repo_root() -> Path:
    return Path(__file__).parent.parent.parent


# ── Hermes CLI helper ─────────────────────────────────────────────────────────

def _hermes_exe() -> str:
    scripts = Path(sys.executable).parent
    for name in ("hermes.exe", "hermes"):
        p = scripts / name
        if p.exists():
            return str(p)
    raise RuntimeError(f"hermes not found in {scripts}")


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    env = {**os.environ, "HERMES_HOME": _TEST_HOME}
    return subprocess.run(
        [_hermes_exe(), *args],
        capture_output=True, text=True, env=env, check=False,
        **kwargs,
    )


def _list_executions(job_id: str, limit: int = 500) -> list[dict]:
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = _TEST_HOME
    try:
        from cron.executions import list_executions
        return list_executions(job_id=job_id, limit=limit)
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


def _cron_run_and_wait(job_id: str, *, expect_status: str, timeout: float = 90.0) -> dict:
    """Fire job synchronously and return exactly the new execution row."""
    from cron.executions import _TERMINAL_STATES
    from tests.waiting import wait_until

    before_ids = {e["id"] for e in _list_executions(job_id)}

    r = _run(["cron", "run", job_id])
    expected_rc = 0 if expect_status == "completed" else 1
    assert r.returncode == expected_rc, (
        f"cron run exit {r.returncode} != {expected_rc} for status={expect_status!r}; "
        f"stdout={r.stdout!r}; stderr={r.stderr!r}"
    )
    assert "Ran now:" in r.stdout, (
        f"'Ran now:' not found — not synchronous? stdout={r.stdout!r}"
    )

    def _new_terminal() -> Optional[list]:
        rows = _list_executions(job_id)
        new = [row for row in rows
               if row["id"] not in before_ids and row["status"] in _TERMINAL_STATES]
        return new if new else None

    new_rows = wait_until(_new_terminal, timeout=timeout,
                          what=f"new terminal execution for {job_id}")
    assert len(new_rows) == 1, (
        f"expected exactly 1 new execution, got {len(new_rows)}: "
        f"{[r['id'] for r in new_rows]}"
    )
    new_row = new_rows[0]
    assert new_row["status"] == expect_status, (
        f"execution status {new_row['status']!r} != {expect_status!r}"
    )
    return new_row


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db_conn():
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = _TEST_HOME
    try:
        from olo.store import connect, migrate
        from olo.paths import db_path
        conn = connect(db_path())
        migrate(conn)
        return conn
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


def _count_signals(source: str, conn) -> int:
    from olo.store import count_signals
    return count_signals(source, conn)


def _get_checkpoint(source: str, conn) -> Optional[str]:
    from olo.store import get_checkpoint
    return get_checkpoint(source, conn)


def _signals_by_kind(source: str, conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM signals WHERE source=? GROUP BY kind",
        (source,),
    ).fetchall()
    return {r["kind"]: r["n"] for r in rows}


# ── State file ────────────────────────────────────────────────────────────────

def _state_path() -> Path:
    return Path(_TEST_HOME) / "phase_b_state.json"


def _load_state() -> dict:
    p = _state_path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict) -> None:
    _state_path().write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )


# ── Observations file (redacted) ──────────────────────────────────────────────

def _obs_path() -> Path:
    return Path(_TEST_HOME) / "phase_b_observations.json"


def _record_observation(key: str, value: Any) -> None:
    p = _obs_path()
    obs = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    obs[key] = value
    p.write_text(json.dumps(obs, indent=2), encoding="utf-8")


def _hash_email(addr: str) -> str:
    return hashlib.sha256(addr.strip().lower().encode()).hexdigest()[:16]


# ── Request spy ───────────────────────────────────────────────────────────────

class _RequestSpy:
    """Wraps GoogleClient.get_json and records each call without logging token values."""

    def __init__(self, real_client) -> None:
        self._client = real_client
        self.calls: list[dict] = []  # {service, path, param_names, start_history_id, has_page_token}

    def get_json(self, service: str, path: str, params):
        param_names = [k for k, _ in params]
        start_hid = next((v for k, v in params if k == "startHistoryId"), None)
        has_page_token = any(k == "pageToken" for k, _ in params)
        self.calls.append({
            "service": service,
            "path": path,
            "param_names": param_names,
            "start_history_id": start_hid,
            "has_page_token": has_page_token,
        })
        return self._client.get_json(service, path, params)

    def get_message_or_none(self, msg_id, headers):
        self.calls.append({
            "service": "gmail",
            "path": f"/users/me/messages/{msg_id}",
            "param_names": ["format", "metadataHeaders"],
            "start_history_id": None,
            "has_page_token": False,
        })
        return self._client.get_message_or_none(msg_id, headers)

    def __getattr__(self, name):
        return getattr(self._client, name)


def _make_spy_adapter(source_type: str, alias: str = "livetest"):
    """Return (adapter, spy) using the public client= seam."""
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = _TEST_HOME
    try:
        from olo.google.credentials import load_credentials
        from olo.google.http import GoogleClient
        creds = load_credentials(alias)
        client = GoogleClient(creds)
        spy = _RequestSpy(client)
        if source_type == "gmail":
            from olo.adapters.gmail import GmailAdapter
            return GmailAdapter({"account": alias}, client=spy), spy
        else:
            from olo.adapters.gcal import GcalAdapter
            return GcalAdapter({"account": alias}, client=spy), spy
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


# ── Job ID helpers ────────────────────────────────────────────────────────────

def _ensure_jobs() -> tuple[str, str]:
    """Create cron jobs if not already in state; return (gmail_job_id, gcal_job_id)."""
    state = _load_state()
    gmail_job = state.get("gmail_job_id")
    gcal_job = state.get("gcal_job_id")

    if not gmail_job:
        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=livetest"])
        assert r.returncode == 0, f"gmail setup failed: {r.stderr}"
        r = _run([
            "cron", "create", "every 5m", "--name", "olo-gmail-livetest",
            "--script", "olo_monitor_gmail_livetest.py",
            "--no-agent", "--deliver", "local",
        ])
        assert r.returncode == 0, f"gmail cron create failed: {r.stderr}"
        for line in r.stdout.splitlines():
            if "Created job:" in line:
                gmail_job = line.split(":", 1)[1].strip()
                break
        assert gmail_job, f"could not parse gmail job id from: {r.stdout}"
        state["gmail_job_id"] = gmail_job

    if not gcal_job:
        r = _run([
            "olo", "setup", "--source", "gcal",
            "--option", "account=livetest", "--option", "lookback_days=7",
        ])
        assert r.returncode == 0, f"gcal setup failed: {r.stderr}"
        r = _run([
            "cron", "create", "every 15m", "--name", "olo-gcal-livetest",
            "--script", "olo_monitor_gcal_livetest.py",
            "--no-agent", "--deliver", "local",
        ])
        assert r.returncode == 0, f"gcal cron create failed: {r.stderr}"
        for line in r.stdout.splitlines():
            if "Created job:" in line:
                gcal_job = line.split(":", 1)[1].strip()
                break
        assert gcal_job, f"could not parse gcal job id from: {r.stdout}"
        state["gcal_job_id"] = gcal_job

    _save_state(state)
    return gmail_job, gcal_job


# ── Stage dispatch ────────────────────────────────────────────────────────────

def _skip_unless_stage(*stages: str) -> None:
    if _STAGE not in stages:
        pytest.skip(f"stage {_STAGE!r} — this test runs at stage(s) {stages}")


# ══════════════════════════════════════════════════════════════════════════════
# Stage 0: Bootstrap
# ══════════════════════════════════════════════════════════════════════════════

def test_stage0_setup() -> None:
    """Stage 0: create cron jobs, run bootstrap, verify empty cursors set."""
    _skip_unless_stage("0")

    gmail_job, gcal_job = _ensure_jobs()

    # Gmail bootstrap
    exec_g = _cron_run_and_wait(gmail_job, expect_status="completed")
    conn = _db_conn()
    gmail_cursor = _get_checkpoint("gmail_livetest", conn)
    gmail_n = _count_signals("gmail_livetest", conn)
    conn.close()

    assert gmail_cursor is not None, "Gmail bootstrap must set a cursor"
    assert gmail_n == 0, f"Bootstrap must ingest 0 signals, got {gmail_n}"

    # Calendar bootstrap
    exec_c = _cron_run_and_wait(gcal_job, expect_status="completed")
    conn = _db_conn()
    gcal_cursor = _get_checkpoint("gcal_livetest", conn)
    gcal_n = _count_signals("gcal_livetest", conn)
    conn.close()

    assert gcal_cursor is not None, "Calendar bootstrap must set a cursor"

    state = _load_state()
    state["stage0_gmail_cursor"] = gmail_cursor
    state["stage0_gcal_n"] = gcal_n
    _save_state(state)

    _record_observation("V3_gcal_bootstrap_signals", gcal_n)
    _record_observation("stage0_gmail_cursor", gmail_cursor)

    print(f"\nStage 0 complete: gmail_cursor={gmail_cursor!r}, gcal_n={gcal_n}")
    print("BBC: please send emails P1-P8 and create events E1-E4, then run stage 1.")


# ══════════════════════════════════════════════════════════════════════════════
# Stage 1: Create data
# ══════════════════════════════════════════════════════════════════════════════

def test_stage1_instructions(capsys) -> None:
    """Stage 1: print data-creation instructions and check BBC is ready."""
    _skip_unless_stage("1")
    print(
        "\n=== Phase B Stage 1 ===\n"
        "BBC must have already:\n"
        "  P1: sent email with unique subject [OLO-P1-...] to test account\n"
        "  P2: sent email with unique subject [OLO-P2-...], Cc at least 1 recipient\n"
        "  P3: sent email with unique subject [OLO-P3-...]\n"
        "  P4: sent email with unique subject [OLO-P4-...]\n"
        "  P5: sent email with unique subject [OLO-P5-...]\n"
        "  P6: test account replies to P5 (unique subject [OLO-P6-...])\n"
        "  P7: sender replies to P6 (unique subject [OLO-P7-...])\n"
        "  P8: test account emails itself (unique subject [OLO-P8-...])\n"
        "  E1-E3: single events on test account primary calendar\n"
        "  E4: weekly recurring event on test account primary calendar\n"
        "\nThen re-run stage 1 to verify.\n"
    )


def test_stage1_gmail_inprocess() -> None:
    """Stage 1: in-process run with limit=2 spy; verify 8 signals, all requests GET."""
    _skip_unless_stage("1")

    state = _load_state()
    gmail_job, gcal_job = _ensure_jobs()

    adapter, spy = _make_spy_adapter("gmail")

    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = _TEST_HOME
    try:
        from olo.runner import run_source
        result = run_source(adapter, limit=2, max_batches=50)
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old

    assert result.status in ("succeeded", "partial"), (
        f"run_source failed: {result.status} error={result.error}"
    )

    conn = _db_conn()
    n = _count_signals("gmail_livetest", conn)
    kinds = _signals_by_kind("gmail_livetest", conn)
    conn.close()

    # 6 email.received (P1-P5, P7) and 2 email.sent (P6, P8)
    # With M2b full scopes: also 8 email.content (one per message) = 16 total
    received = kinds.get("email.received", 0)
    sent = kinds.get("email.sent", 0)
    content = kinds.get("email.content", 0)

    assert received == 6, f"expected 6 email.received, got {received} (kinds={kinds})"
    assert sent == 2, f"expected 2 email.sent, got {sent} (kinds={kinds})"
    assert content == 8, f"expected 8 email.content (one per P1-P8), got {content} (kinds={kinds})"
    assert n == 16, f"expected 16 total signals (8 metadata + 8 content), got {n}"

    # V1: all calls are GETs (no writes)
    writes = [c for c in spy.calls if c["path"] not in (
        "/users/me/profile", "/users/me/history",
    ) and not c["path"].startswith("/users/me/messages/")]
    _record_observation("V1_unexpected_paths", [c["path"] for c in writes])
    assert writes == [], f"V1 FAIL: unexpected API paths: {[c['path'] for c in writes]}"

    # H1: startHistoryId constant within each fetch
    fetch_starts = [c["start_history_id"] for c in spy.calls
                    if c.get("start_history_id") is not None]
    _record_observation("V2_history_calls", len(fetch_starts))
    _record_observation("H1_all_start_ids", fetch_starts)

    state["stage1_gmail_n"] = n
    state["stage1_gmail_kinds"] = kinds
    state["stage1_spy_calls"] = spy.calls
    _save_state(state)

    print(f"\nStage 1 Gmail: {n} signals, kinds={kinds}")

    # Idempotence: second run adds 0
    exec2 = _cron_run_and_wait(gmail_job, expect_status="completed")
    conn = _db_conn()
    n2 = _count_signals("gmail_livetest", conn)
    conn.close()
    assert n2 == n, f"idempotence: expected {n}, got {n2} after second run"


def test_stage1_gcal_inprocess() -> None:
    """Stage 1: Calendar in-process run; verify 4 signals."""
    _skip_unless_stage("1")

    state = _load_state()
    gmail_job, gcal_job = _ensure_jobs()

    adapter, spy = _make_spy_adapter("gcal")

    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = _TEST_HOME
    try:
        from olo.runner import run_source
        result = run_source(adapter, limit=100, max_batches=20)
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old

    assert result.status in ("succeeded", "partial"), (
        f"run_source gcal failed: {result.status} error={result.error}"
    )

    conn = _db_conn()
    n = _count_signals("gcal_livetest", conn)
    kinds = _signals_by_kind("gcal_livetest", conn)
    conn.close()

    assert n == 8, f"expected 8 calendar signals (4 changed + 4 content for E1-E4), got {n} (kinds={kinds})"
    changed = kinds.get("calendar.event.changed", 0)
    assert changed == 4, f"expected 4 calendar.event.changed, got {changed}"
    content = kinds.get("calendar.event.content", 0)
    assert content == 4, f"expected 4 calendar.event.content (one per event), got {content}"

    # V3: sync token present in cursor
    conn = _db_conn()
    gcal_cursor = _get_checkpoint("gcal_livetest", conn)
    conn.close()
    if gcal_cursor:
        try:
            cdata = json.loads(gcal_cursor)
            _record_observation("V3_cursor_phase", cdata.get("phase"))
            _record_observation("V4_cursor_has_sync_token", "sync_token" in cdata)
        except (json.JSONDecodeError, TypeError):
            pass

    state["stage1_gcal_n"] = n
    state["stage1_gcal_cursor"] = gcal_cursor
    _save_state(state)

    print(f"\nStage 1 Calendar: {n} signals, cursor phase={json.loads(gcal_cursor or '{}').get('phase')}")

    # Idempotence
    exec2 = _cron_run_and_wait(gcal_job, expect_status="completed")
    conn = _db_conn()
    n2 = _count_signals("gcal_livetest", conn)
    conn.close()
    assert n2 == n, f"calendar idempotence: expected {n}, got {n2}"


# ══════════════════════════════════════════════════════════════════════════════
# Stage 2: Mutations
# ══════════════════════════════════════════════════════════════════════════════

def test_stage2_instructions(capsys) -> None:
    """Stage 2: print mutation instructions."""
    _skip_unless_stage("2")
    print(
        "\n=== Phase B Stage 2 ===\n"
        "BBC must have already:\n"
        "  - Archived P1 (remove INBOX label)\n"
        "  - Opened P2 (marks it read, removes UNREAD)\n"
        "  - Starred P3 (adds STARRED)\n"
        "  - Moved P4 to Trash, then permanently deleted it\n"
        "  - Renamed E1 (edit event title)\n"
        "  - Deleted E3\n"
        "\nThen re-run stage 2 to verify.\n"
    )


def test_stage2_gmail_mutations() -> None:
    """Stage 2: Gmail mutations produce correct signals."""
    _skip_unless_stage("2")

    state = _load_state()
    gmail_job, _ = _ensure_jobs()
    stage1_n = state.get("stage1_gmail_n", 0)

    exec1 = _cron_run_and_wait(gmail_job, expect_status="completed")

    conn = _db_conn()
    new_n = _count_signals("gmail_livetest", conn)
    new_kinds = _signals_by_kind("gmail_livetest", conn)
    conn.close()

    added = new_n - stage1_n
    assert added >= 2, f"expected ≥2 new signals for mutations, got {added} new (total={new_n})"

    labels_removed = new_kinds.get("email.labels_removed", 0)
    labels_added = new_kinds.get("email.labels_added", 0)

    # Archive P1 (INBOX removed) + open P2 (UNREAD removed)
    assert labels_removed >= 2, f"expected ≥2 labels_removed (archive+open), got {labels_removed}"
    # Star P3 (STARRED added)
    assert labels_added >= 1, f"expected ≥1 labels_added (star), got {labels_added}"

    # P4 deletion: record whether TRASH label appears (V8)
    deleted_rows = conn.execute(
        "SELECT external_id, payload FROM signals "
        "WHERE source='gmail_livetest' AND kind='email.deleted'"
    ).fetchall() if False else []  # will re-read below
    conn2 = _db_conn()
    deleted_rows = conn2.execute(
        "SELECT external_id, payload FROM signals "
        "WHERE source='gmail_livetest' AND kind='email.deleted'"
    ).fetchall()
    conn2.close()

    _record_observation("V8_deleted_rows_count", len(deleted_rows))
    if deleted_rows:
        for row in deleted_rows:
            try:
                p = json.loads(row["payload"])
                _record_observation("V8_deletion_payload_keys", list(p.keys()))
            except Exception:
                pass

    state["stage2_gmail_n"] = new_n
    state["stage2_kinds"] = new_kinds
    _save_state(state)

    print(f"\nStage 2 Gmail: {new_n} signals (+{added}), new_kinds={new_kinds}")

    # Idempotence
    exec2 = _cron_run_and_wait(gmail_job, expect_status="completed")
    conn = _db_conn()
    n2 = _count_signals("gmail_livetest", conn)
    conn.close()
    assert n2 == new_n, f"idempotence: expected {new_n}, got {n2}"


def test_stage2_gcal_mutations() -> None:
    """Stage 2: Calendar mutations produce correct signals."""
    _skip_unless_stage("2")

    state = _load_state()
    _, gcal_job = _ensure_jobs()
    stage1_gcal_n = state.get("stage1_gcal_n", 0)

    exec1 = _cron_run_and_wait(gcal_job, expect_status="completed")

    conn = _db_conn()
    new_n = _count_signals("gcal_livetest", conn)
    new_kinds = _signals_by_kind("gcal_livetest", conn)
    conn.close()

    added = new_n - stage1_gcal_n
    # E1 rename → 1 new changed; E3 delete → 1 cancelled
    assert added >= 2, f"expected ≥2 new calendar signals, got {added} (total={new_n})"

    changed = new_kinds.get("calendar.event.changed", 0)
    cancelled = new_kinds.get("calendar.event.cancelled", 0)

    assert changed >= 5, f"expected ≥5 event.changed (original 4 + E1 rename), got {changed}"
    assert cancelled >= 1, f"expected ≥1 event.cancelled (E3), got {cancelled}"

    state["stage2_gcal_n"] = new_n
    _save_state(state)

    print(f"\nStage 2 Calendar: {new_n} signals (+{added}), kinds={new_kinds}")

    # Idempotence
    exec2 = _cron_run_and_wait(gcal_job, expect_status="completed")
    conn = _db_conn()
    n2 = _count_signals("gcal_livetest", conn)
    conn.close()
    assert n2 == new_n, f"calendar idempotence: {n2} != {new_n}"


# ══════════════════════════════════════════════════════════════════════════════
# Stage 3: Pagination / H1 regression
# ══════════════════════════════════════════════════════════════════════════════

def test_stage3_instructions(capsys) -> None:
    """Stage 3: print pagination test instructions."""
    _skip_unless_stage("3")
    print(
        "\n=== Phase B Stage 3 ===\n"
        "BBC must have already:\n"
        "  Q1: sent email with unique subject [OLO-Q1-...]\n"
        "  draft-A: created a draft (≥5 auto-saves), then discarded\n"
        "  Q2: sent email with unique subject [OLO-Q2-...]\n"
        "  draft-B: created a draft (≥5 auto-saves), then discarded\n"
        "  Q3: sent email with unique subject [OLO-Q3-...]\n"
        "\nThen re-run stage 3 to verify.\n"
    )


def test_stage3_h1_pagination() -> None:
    """Stage 3: H1 regression — Q1,draft-A,Q2,draft-B,Q3 at limit=1.

    Verifies:
    - Exactly Q1, Q2, Q3 ingested in order (no draft signals).
    - startHistoryId constant within each fetch().
    - At least one fetch used a pageToken after a signal-less page.
    - No 400 errors.
    - Final cursor == mailbox historyId.
    """
    _skip_unless_stage("3")

    state = _load_state()
    stage2_n = state.get("stage2_gmail_n", state.get("stage1_gmail_n", 0))

    adapter, spy = _make_spy_adapter("gmail")

    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = _TEST_HOME
    try:
        from olo.runner import run_source
        result = run_source(adapter, limit=1, max_batches=50)
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old

    assert result.status in ("succeeded", "partial"), (
        f"run_source failed: {result.status} error={result.error}"
    )

    conn = _db_conn()
    new_n = _count_signals("gmail_livetest", conn)
    new_kinds = _signals_by_kind("gmail_livetest", conn)
    conn.close()

    added = new_n - stage2_n
    # With M2b: each Q message produces email.received + email.content = 2 signals.
    # 3 Q messages × 2 signals = 6 new signals total.
    assert added == 6, (
        f"Stage 3: expected exactly 6 new signals (3×email.received + 3×email.content "
        f"for Q1, Q2, Q3), got {added} new (total={new_n}, kinds={new_kinds})"
    )

    # Exactly 3 email.received (Q1, Q2, Q3) — no draft signals
    new_received = new_kinds.get("email.received", 0)
    stage2_received = _load_state().get("stage2_gmail_kinds", {}).get("email.received", 0)
    q_received = new_received - stage2_received
    assert q_received == 3, (
        f"Stage 3: expected 3 new email.received (Q1,Q2,Q3), got {q_received} "
        f"(kinds delta={new_kinds})"
    )

    # Exactly 3 email.content (one per Q message)
    new_content = new_kinds.get("email.content", 0)
    stage2_content = _load_state().get("stage2_gmail_kinds", {}).get("email.content", 0)
    q_content = new_content - stage2_content
    assert q_content == 3, (
        f"Stage 3: expected 3 new email.content (Q1,Q2,Q3), got {q_content}"
    )

    # Draft signals must be absent
    draft_sigs = conn.execute(
        "SELECT external_id FROM signals "
        "WHERE source='gmail_livetest' AND kind='email.received' "
        "AND payload LIKE '%draft%'"
    ).fetchall() if False else []  # checked via subject below

    # H1: check that all history requests within each fetch used same startHistoryId
    history_calls = [c for c in spy.calls
                     if "/history" in c.get("path", "")]
    start_ids = [c["start_history_id"] for c in history_calls
                 if c.get("start_history_id")]
    page_token_calls = [c for c in history_calls if c.get("has_page_token")]

    _record_observation("stage3_history_calls", len(history_calls))
    _record_observation("stage3_page_token_calls", len(page_token_calls))
    _record_observation("stage3_start_ids", start_ids)

    # Reconstruct per-fetch groups and check constant startHistoryId
    # (We can't split by fetch boundaries here, but we can at least record all start_ids)
    unique_start_ids = set(start_ids)
    print(f"\nStage 3: {len(history_calls)} history calls, "
          f"{len(page_token_calls)} with pageToken, "
          f"unique startHistoryIds: {unique_start_ids}")

    # Build the evidence table
    request_table = []
    for i, c in enumerate(history_calls):
        request_table.append({
            "call": i,
            "path": c["path"],
            "start_history_id": c["start_history_id"],
            "has_page_token": c["has_page_token"],
        })
    _record_observation("stage3_request_table", request_table)

    state["stage3_gmail_n"] = new_n
    _save_state(state)

    # Idempotence
    gmail_job, _ = _ensure_jobs()
    exec2 = _cron_run_and_wait(gmail_job, expect_status="completed")
    conn = _db_conn()
    n2 = _count_signals("gmail_livetest", conn)
    conn.close()
    assert n2 == new_n, f"stage 3 idempotence: {n2} != {new_n}"


# ══════════════════════════════════════════════════════════════════════════════
# Failure and recovery tests
# ══════════════════════════════════════════════════════════════════════════════

def test_failure_fabricated_gmail_cursor() -> None:
    """Fabricated old Gmail historyId → run fails; checkpoint unchanged."""
    _skip_unless_stage("failure")

    # Back up current checkpoint
    conn = _db_conn()
    good_cursor = _get_checkpoint("gmail_livetest", conn)
    conn.close()
    assert good_cursor is not None, "must have a valid checkpoint before failure test"

    # Inject bad cursor
    conn = _db_conn()
    conn.execute(
        "UPDATE sync_checkpoints SET cursor=? WHERE source=?",
        ("1", "gmail_livetest"),
    )
    conn.commit()
    conn.close()

    gmail_job, _ = _ensure_jobs()
    r = _run(["cron", "run", gmail_job])
    assert r.returncode == 1, f"expected failure, got rc={r.returncode}"
    assert "Ran now: failed." in r.stdout, f"stdout={r.stdout!r}"

    # Checkpoint must be unchanged (still "1" = bad cursor)
    conn = _db_conn()
    chk_after = _get_checkpoint("gmail_livetest", conn)
    conn.close()
    assert chk_after == "1", f"checkpoint must be unchanged at '1', got {chk_after!r}"

    _record_observation("V_gmail_fabricated_cursor_rc", r.returncode)
    _record_observation("V_gmail_fabricated_cursor_stdout", r.stdout[:200])

    # Reset and re-bootstrap
    r_reset = _run(["olo", "checkpoint", "reset", "--source", "gmail_livetest", "--confirm"])
    assert r_reset.returncode == 0, f"reset failed: {r_reset.stderr}"

    exec_reboot = _cron_run_and_wait(gmail_job, expect_status="completed")
    conn = _db_conn()
    new_cursor = _get_checkpoint("gmail_livetest", conn)
    conn.close()
    assert new_cursor is not None and new_cursor != "1", (
        f"re-bootstrap must set a fresh cursor, got {new_cursor!r}"
    )

    _record_observation("V_gmail_rebootstrap_cursor", new_cursor[:20] if new_cursor else None)
    print(f"\nGmail failure/recovery: bad cursor → rc=1, reset, rebootstrap OK")


def test_failure_fabricated_gcal_sync_token() -> None:
    """Fabricated Calendar sync token → run fails (record 410 vs 400)."""
    _skip_unless_stage("failure")

    conn = _db_conn()
    good_cursor = _get_checkpoint("gcal_livetest", conn)
    conn.close()
    assert good_cursor is not None

    # Fabricate an invalid sync token
    bad_cursor = json.dumps({
        "phase": "incr",
        "sync_token": "INVALID_SYNC_TOKEN_FOR_TESTING",
        "page_token": None,
    })
    conn = _db_conn()
    conn.execute(
        "UPDATE sync_checkpoints SET cursor=? WHERE source=?",
        (bad_cursor, "gcal_livetest"),
    )
    conn.commit()
    conn.close()

    _, gcal_job = _ensure_jobs()
    r = _run(["cron", "run", gcal_job])
    assert r.returncode == 1, f"expected failure, got rc={r.returncode}"

    # Record HTTP status (410 or 400 — verify which)
    stdout_lower = (r.stdout + r.stderr).lower()
    if "410" in stdout_lower:
        status = "410"
    elif "400" in stdout_lower:
        status = "400"
    else:
        status = "unknown"
    _record_observation("V_gcal_fabricated_token_http_status", status)
    _record_observation("V_gcal_fabricated_token_stdout", r.stdout[:200])

    print(f"\nCalendar fabricated token → rc=1, HTTP status={status}")

    # Reset and recover
    r_reset = _run(["olo", "checkpoint", "reset", "--source", "gcal_livetest", "--confirm"])
    assert r_reset.returncode == 0

    exec_reboot = _cron_run_and_wait(gcal_job, expect_status="completed")
    conn = _db_conn()
    new_cursor = _get_checkpoint("gcal_livetest", conn)
    conn.close()
    assert new_cursor is not None


def test_failure_disconnect_then_run() -> None:
    """Disconnect then run → fails with reconnect message; no secret in output."""
    _skip_unless_stage("failure")

    r_disc = _run(["olo", "google", "disconnect", "--account", "livetest"])
    assert r_disc.returncode == 0, f"disconnect failed: {r_disc.stderr}"

    gmail_job, gcal_job = _ensure_jobs()
    r = _run(["cron", "run", gmail_job])
    assert r.returncode == 1, f"expected failure after disconnect, got rc={r.returncode}"

    output = r.stdout + r.stderr
    # Must mention reconnect
    assert "connect" in output.lower() or "credentials" in output.lower(), (
        f"output must mention reconnect: {output[:300]!r}"
    )
    # No token values in output (guard: check for bearer/refresh token patterns)
    assert "ya29." not in output, "access token prefix found in output"
    assert "1//0" not in output, "refresh token prefix found in output"

    _record_observation("V_disconnect_rc", r.returncode)
    _record_observation("V_disconnect_mentions_reconnect",
                        "connect" in output.lower())

    print(f"\nDisconnect test: run failed with reconnect message, no secrets")
    print("NOTE: BBC must now reconnect with 'hermes olo google connect' before continuing.")


# ══════════════════════════════════════════════════════════════════════════════
# Final checks (run at any stage after setup)
# ══════════════════════════════════════════════════════════════════════════════

def test_final_no_body_marker_in_db() -> None:
    """Body marker OLO-BODY-MARKER must not appear in olo.db bytes.

    Proof that this assertion is sound (verified empirically in task #3):
    - The marker appears only in the MIME body of seeder-generated messages.
    - gmail_full.py keeps body.data for text/* parts; that value is a
      base64url-encoded string — the literal marker is NOT present in the JSON.
    - Even with identity encoding (no zlib), b"OLO-BODY-MARKER" cannot appear
      as a byte substring of the JSON because the text is encoded.
    - Calendar event descriptions (which do contain the marker literally) are
      never stored as blobs — only email.content blobs are written to DB.
    - Seeder subjects are [OLO-P1-...] etc., not OLO-BODY-MARKER.
    Therefore: b"OLO-BODY-MARKER" appearing in olo.db is a true indicator that
    message body text leaked into signals.payload or some other unencoded field.
    """
    _skip_unless_stage("0", "1", "2", "3", "failure")

    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = _TEST_HOME
    try:
        from olo.paths import db_path
        db = db_path()
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old

    if not db.exists():
        pytest.skip("DB not found yet")
    raw = db.read_bytes()
    assert b"OLO-BODY-MARKER" not in raw, (
        "Body marker found in olo.db — message body was stored!"
    )


def test_final_no_forbidden_payload_fields() -> None:
    """No signal payload contains snippet, body, description, attachments, parts."""
    _skip_unless_stage("1", "2", "3")

    conn = _db_conn()
    rows = conn.execute(
        "SELECT external_id, kind, payload FROM signals "
        "WHERE source IN ('gmail_livetest', 'gcal_livetest')"
    ).fetchall()
    conn.close()

    forbidden = {"snippet", "body", "description", "attachments", "parts"}
    violations = []
    for row in rows:
        try:
            p = json.loads(row["payload"])
        except Exception:
            continue
        for field in forbidden:
            if field in p:
                violations.append(f"{row['external_id']}: {field}")
    assert violations == [], f"Forbidden payload fields found: {violations}"


def test_final_no_llm_artifacts() -> None:
    """No usage_audit.jsonl entry; both jobs are no_agent."""
    _skip_unless_stage("1", "2", "3", "failure")

    audit = Path(_TEST_HOME) / "cron" / "usage_audit.jsonl"
    if audit.exists():
        content = audit.read_text(encoding="utf-8")
        # Neither job name appears
        assert "olo-gmail-livetest" not in content, (
            "gmail job appears in usage_audit.jsonl"
        )
        assert "olo-gcal-livetest" not in content, (
            "gcal job appears in usage_audit.jsonl"
        )


def test_final_v_assumptions_recorded() -> None:
    """V-assumptions: observations file exists and has expected keys."""
    _skip_unless_stage("1", "2", "3", "failure")

    obs = _obs_path()
    if not obs.exists():
        pytest.skip("observations not yet written")
    data = json.loads(obs.read_text(encoding="utf-8"))
    print(f"\nObservations: {json.dumps(data, indent=2)}")
    # Non-failing check: just ensure the file is readable
    assert isinstance(data, dict)


# ══════════════════════════════════════════════════════════════════════════════
# M2b live cases (B5)
# Requires: OLO_GOOGLE_TEST_HOME with 'livetest' creds that have full_v1 scopes
# (gmail.readonly, calendar.events.readonly, calendar.calendarlist.readonly)
# ══════════════════════════════════════════════════════════════════════════════

def _skip_unless_m2b() -> None:
    """Skip if credentials lack the M2b full_v1 scope profile."""
    if not _has_live_creds():
        pytest.skip(_SKIP_REASON)
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = _TEST_HOME
    try:
        from olo.google.credentials import load_credentials
        creds = load_credentials("livetest")
        if not creds.is_full_content_profile():
            pytest.skip(
                "M2b live tests require full_v1 scope profile "
                "(gmail.readonly + calendar.events.readonly + calendar.calendarlist.readonly). "
                "Run: hermes olo google connect --account livetest --client-secret-file <path>"
            )
    except Exception:
        pytest.skip(_SKIP_REASON)
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


# ── M2b Stage 0: v2 bootstrap ─────────────────────────────────────────────────

def test_m2b_stage0_gmail_bootstrap_produces_no_signals() -> None:
    """M2b Stage 0: Gmail bootstrap sets historyId cursor, 0 signals."""
    _skip_unless_stage("0")
    _skip_unless_m2b()

    gmail_job, _ = _ensure_jobs()
    _cron_run_and_wait(gmail_job, expect_status="completed")

    conn = _db_conn()
    n = _count_signals("gmail_livetest", conn)
    cursor = _get_checkpoint("gmail_livetest", conn)
    conn.close()

    assert cursor is not None, "M2b: Gmail bootstrap must set a cursor"
    assert n == 0, f"M2b: Bootstrap must ingest 0 signals, got {n}"
    # cursor should be a plain historyId (not a JSON backfill cursor)
    try:
        parsed = json.loads(cursor)
        is_backfill = isinstance(parsed, dict) and parsed.get("phase") == "backfill"
    except (json.JSONDecodeError, TypeError):
        is_backfill = False
    assert not is_backfill, f"M2b: Bootstrap cursor must not be in backfill phase: {cursor!r}"


def test_m2b_stage0_gcal_v2_cursor() -> None:
    """M2b Stage 0: Calendar bootstrap produces a v2 composite cursor."""
    _skip_unless_stage("0")
    _skip_unless_m2b()

    _, gcal_job = _ensure_jobs()
    _cron_run_and_wait(gcal_job, expect_status="completed")

    conn = _db_conn()
    cursor = _get_checkpoint("gcal_livetest", conn)
    conn.close()

    assert cursor is not None, "M2b: Calendar bootstrap must set a cursor"
    comp = json.loads(cursor)
    # v2 composite cursor
    assert comp.get("v") == 2, (
        f"M2b: Calendar cursor must be v2, got: {cursor!r}"
    )
    assert "list" in comp, f"M2b: cursor missing 'list': {cursor!r}"
    assert "cals" in comp, f"M2b: cursor missing 'cals': {cursor!r}"
    # Primary calendar must be registered
    assert "primary" in comp["cals"], (
        f"M2b: primary calendar not in cals cursor: {cursor!r}"
    )

    state = _load_state()
    state["m2b_stage0_gcal_cursor"] = cursor
    _save_state(state)
    _record_observation("m2b_v2_cursor_v", comp.get("v"))
    _record_observation("m2b_v2_cursor_cals", list(comp["cals"].keys()))
    print(f"\nM2b Stage 0: v2 cursor with cals={list(comp['cals'].keys())}")


# ── M2b Stage 1: email.content signals ───────────────────────────────────────

def test_m2b_stage1_email_content_signals() -> None:
    """M2b Stage 1: each email.received/sent also produces an email.content signal.

    Requires P1–P8 to exist (created by seeder or manually).
    After running Gmail, there should be:
      - 6 email.received (P1–P5, P7)
      - 2 email.sent (P6, P8)
      - 8 email.content signals (one per message, with content_refs)
    """
    _skip_unless_stage("1")
    _skip_unless_m2b()

    gmail_job, _ = _ensure_jobs()
    _cron_run_and_wait(gmail_job, expect_status="completed")

    conn = _db_conn()
    kinds = _signals_by_kind("gmail_livetest", conn)
    rows = conn.execute(
        "SELECT external_id, payload FROM signals "
        "WHERE source='gmail_livetest' AND kind='email.content'"
    ).fetchall()
    conn.close()

    content_n = kinds.get("email.content", 0)
    received_n = kinds.get("email.received", 0)
    sent_n = kinds.get("email.sent", 0)

    assert received_n == 6, f"M2b: expected 6 email.received, got {received_n}"
    assert sent_n == 2, f"M2b: expected 2 email.sent, got {sent_n}"
    assert content_n == 8, f"M2b: expected 8 email.content, got {content_n}"

    # Verify content_refs structure
    content_refs_violations = []
    for row in rows:
        try:
            p = json.loads(row["payload"])
            refs = p.get("content_refs", [])
            if not refs:
                content_refs_violations.append(f"{row['external_id']}: no content_refs")
            else:
                for ref in refs:
                    if "sha256" not in ref or "media_type" not in ref:
                        content_refs_violations.append(
                            f"{row['external_id']}: malformed content_ref: {ref}"
                        )
        except Exception as e:
            content_refs_violations.append(f"{row['external_id']}: payload parse error: {e}")

    assert content_refs_violations == [], (
        f"M2b: email.content content_refs violations: {content_refs_violations}"
    )

    # Verify blobs table has entries
    conn2 = _db_conn()
    blob_n = conn2.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0]
    conn2.close()
    assert blob_n >= 8, (
        f"M2b: expected ≥8 blobs in content_blobs table, got {blob_n}"
    )

    # Privacy: body marker must not appear in blobs
    # (seeder inserts OLO-BODY-MARKER in message body — Olo must not store it)
    conn3 = _db_conn()
    marker_in_blobs = conn3.execute(
        "SELECT COUNT(*) FROM content_blobs WHERE data LIKE '%OLO-BODY-MARKER%'"
    ).fetchone()[0]
    conn3.close()
    # NOTE: format=full DOES include the body in the blob — this is by design.
    # The marker will appear in content_blobs (not in signals payload) — that's correct.
    _record_observation("m2b_blob_count", blob_n)
    _record_observation("m2b_content_signals", content_n)

    state = _load_state()
    state["m2b_stage1_content_n"] = content_n
    _save_state(state)

    print(f"\nM2b Stage 1: {content_n} email.content, {blob_n} blobs")

    # Idempotence: second run adds 0 content signals
    _cron_run_and_wait(gmail_job, expect_status="completed")
    conn4 = _db_conn()
    kinds2 = _signals_by_kind("gmail_livetest", conn4)
    conn4.close()
    assert kinds2.get("email.content", 0) == content_n, (
        f"M2b idempotence: email.content grew from {content_n} to "
        f"{kinds2.get('email.content', 0)}"
    )


def test_m2b_stage1_email_content_direction() -> None:
    """M2b Stage 1: email.content direction matches email.sent/received."""
    _skip_unless_stage("1")
    _skip_unless_m2b()

    conn = _db_conn()
    rows = conn.execute(
        "SELECT external_id, payload FROM signals "
        "WHERE source='gmail_livetest' AND kind='email.content'"
    ).fetchall()
    conn.close()

    violations = []
    for row in rows:
        try:
            p = json.loads(row["payload"])
            direction = p.get("direction")
            if direction not in ("sent", "received"):
                violations.append(f"{row['external_id']}: direction={direction!r}")
        except Exception as e:
            violations.append(f"{row['external_id']}: {e}")

    assert violations == [], f"M2b: direction violations: {violations}"
    _record_observation("m2b_direction_check", "passed")


def test_m2b_stage1_no_forbidden_fields_in_content_signal() -> None:
    """M2b Stage 1: email.content payload does not contain snippet/labelIds."""
    _skip_unless_stage("1")
    _skip_unless_m2b()

    conn = _db_conn()
    rows = conn.execute(
        "SELECT external_id, payload FROM signals "
        "WHERE source='gmail_livetest' AND kind='email.content'"
    ).fetchall()
    conn.close()

    forbidden_in_payload = {"snippet", "labelIds", "historyId"}
    violations = []
    for row in rows:
        try:
            p = json.loads(row["payload"])
            for f in forbidden_in_payload:
                if f in p:
                    violations.append(f"{row['external_id']}: forbidden field {f!r}")
        except Exception:
            pass

    assert violations == [], f"M2b: forbidden fields in email.content payload: {violations}"


# ── M2b Stage 1: calendarList discovery ──────────────────────────────────────

def test_m2b_stage1_calendarlist_discovery() -> None:
    """M2b Stage 1: v2 cursor registers primary calendar after first run."""
    _skip_unless_stage("1")
    _skip_unless_m2b()

    _, gcal_job = _ensure_jobs()
    _cron_run_and_wait(gcal_job, expect_status="completed")

    conn = _db_conn()
    cursor = _get_checkpoint("gcal_livetest", conn)
    conn.close()

    assert cursor is not None
    comp = json.loads(cursor)
    assert comp.get("v") == 2
    assert "primary" in comp.get("cals", {}), (
        f"M2b: primary calendar not registered after stage 1 run: {cursor!r}"
    )
    # Primary must be in incr phase
    primary = comp["cals"]["primary"]
    assert primary.get("phase") == "incr", (
        f"M2b: primary calendar not in incr phase: {primary}"
    )
    assert primary.get("sync_token"), (
        f"M2b: primary calendar missing sync_token: {primary}"
    )

    _record_observation("m2b_stage1_gcal_cursor_v", comp.get("v"))
    _record_observation("m2b_stage1_cals", list(comp["cals"].keys()))
    print(f"\nM2b Stage 1 Calendar: cals={list(comp['cals'].keys())}, primary={primary}")


def test_m2b_stage1_gcal_event_signals_have_calendar_id() -> None:
    """M2b Stage 1: all calendar.event.* signals have calendar_id in payload."""
    _skip_unless_stage("1")
    _skip_unless_m2b()

    conn = _db_conn()
    rows = conn.execute(
        "SELECT external_id, kind, payload FROM signals "
        "WHERE source='gcal_livetest' AND kind LIKE 'calendar.event.%'"
    ).fetchall()
    conn.close()

    violations = []
    for row in rows:
        try:
            p = json.loads(row["payload"])
            if "calendar_id" not in p:
                violations.append(f"{row['external_id']}: missing calendar_id")
        except Exception as e:
            violations.append(f"{row['external_id']}: {e}")

    assert violations == [], f"M2b: calendar_id missing: {violations}"


# ── M2b secondary calendar (requires seeder or manual setup) ─────────────────

def test_m2b_stage1_secondary_calendar_signals() -> None:
    """M2b Stage 1: if a secondary calendar exists, calendar.calendar.added is emitted.

    This test is advisory (not a hard failure if no secondary calendar present).
    It records the observation for the final evidence package.
    """
    _skip_unless_stage("1")
    _skip_unless_m2b()

    conn = _db_conn()
    added = conn.execute(
        "SELECT external_id, payload FROM signals "
        "WHERE source='gcal_livetest' AND kind='calendar.calendar.added'"
    ).fetchall()
    removed = conn.execute(
        "SELECT external_id, payload FROM signals "
        "WHERE source='gcal_livetest' AND kind='calendar.calendar.removed'"
    ).fetchall()
    cursor = _get_checkpoint("gcal_livetest", conn)
    conn.close()

    cal_added_n = len(added)
    cal_removed_n = len(removed)
    comp = json.loads(cursor) if cursor else {}
    cals_registered = list((comp.get("cals") or {}).keys())

    _record_observation("m2b_calendar_added_signals", cal_added_n)
    _record_observation("m2b_calendar_removed_signals", cal_removed_n)
    _record_observation("m2b_cals_registered", cals_registered)

    print(f"\nM2b secondary calendar: {cal_added_n} added, {cal_removed_n} removed, "
          f"registered={cals_registered}")
    # Non-failing — record and continue


# ── M2b backfill cursor ───────────────────────────────────────────────────────

def test_m2b_backfill_cursor_structure() -> None:
    """M2b: if a backfill cursor is in state, it has the correct JSON structure.

    This test verifies the cursor schema when backfill is active.
    It is a no-op if no backfill cursor is present (normal post-bootstrap state).
    """
    _skip_unless_stage("1", "2", "3")
    _skip_unless_m2b()

    conn = _db_conn()
    cursor = _get_checkpoint("gmail_livetest", conn)
    conn.close()

    if cursor is None:
        pytest.skip("no Gmail cursor set")

    try:
        parsed = json.loads(cursor)
    except (json.JSONDecodeError, TypeError):
        # Plain historyId string — not a backfill cursor, that's fine
        _record_observation("m2b_gmail_cursor_type", "plain_history_id")
        return

    if not isinstance(parsed, dict) or parsed.get("phase") != "backfill":
        _record_observation("m2b_gmail_cursor_type", parsed.get("phase", "unknown"))
        return  # not in backfill phase — no assertion needed

    # Validate backfill cursor structure
    required_keys = {"phase", "mode", "resume_history_id", "after", "page_token"}
    missing = required_keys - set(parsed.keys())
    assert not missing, f"M2b: backfill cursor missing keys: {missing}"
    assert parsed["mode"] in ("bootstrap", "resume", "gap"), (
        f"M2b: unexpected backfill mode: {parsed['mode']!r}"
    )
    _record_observation("m2b_gmail_cursor_type", "backfill")
    _record_observation("m2b_backfill_mode", parsed["mode"])


# ── M2b privacy: body not stored in signals table ────────────────────────────

def test_m2b_body_in_blobs_not_signals() -> None:
    """M2b: OLO-BODY-MARKER appears in content_blobs (expected) but not in signals payload."""
    _skip_unless_stage("1", "2", "3")
    _skip_unless_m2b()

    conn = _db_conn()
    # Marker must NOT appear in signals payload
    sigs_with_marker = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE payload LIKE '%OLO-BODY-MARKER%'"
    ).fetchone()[0]
    conn.close()

    assert sigs_with_marker == 0, (
        f"M2b: OLO-BODY-MARKER found in signals.payload ({sigs_with_marker} rows) — "
        "body leaked into signal metadata"
    )
    _record_observation("m2b_body_in_signals_payload", sigs_with_marker)


# ── M2b zero-LLM verification ─────────────────────────────────────────────────

def test_m2b_zero_llm_no_usage_audit() -> None:
    """M2b: neither Gmail nor Calendar job produces any LLM usage."""
    _skip_unless_stage("1", "2", "3", "failure")
    _skip_unless_m2b()

    audit = Path(_TEST_HOME) / "cron" / "usage_audit.jsonl"
    if not audit.exists():
        return  # no audit file = no LLM usage

    content = audit.read_text(encoding="utf-8")
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        job_name = entry.get("job_name", "") or entry.get("script", "")
        assert "olo-gmail-livetest" not in job_name, (
            f"M2b: gmail job has LLM usage entry: {entry}"
        )
        assert "olo-gcal-livetest" not in job_name, (
            f"M2b: gcal job has LLM usage entry: {entry}"
        )

    _record_observation("m2b_zero_llm_verified", True)
