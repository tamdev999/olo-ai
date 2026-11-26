"""Keyless commitments that share a date (hostile audit, post-audit batch 3).

Two legitimate commitments without an order number can fall on the same
date.  The second is left unresolved (date alone cannot tell a restatement
from a second commitment); before this batch a later request in that second
commitment's own thread then matched the *first* commitment by date.  These
tests pin the safe behaviour: unresolved commitments are pending identities
that later events are checked against, nothing is guessed or dropped, and an
operator decision replays what waited.
"""
from __future__ import annotations

import sqlite3

import pytest

from olo.ledger import LedgerEvent, OutcomeKind, apply_event, get_current_state, migrate
from olo.operator import authorize, enroll, resolve_commitment
from olo.store import connect
from tests.olo.test_s5_pipeline import Pipeline, answer, content

NOW = "2026-11-26T12:00:00.000000Z"
ALICE, BOB = "cid_alice", "cid_bob"


@pytest.fixture()
def db(tmp_path) -> sqlite3.Connection:
    from olo.store import migrate as migrate_store
    conn = connect(tmp_path / "olo.db")
    migrate_store(conn)
    migrate(conn)
    return conn


def ev(doc: str, action: str, *, contact=ALICE, thread="", key="", confirmed=None,
       prior=None, proposed=None, actor=None, day=1) -> LedgerEvent:
    return LedgerEvent(
        event_type="delivery", contact_id=contact, business_key=key, source_doc_id=doc,
        source_kind="email.content", occurred_at=f"2024-03-{day:02d}T09:00:00+00:00",
        actor_role=actor or ("customer" if action == "request_change" else "business"),
        action=action, confirmed_date=confirmed, prior_confirmed_date=prior,
        proposed_date=proposed, thread_id=thread, notes=f"sig:{doc}")


def apply(db, event) -> OutcomeKind:
    out = apply_event(db, event, recorded_at=NOW).outcome
    db.commit()
    return out


