"""tests/olo/test_ledger_hardening.py — S1–S4 hardening: ledger fails safely.

Regression tests for the reproduced audit cases (baseline 7e0a56d324):

  L1  commit for a NEW key claimed an unrelated unnamed commitment
  L2  cancel for an UNKNOWN key with no date cancelled an unrelated commitment
  L3  dateless, keyless reaffirm wiped the scheduled date
  L4  reaffirm with a different date silently changed the schedule
  L5  a second same-date commit with no key merged into the first
  L6  a commit with no date was accepted as 'scheduled'

Plus: ambiguity stays unresolved, impossible dates are rejected, and replay
stays idempotent under the stricter matcher.
"""
from __future__ import annotations

import sqlite3

import pytest

from olo.ledger import (
    STATUS_CANCELLED,
    STATUS_SCHEDULED,
    LedgerEvent,
    OutcomeKind,
    apply_event,
    get_all_versions,
    get_current_state,
    is_valid_iso_date,
    migrate,
    replay_events,
)

pytestmark = pytest.mark.not_integration

T = "2026-01-01T00:00:00+00:00"


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    return conn


def _ev(**kw) -> LedgerEvent:
    base: dict = dict(
        event_type="delivery", contact_id="cid_a", business_key="",
        source_doc_id="d1", source_kind="email.sent",
        occurred_at="2026-04-01T00:00:00+00:00",
        actor_role="business", action="commit", confirmed_date="2026-04-10",
    )
    base.update(kw)
    return LedgerEvent(**base)


def _later(n: int) -> str:
    return f"2026-04-0{n + 1}T00:00:00+00:00"


def _count(db: sqlite3.Connection, table: str) -> int:
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _unresolved_sources(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute(
        "SELECT source_doc_id FROM unresolved_matches WHERE resolved_at IS NULL")}


# ── L1: unknown key must not claim an unrelated unnamed commitment ────────────

class TestL1UnknownKeyCommit:
    def test_new_key_with_different_date_creates_separate_commitment(self, db) -> None:
        first = apply_event(db, _ev(), recorded_at=T)
        second = apply_event(db, _ev(source_doc_id="d2", business_key="ORD-2",
                                     confirmed_date="2026-05-20", occurred_at=_later(1)),
                             recorded_at=T)
        assert second.outcome == OutcomeKind.CREATED
        assert second.commitment_id != first.commitment_id
        state = get_current_state(db, first.commitment_id)
        assert state["scheduled_date"] == "2026-04-10"
        assert state["business_key"] == ""
        assert len(get_all_versions(db, first.commitment_id)) == 1

    def test_new_key_with_same_date_is_ambiguous(self, db) -> None:
        first = apply_event(db, _ev(), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d2", business_key="ORD-2",
                                  occurred_at=_later(1)), recorded_at=T)
        assert res.outcome == OutcomeKind.UNRESOLVED
        assert _count(db, "commitments") == 1
        assert get_current_state(db, first.commitment_id)["business_key"] == ""
        assert "d2" in _unresolved_sources(db)

    def test_new_key_claims_unnamed_only_with_prior_date_evidence(self, db) -> None:
        first = apply_event(db, _ev(), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d2", business_key="ORD-2",
                                  action="confirm_change", confirmed_date="2026-04-15",
                                  prior_confirmed_date="2026-04-10", occurred_at=_later(1)),
                          recorded_at=T)
        assert res.outcome == OutcomeKind.UPDATED
        assert res.commitment_id == first.commitment_id
        assert get_current_state(db, first.commitment_id)["business_key"] == "ORD-2"

    def test_other_named_commitment_is_never_a_candidate(self, db) -> None:
        a = apply_event(db, _ev(business_key="ORD-1"), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d2", business_key="ORD-2",
                                  action="confirm_change", confirmed_date="2026-04-15",
                                  prior_confirmed_date="2026-04-10", occurred_at=_later(1)),
                          recorded_at=T)
        assert res.outcome == OutcomeKind.UNRESOLVED
        assert len(get_all_versions(db, a.commitment_id)) == 1


# ── L2 / L3: updates without trustworthy evidence stay unresolved ─────────────

