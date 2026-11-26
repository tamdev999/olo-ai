"""Hindsight recovery through the real worker (post-audit batch 4).

Disposable failure scenarios against the in-process Hindsight fake: a lost
bank, a restarted server, a partial retain and a cancelled retain that wrote
its documents anyway.  olo.db stays the authority; recovery resubmits what
is missing under the same document ids (no duplicated history) and never
resubmits what the server already holds.
"""
from __future__ import annotations


from olo.store import connect
from tests.olo.test_s5_pipeline import (
    CANCEL_BODY,
    COMMIT_BODY,
    CONFIRM_BODY,
    REQUEST_BODY,
    Pipeline,
    content,
)

BANK = "olo-biz"


def _three(p: Pipeline):
    return p.run(content("m1", sent=True, body=COMMIT_BODY, day=1),
                 content("m2", sent=False, body=REQUEST_BODY, day=2),
                 content("m3", sent=True, body=CONFIRM_BODY, day=3))


def _desired(p: Pipeline) -> set[str]:
    from olo.projection import desired_documents
    conn = connect(p.db)
    try:
        return {d["document_id"] for d in desired_documents(conn, "gmail_biz")}
    finally:
        conn.close()


def _rows(p: Pipeline, sql: str, params: tuple = ()) -> list[tuple]:
    conn = connect(p.db)
    try:
        return [tuple(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _restart_recorded(p: Pipeline) -> None:
    """What olo.hindsight_service records when it restarts the server."""
    from olo.hindsight_service import migrate
    conn = connect(p.db)
    migrate(conn)
    conn.execute("INSERT INTO hindsight_service_events (at, event, detail)"
                 " VALUES ('2999-01-01T00:00:00+00:00', 'restarted', 'test')")
    conn.commit()
    conn.close()


def test_a_lost_bank_is_rebuilt_from_sqlite_without_duplicates(tmp_path) -> None:
    p = Pipeline(tmp_path)
    _three(p)
    before = set(p.hindsight.docs(BANK))
    originals = {d: dict(i) for d, i in p.hindsight.docs(BANK).items()}
    assert before == _desired(p)
    versions = _rows(p, "SELECT COUNT(*) FROM commitment_versions")
    p.hindsight.drop_bank(BANK)
    r = p.run()
    assert r.projection["bank_recreated"] == len(before)
    assert r.degraded                              # the run says the bank was lost
    p.run()                                        # resubmitted within the usual caps
    assert set(p.hindsight.docs(BANK)) == before
    # Same content, metadata and provenance (supporting documents, checksums).
    assert {d: dict(i) for d, i in p.hindsight.docs(BANK).items()} == originals
    assert _rows(p, "SELECT COUNT(*) FROM commitment_versions") == versions
    assert _rows(p, "SELECT status, COUNT(*) FROM hindsight_projection GROUP BY status") == [
        ("confirmed", len(before))]


def test_a_partial_retain_is_found_and_resubmitted(tmp_path) -> None:
    p = Pipeline(tmp_path)
    _three(p)
    lost = min(p.hindsight.docs(BANK))
    p.hindsight.lose(BANK, lost)                   # completed, but never persisted
    _restart_recorded(p)                           # makes a reconciliation due now
    r = p.run()
    assert r.projection["reconcile_missing"] == 1
    assert lost in p.hindsight.docs(BANK)
    [(trigger, status)] = _rows(p, "SELECT trigger, status FROM hindsight_reconciliations"
                                   " ORDER BY id DESC LIMIT 1")
    assert (trigger, status) == ("restart", "recovered")


def test_a_restarted_server_loses_in_flight_work_that_is_then_resubmitted(tmp_path) -> None:
    p = Pipeline(tmp_path)
    p.hindsight.next_status = "processing"         # still running when the server dies
    _three(p)
    p.hindsight.forget_operations()
    p.hindsight.next_status = "completed"
    r = p.run()
    assert r.projection["lost"] > 0
    p.run()
    assert _rows(p, "SELECT DISTINCT status FROM hindsight_projection") == [("confirmed",)]
    assert set(p.hindsight.docs(BANK)) == _desired(p)


def test_a_cancelled_retain_that_wrote_its_documents_is_replaced_in_place(tmp_path) -> None:
    """Presence after a cancel does not prove extraction finished (Hindsight
    writes the document row first): it is reported and resubmitted under the
    same ids, so the bank still holds each document exactly once."""
    p = Pipeline(tmp_path)
    p.hindsight.next_status = "cancelled"          # cooperative cancel after writing
    _three(p)
    held = set(p.hindsight.docs(BANK))
    assert _rows(p, "SELECT DISTINCT status FROM hindsight_projection") == [("failed",)]
    p.hindsight.next_status = "completed"
    r = p.run()
    assert r.projection["reconcile_present_unconfirmed"] == len(held)
    assert r.projection["submitted"] == len(held)
    assert set(p.hindsight.docs(BANK)) == held     # replaced, never duplicated
    assert _rows(p, "SELECT DISTINCT status FROM hindsight_projection") == [("confirmed",)]


def test_a_stale_copy_is_resubmitted_not_confirmed(tmp_path) -> None:
    """A ledger document whose server copy still says CURRENT after a newer
    version superseded it (an older write landed last) is drift."""
    p = Pipeline(tmp_path)
    p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
    [v1] = [d for d in p.hindsight.docs(BANK) if d.startswith("ledger:")]
    stale = dict(p.hindsight.docs(BANK)[v1])
    p.run(content("m3", sent=True, body=CONFIRM_BODY, day=3))
    p.hindsight.retained[BANK].append(stale)       # the old copy wins the race
    _restart_recorded(p)
    r = p.run()
    assert r.projection["reconcile_drifted"] == 1
    assert "ledger:superseded" in p.hindsight.docs(BANK)[v1]["tags"]


def test_permanent_failures_stay_visible_until_requeued(tmp_path) -> None:
    from olo.health import processing
    from olo.requeue import requeue_projection
    p = Pipeline(tmp_path)
    p.hindsight.raise_on_retain = True             # every submission refused
    _three(p)
    for _ in range(3):
        p.run()
    conn = connect(p.db)
    stuck = processing(conn)
    assert stuck["permanent"]["projection_retries_exhausted"] > 0
    assert requeue_projection(conn, BANK, "2999-01-01T00:00:00+00:00") == \
        stuck["permanent"]["projection_retries_exhausted"]
    after = processing(conn)
    conn.close()
    assert after["permanent"]["projection_retries_exhausted"] == 0
    assert after["incomplete"]["projection_unconfirmed"] > 0
    p.hindsight.raise_on_retain = False
    p.run()
    assert set(p.hindsight.docs(BANK)) >= _desired(p)


def test_unresolved_and_ambiguous_work_is_reported_as_awaiting_an_operator(tmp_path) -> None:
    from olo.health import processing
    p = Pipeline(tmp_path)
    p.run(content("m2", sent=False, body=REQUEST_BODY, day=2),      # no commitment to change
          content("m9", sent=True, body=CANCEL_BODY, day=3,
                  to="Alice <alice@x.com>, Bob <bob@y.com>"))         # two customers
    conn = connect(p.db)
    waiting = processing(conn)["awaiting_operator"]
    conn.close()
    assert waiting["ledger_unresolved"] == 1 and waiting["identity_ambiguous"] == 1


def test_reconciliation_reads_only_this_business_bank(tmp_path) -> None:
    a, b = Pipeline(tmp_path, "a"), Pipeline(tmp_path, "b")
    b.hindsight = a.hindsight                       # one server, two banks
    _three(a)
    b.run(content("x1", sent=True, body=COMMIT_BODY, day=1))
    _restart_recorded(a)
    r = a.run()
    assert r.projection["reconcile_unexpected"] == 0
    assert r.projection["reconcile_missing"] == 0


def test_a_document_no_longer_wanted_is_retired_not_left_unconfirmed(tmp_path) -> None:
    """Reconciliation only redoes what olo.db still wants; an absent document
    it no longer projects never shows up as unfinished work."""
    from olo.health import processing
    p = Pipeline(tmp_path)
    _three(p)
    conn = connect(p.db)
    conn.execute("INSERT INTO hindsight_projection (signal_id, bank_id, content_sha256,"
                 " operation_id, status, confirmed_at) VALUES ('sig:gone', ?, 'x', 'op-x',"
                 " 'confirmed', '2026-01-01T00:00:00Z')", (BANK,))
    conn.commit()
    conn.close()
    _restart_recorded(p)
    r = p.run()
    assert r.projection["reconcile_missing"] == 0
    conn = connect(p.db)
    assert processing(conn)["incomplete"]["projection_unconfirmed"] == 0
    [(report,)] = conn.execute("SELECT report FROM hindsight_reconciliations ORDER BY id DESC"
                               " LIMIT 1").fetchall()
    conn.close()
    assert '"retired": 1' in report