def open_rows(db) -> dict[str, tuple]:
    return {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT source_doc_id, reason, blocked_by FROM unresolved_matches WHERE resolved_at IS NULL")}


def grant(db):
    enroll(db, "ops", "correct horse battery", now=NOW)
    return authorize(db, "ops", "correct horse battery", now=NOW)


class TestSameDateKeylessCommitments:
    def setup_two(self, db) -> str:
        assert apply(db, ev("d1", "commit", thread="t1", confirmed="2024-04-10")) == OutcomeKind.CREATED
        assert apply(db, ev("d2", "commit", thread="t2", confirmed="2024-04-10", day=2)) == OutcomeKind.UNRESOLVED
        return db.execute("SELECT id FROM commitments").fetchone()[0]

    def test_a_request_in_the_unresolved_commitments_thread_never_reaches_the_first(self, db) -> None:
        first = self.setup_two(db)
        out = apply(db, ev("d3", "request_change", thread="t2", prior="2024-04-10",
                           proposed="2024-04-12", day=3))
        assert out == OutcomeKind.UNRESOLVED
        assert get_current_state(db, first)["pending_proposed_date"] is None
        reason, blocked = open_rows(db)["d3"]
        assert blocked == '["d2"]' and "awaiting an operator" in reason

    def test_a_keyless_request_matched_by_date_alone_competes_with_both(self, db) -> None:
        first = self.setup_two(db)
        out = apply(db, ev("d3", "request_change", prior="2024-04-10", proposed="2024-04-12", day=3))
        assert out == OutcomeKind.UNRESOLVED
        assert get_current_state(db, first)["pending_proposed_date"] is None
        assert open_rows(db)["d3"][1] == '["d2"]'

    def test_a_request_anchored_to_the_first_commitments_thread_still_applies(self, db) -> None:
        first = self.setup_two(db)
        out = apply(db, ev("d3", "request_change", thread="t1", prior="2024-04-10",
                           proposed="2024-04-12", day=3))
        assert out == OutcomeKind.UPDATED
        assert get_current_state(db, first)["pending_proposed_date"] == "2024-04-12"

    def test_a_cancellation_by_date_alone_is_never_applied_to_one_of_them(self, db) -> None:
        first = self.setup_two(db)
        assert apply(db, ev("d3", "cancel", prior="2024-04-10", day=3)) == OutcomeKind.UNRESOLVED
        assert get_current_state(db, first)["status"] == "scheduled"
        assert apply(db, ev("d4", "cancel", thread="t1", prior="2024-04-10", day=4)) == OutcomeKind.UPDATED
        assert get_current_state(db, first)["status"] == "cancelled"

    def test_other_customers_are_never_affected(self, db) -> None:
        self.setup_two(db)
        assert apply(db, ev("b1", "commit", contact=BOB, confirmed="2024-04-10")) == OutcomeKind.CREATED
        assert apply(db, ev("b2", "request_change", contact=BOB, prior="2024-04-10",
                            proposed="2024-04-11", day=2)) == OutcomeKind.UPDATED

    def test_replay_is_idempotent_and_nothing_is_dropped(self, db) -> None:
        self.setup_two(db)
        events = [ev("d3", "request_change", thread="t2", prior="2024-04-10",
                     proposed="2024-04-12", day=3),
                  ev("d1", "commit", thread="t1", confirmed="2024-04-10"),
                  ev("d2", "commit", thread="t2", confirmed="2024-04-10", day=2)]
        for _ in range(3):
            for e in events:
                apply(db, e)
        assert db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM commitment_versions").fetchone()[0] == 1
        assert set(open_rows(db)) == {"d2", "d3"}
        assert db.execute("SELECT COUNT(*) FROM unresolved_matches").fetchone()[0] == 2

    def test_operator_distinct_replays_the_waiting_request_onto_the_new_commitment(self, db) -> None:
        first = self.setup_two(db)
        apply(db, ev("d3", "request_change", thread="t2", prior="2024-04-10",
                     proposed="2024-04-12", day=3))
        res = resolve_commitment(db, grant(db), "d2", "distinct", now=NOW,
                                 reason="second delivery for the warehouse")
        assert (res.outcome, res.replayed) == ("applied", ("d3",))
        second = res.commitment_id
        assert second != first
        assert get_current_state(db, second)["pending_proposed_date"] == "2024-04-12"
        assert get_current_state(db, first)["pending_proposed_date"] is None
        assert open_rows(db) == {}

    def test_operator_link_makes_the_thread_part_of_the_first_commitment(self, db) -> None:
        first = self.setup_two(db)
        apply(db, ev("d3", "request_change", thread="t2", prior="2024-04-10",
                     proposed="2024-04-12", day=3))
        res = resolve_commitment(db, grant(db), "d2", "link", target=first, now=NOW,
                                 reason="the same delivery confirmed twice")
        assert res.replayed == ("d3",)
        assert db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 1
        assert get_current_state(db, first)["pending_proposed_date"] == "2024-04-12"

    def test_dismissed_commitment_releases_date_only_requests_to_the_matcher(self, db) -> None:
        first = self.setup_two(db)
        apply(db, ev("d3", "request_change", prior="2024-04-10", proposed="2024-04-12", day=3))
        res = resolve_commitment(db, grant(db), "d2", "dismiss", now=NOW,
                                 reason="duplicate notification, not a commitment")
        assert res.replayed == ("d3",)
        assert get_current_state(db, first)["pending_proposed_date"] == "2024-04-12"


class TestProviderKeys:
    def test_a_provider_key_from_an_email_is_invalid(self, db) -> None:
        bad = ev("x1", "commit", key="gcal:primary:e1", confirmed="2024-04-10")
        assert apply(db, bad) == OutcomeKind.INVALID

    def test_a_calendar_event_never_claims_a_same_day_email_commitment(self, db) -> None:
        apply(db, ev("d1", "commit", confirmed="2024-04-10"))
        cal = LedgerEvent(
            event_type="delivery", contact_id=ALICE, business_key="gcal:primary:e1",
            source_doc_id="gcal:primary:e1@1", source_kind="calendar.event.changed",
            occurred_at="2024-03-02T09:00:00+00:00", actor_role="business", action="commit",
            confirmed_date="2024-04-10", thread_id="gcal:primary:e1")
        assert apply(db, cal) == OutcomeKind.CREATED
        assert db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 2


# ── Through the worker (extraction → ledger), Gmail-shaped messages ───────────

FIRST = "Your delivery is confirmed for 2024-04-10."
SECOND = "We also booked the warehouse delivery for 2024-04-10."
MOVE = "Could we move that delivery from 2024-04-10 to 2024-04-12?"
ROUTES = {
    FIRST: answer("commit", FIRST.rstrip("."), confirmed="2024-04-10"),
    SECOND: answer("commit", SECOND.rstrip("."), confirmed="2024-04-10"),
    MOVE: answer("request_change", MOVE.rstrip("?"), proposed="2024-04-12", prior="2024-04-10"),
}


def test_the_audit_scenario_through_the_worker(tmp_path) -> None:
    p = Pipeline(tmp_path, routes=ROUTES)
    p.run(content("m1", sent=True, body=FIRST, day=1, thread="t1"),
          content("m2", sent=True, body=SECOND, day=2, thread="t2"),
          content("m3", sent=False, body=MOVE, day=3, thread="t2"))
    [first] = p.commitments()
    assert p.state(first)["pending_proposed_date"] is None
    res = p.resolution()
    assert {s: res[s]["commitment_outcome"] for s in ("c:m1", "c:m2", "c:m3")} == {
        "c:m1": "applied", "c:m2": "ledger_unresolved", "c:m3": "ledger_unresolved"}
    conn = connect(p.db)
    g = grant(conn)
    out = resolve_commitment(conn, g, "gmail:m2", "distinct", now=NOW,
                             reason="a second, separate delivery")
    conn.close()
    assert out.replayed == ("gmail:m3",)
    second = out.commitment_id
    assert p.state(second)["pending_proposed_date"] == "2024-04-12"
    assert p.state(first)["pending_proposed_date"] is None
    res = p.resolution()
    assert (res["c:m2"]["commitment_outcome"], res["c:m3"]["commitment_outcome"]) == (
        "operator_distinct", "applied")
    assert res["c:m3"]["commitment_id"] == second