class TestL2L3NoEvidence:
    def test_cancel_for_unknown_key_without_date_is_unresolved(self, db) -> None:
        first = apply_event(db, _ev(), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d2", business_key="ORD-9",
                                  action="cancel", confirmed_date=None,
                                  occurred_at=_later(1)), recorded_at=T)
        assert res.outcome == OutcomeKind.UNRESOLVED
        assert get_current_state(db, first.commitment_id)["status"] == STATUS_SCHEDULED
        assert "d2" in _unresolved_sources(db)

    @pytest.mark.parametrize("actor,action", [
        ("business", "cancel"), ("customer", "cancel"), ("customer", "request_change"),
    ])
    def test_keyless_dateless_threadless_update_is_unresolved(
        self, db, actor: str, action: str,
    ) -> None:
        first = apply_event(db, _ev(), recorded_at=T)
        extra = {"proposed_date": "2026-04-20"} if action == "request_change" else {}
        res = apply_event(db, _ev(source_doc_id="d2", actor_role=actor, action=action,
                                  confirmed_date=None, occurred_at=_later(1), **extra),
                          recorded_at=T)
        assert res.outcome == OutcomeKind.UNRESOLVED
        assert len(get_all_versions(db, first.commitment_id)) == 1

    def test_dateless_reaffirm_is_invalid(self, db) -> None:
        first = apply_event(db, _ev(), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d2", action="reaffirm",
                                  confirmed_date=None, occurred_at=_later(1)), recorded_at=T)
        assert res.outcome == OutcomeKind.INVALID
        assert get_current_state(db, first.commitment_id)["scheduled_date"] == "2026-04-10"

    def test_thread_is_trustworthy_evidence(self, db) -> None:
        first = apply_event(db, _ev(thread_id="t-1"), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d2", action="cancel", confirmed_date=None,
                                  thread_id="t-1", occurred_at=_later(1)), recorded_at=T)
        assert res.outcome == OutcomeKind.UPDATED and res.commitment_id == first.commitment_id
        assert get_current_state(db, first.commitment_id)["status"] == STATUS_CANCELLED


# ── L4: reaffirm cannot change the confirmed date ─────────────────────────────

class TestL4Reaffirm:
    def test_reaffirm_with_different_date_is_unresolved(self, db) -> None:
        first = apply_event(db, _ev(business_key="ORD-1"), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d2", business_key="ORD-1", action="reaffirm",
                                  confirmed_date="2026-06-30", occurred_at=_later(1)),
                          recorded_at=T)
        assert res.outcome == OutcomeKind.UNRESOLVED
        assert get_current_state(db, first.commitment_id)["scheduled_date"] == "2026-04-10"
        assert len(get_all_versions(db, first.commitment_id)) == 1

    def test_reaffirm_of_superseded_date_is_unresolved(self, db) -> None:
        first = apply_event(db, _ev(business_key="ORD-1"), recorded_at=T)
        apply_event(db, _ev(source_doc_id="d2", business_key="ORD-1", action="confirm_change",
                            confirmed_date="2026-04-20", prior_confirmed_date="2026-04-10",
                            occurred_at=_later(1)), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d3", action="reaffirm",
                                  confirmed_date="2026-04-10", occurred_at=_later(2)),
                          recorded_at=T)
        assert res.outcome == OutcomeKind.UNRESOLVED
        assert get_current_state(db, first.commitment_id)["scheduled_date"] == "2026-04-20"

    def test_reaffirm_of_cancelled_commitment_is_unresolved(self, db) -> None:
        first = apply_event(db, _ev(business_key="ORD-1"), recorded_at=T)
        apply_event(db, _ev(source_doc_id="d2", business_key="ORD-1", action="cancel",
                            confirmed_date=None, prior_confirmed_date="2026-04-10",
                            occurred_at=_later(1)), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d3", business_key="ORD-1", action="reaffirm",
                                  occurred_at=_later(2)), recorded_at=T)
        assert res.outcome == OutcomeKind.UNRESOLVED
        assert get_current_state(db, first.commitment_id)["status"] == STATUS_CANCELLED

    def test_matching_reaffirm_is_applied(self, db) -> None:
        first = apply_event(db, _ev(business_key="ORD-1"), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d2", business_key="ORD-1", action="reaffirm",
                                  occurred_at=_later(1)), recorded_at=T)
        assert res.outcome == OutcomeKind.UPDATED
        assert get_current_state(db, first.commitment_id)["scheduled_date"] == "2026-04-10"


# ── L5: a second same-date commitment never merges ────────────────────────────

