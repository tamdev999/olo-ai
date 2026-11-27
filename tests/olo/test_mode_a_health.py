"""No-model (Mode A) activations are healthy (olo.commitments, olo.health).

Regression for the live Google acceptance of 2026-11-27 (E2): with
``--extract-max-calls 0`` every scheduled run recorded the commitment stage
as ``budget_exhausted``, so after two unattended hours health read
"degraded (85 consecutive)" although nothing had gone wrong.  A no-model
activation disables extraction on purpose: prose waits for a model
(``extraction_disabled``), calendar commitments are applied as usual, and
only an unexpected failure or an exhausted *positive* budget degrades.
"""
from __future__ import annotations

from datetime import UTC, datetime

from olo.activation import ActivationRequest, activate
from olo.adapters.gcal import _event_to_signal
from olo.adapters.gmail_full import build_email_content_signal
from olo.commitments import CommitmentStageConfig
from olo.extractor import Budget
from olo.health import assess
from olo.runner import run_source
from olo.store import connect, migrate
from tests.olo import gmail_fixtures as gf
from tests.olo.test_calendar_commitments import ALICE, OWNER, event
from tests.olo.test_calendar_email_linking import Business

NOW = "2026-11-27T10:35:00.000000Z"


def _email(msg_id: str, text: str, day: int) -> object:
    payload = gf.with_headers(gf.text_part(text), gf.message_headers(
        frm=f"Owner <{OWNER}>", to=f"Alice <{ALICE}>", subject="Visit", msg_id=msg_id))
    ms = str(int(datetime(2026, 11, day, 9, tzinfo=UTC).timestamp() * 1000))
    return build_email_content_signal("gmail_biz", gf.raw_message(
        msg_id, payload, labels=["SENT"], thread=f"t-{msg_id}", internal_ms=ms), "1")


def _health(b: Business, source: str) -> dict:
    return {r[0]: (r[1], r[2]) for r in b.rows(
        "SELECT stage, status, consecutive_failures FROM pipeline_health WHERE source=?", (source,))}


def _mode_a(b: Business):
    conn = connect(b.db)
    migrate(conn)
    try:
        return activate(ActivationRequest(business_id="biz", enable_commitments=True,
                                          extract_max_calls=0, own_emails=(OWNER,)), conn, NOW)
    finally:
        conn.close()


def test_scheduled_no_model_runs_stay_healthy_while_prose_waits(tmp_path) -> None:
    b = Business(tmp_path)
    act = _mode_a(b)
    assert act.commitment.extraction_enabled is False
    for n, day in enumerate((20, 21, 22)):                  # three scheduled runs of each job
        b.gmail.queue.append(_email(f"m{n}", f"Note {n}: see you at the visit.", day))
        r = run_source(b.gmail, db_path=b.db, commitment=act.commitment,
                       own_identifiers=act.own_identifiers)
        assert r.status == "succeeded" and r.degraded == (), r.degraded
        assert r.commitment == {"extraction_disabled": n + 1}       # every waiting email
        b.gcal.queue.append(_event_to_signal(
            event("e1", f"2027-01-{15 + n}", updated=f"2026-11-2{n}T10:00:00.000Z"),
            "gcal_biz", datetime(2026, 11, 26, tzinfo=UTC), "primary"))
        r = run_source(b.gcal, db_path=b.db, commitment=act.commitment,
                       own_identifiers=act.own_identifiers)
        assert r.status == "succeeded" and r.degraded == () and r.commitment.get("applied") == 1
        assert _health(b, "gmail_biz")["commitment"] == ("ok", 0)
        assert _health(b, "gcal_biz")["commitment"] == ("ok", 0)
    conn = connect(b.db)
    try:
        bad = [c for c in assess(conn, datetime(2026, 11, 27, 12, 36, tzinfo=UTC))
               if c.severity in ("degraded", "stalled") and c.stage != "gmail_biz/ingest"
               and c.stage != "gcal_biz/ingest"]
        assert bad == [], bad
    finally:
        conn.close()
    assert b.rows("SELECT DISTINCT commitment_status FROM signal_resolution sr JOIN signals s"
                  " ON s.external_id=sr.signal_id WHERE s.kind='email.content'") == [("pending",)]
    assert len(b.rows("SELECT * FROM commitment_versions")) == 3     # commit + two moves
    assert b.model.calls == []


def test_an_exhausted_positive_budget_still_degrades(tmp_path) -> None:
    b = Business(tmp_path)
    b.gmail.queue.extend([_email("m1", "First note.", 20), _email("m2", "Second note.", 21)])
    r = run_source(b.gmail, db_path=b.db, commitment=CommitmentStageConfig(
        model_fn=b.model, budget=Budget(max_calls=1)))
    assert r.commitment.get("budget_exhausted") == 1
    assert _health(b, "gmail_biz")["commitment"] == ("degraded", 1)
    assert any(d.startswith("commitment:") for d in r.degraded)