class TestL5SameDateCommit:
    def test_second_same_date_commit_is_unresolved_not_merged(self, db) -> None:
        first = apply_event(db, _ev(), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d2", occurred_at=_later(1)), recorded_at=T)
        assert res.outcome == OutcomeKind.UNRESOLVED
        assert _count(db, "commitments") == 1
        assert len(get_all_versions(db, first.commitment_id)) == 1

    def test_same_date_commit_with_new_thread_is_unresolved(self, db) -> None:
        apply_event(db, _ev(thread_id="t-1"), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d2", thread_id="t-2",
                                  occurred_at=_later(1)), recorded_at=T)
        assert res.outcome == OutcomeKind.UNRESOLVED

    def test_commit_matching_a_superseded_date_links(self, db) -> None:
        """Out-of-order original: its date is another version's prior date."""
        trans = apply_event(db, _ev(source_doc_id="t", confirmed_date="2026-04-18",
                                    prior_confirmed_date="2026-04-10",
                                    occurred_at=_later(2)), recorded_at=T)
        email = apply_event(db, _ev(source_doc_id="e"), recorded_at=T)
        assert email.outcome == OutcomeKind.UPDATED
        assert email.commitment_id == trans.commitment_id

    def test_different_date_commit_creates(self, db) -> None:
        apply_event(db, _ev(), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d2", confirmed_date="2026-05-01",
                                  occurred_at=_later(1)), recorded_at=T)
        assert res.outcome == OutcomeKind.CREATED
        assert _count(db, "commitments") == 2


# ── L6: binding actions need a valid confirmed date ───────────────────────────

class TestL6DatedActions:
    @pytest.mark.parametrize("action", ["commit", "reaffirm", "confirm_change"])
    def test_missing_confirmed_date_is_invalid(self, db, action: str) -> None:
        res = apply_event(db, _ev(action=action, confirmed_date=None,
                                  prior_confirmed_date="2026-04-01"), recorded_at=T)
        assert res.outcome == OutcomeKind.INVALID
        assert _count(db, "commitments") == 0
        assert _count(db, "commitment_versions") == 0

    @pytest.mark.parametrize("field,bad", [
        ("confirmed_date", "2024-02-30"), ("confirmed_date", "2026-13-01"),
        ("confirmed_date", "April 10"), ("proposed_date", "2026/04/10"),
        ("prior_confirmed_date", "2026-04-31"),
    ])
    def test_invalid_calendar_dates_rejected(self, db, field: str, bad: str) -> None:
        kw = {field: bad}
        if field == "proposed_date":
            kw.update(actor_role="customer", action="request_change", confirmed_date=None)
        res = apply_event(db, _ev(**kw), recorded_at=T)
        assert res.outcome == OutcomeKind.INVALID
        assert field in res.message
        assert _count(db, "commitment_versions") == 0

    def test_shared_validator(self) -> None:
        assert is_valid_iso_date("2024-02-29")
        assert not is_valid_iso_date("2023-02-29")
        assert not is_valid_iso_date("20240229")
        assert not is_valid_iso_date(None)


# ── Ambiguity, idempotency and isolation preserved ────────────────────────────

class TestPreservedGuarantees:
    def test_two_candidates_stay_unresolved(self, db) -> None:
        apply_event(db, _ev(business_key="A"), recorded_at=T)
        apply_event(db, _ev(source_doc_id="d2", business_key="B"), recorded_at=T)
        res = apply_event(db, _ev(source_doc_id="d3", action="cancel", confirmed_date=None,
                                  prior_confirmed_date="2026-04-10", occurred_at=_later(1)),
                          recorded_at=T)
        assert res.outcome == OutcomeKind.UNRESOLVED

    def test_replay_is_idempotent(self) -> None:
        events = [
            _ev(),
            _ev(source_doc_id="d2", action="confirm_change", confirmed_date="2026-04-20",
                prior_confirmed_date="2026-04-10", occurred_at=_later(1)),
            _ev(source_doc_id="d3", action="cancel", confirmed_date=None,
                prior_confirmed_date="2026-04-20", occurred_at=_later(2)),
        ]
        conn = sqlite3.connect(":memory:")
        migrate(conn)
        first = replay_events(conn, events, recorded_at=T)
        again = replay_events(conn, events, recorded_at=T)
        assert [r.outcome for r in first] == [
            OutcomeKind.CREATED, OutcomeKind.UPDATED, OutcomeKind.UPDATED]
        assert all(r.outcome == OutcomeKind.UNCHANGED for r in again)
        assert _count(conn, "commitment_versions") == 3

    def test_unresolved_writes_no_version(self, db) -> None:
        apply_event(db, _ev(), recorded_at=T)
        before = _count(db, "commitment_versions")
        apply_event(db, _ev(source_doc_id="d2", occurred_at=_later(1)), recorded_at=T)
        assert _count(db, "commitment_versions") == before

    def test_other_customer_never_matched(self, db) -> None:
        apply_event(db, _ev(contact_id="cid_a"), recorded_at=T)
        res = apply_event(db, _ev(contact_id="cid_b", source_doc_id="d2", action="cancel",
                                  confirmed_date=None, prior_confirmed_date="2026-04-10"),
                          recorded_at=T)
        assert res.outcome == OutcomeKind.UNRESOLVED
