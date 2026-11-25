"""Comprehensive tests for olo/ledger.py — S1 acceptance.

Implements the full test sequence from Opus's architecture review §4 and
all required variants:

  Full sequence (Apr 4 → proposed Apr 8 → reaffirmed Apr 4 → confirmed Apr 8)
  D1 — customer proposal never erases confirmed date
  D2 — confirm_change/request with no match → UNRESOLVED, not a fork
  Actor roles and actions
  Thread-based linking
  Out-of-order arrivals
  Missing prior date → UNRESOLVED (not a fork)
  Duplicate deliveries same date → UNRESOLVED
  Identity/business isolation (forged identity rejected at boundary)
  Authorised resolution (DenyAll default; explicit authorization required)
  Deterministic replay

All tests use injected events over in-memory SQLite — no LLM, no Hindsight.
"""
from __future__ import annotations

import sqlite3

import pytest

from olo.ledger import (
    ACTIONS,
    ACTOR_ROLES,
    AuthorizationError,
    LedgerEvent,
    OutcomeKind,
    apply_event,
    get_all_versions,
    get_current_state,
    get_pending_proposal,
    migrate,
    replay_events,
    resolve_unresolved_match,
)

# Isolate from global asyncio conftest (pure SQLite tests, no network/async)
pytestmark = pytest.mark.not_integration

# ── Constants ─────────────────────────────────────────────────────────────────

ALICE = "cid_64c1dd94041b33ab9245"
BOB   = "cid_8baee186a88a5a300f68"
TS    = "2026-01-01T00:00:00+00:00"


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    yield conn
    conn.close()


# ── Event factory ──────────────────────────────────────────────────────────────

def _ev(**kw) -> LedgerEvent:
    defaults: dict = dict(
        event_type="delivery",
        contact_id=ALICE,
        business_key="",
        source_doc_id="sig:default",
        source_kind="email.sent",
        occurred_at="2026-04-02T00:00:00+00:00",
        actor_role="business",
        action="commit",
        proposed_date=None,
        confirmed_date="2026-04-04",
        prior_confirmed_date=None,
        thread_id="",
        notes="",
    )
    defaults.update(kw)
    return LedgerEvent(**defaults)


# ═══════════════════════════════════════════════════════════════════════════════
# Full April sequence (Opus architecture review §4)
# ═══════════════════════════════════════════════════════════════════════════════

class TestFullAprilSequence:
    """The complete Apr 4 → proposed Apr 8 → reaffirmed Apr 4 → confirmed Apr 8
    sequence from the architecture review, plus the separate meeting.

    Expected ledger state after all four events:
      v1  confirmed Apr 4   (original business commitment)
      v2  proposed Apr 8    (customer request; confirmed state stays Apr 4)
      v3  confirmed Apr 4   (business reaffirmation; confirmed state stays Apr 4)
      v4  confirmed Apr 8   (call confirms change; final confirmed state = Apr 8)

    The meeting is a separate commitment.
    """

    def _apply_full_sequence(self, db: sqlite3.Connection) -> str:
        """Apply all four delivery events; return commitment_id."""
        v1 = apply_event(
            db,
            _ev(source_doc_id="sig:v1",
                actor_role="business", action="commit",
                confirmed_date="2026-04-04",
                occurred_at="2026-03-15T10:00:00+00:00"),
            recorded_at=TS,
        )
        apply_event(
            db,
            _ev(source_doc_id="sig:v2",
                source_kind="email.received",
                actor_role="customer", action="request_change",
                proposed_date="2026-04-08", confirmed_date=None,
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-01T09:00:00+00:00"),
            recorded_at=TS,
        )
        apply_event(
            db,
            _ev(source_doc_id="sig:v3",
                actor_role="business", action="reaffirm",
                confirmed_date="2026-04-04",
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-02T10:00:00+00:00"),
            recorded_at=TS,
        )
        apply_event(
            db,
            _ev(source_doc_id="sig:v4",
                source_kind="call.transcript",
                actor_role="business", action="confirm_change",
                confirmed_date="2026-04-08",
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-03T14:00:00+00:00"),
            recorded_at=TS,
        )
        db.commit()
        return v1.commitment_id

    def test_full_sequence_all_created_or_updated(
        self, db: sqlite3.Connection
    ) -> None:
        """All four events apply without UNRESOLVED or INVALID."""
        events = [
            _ev(source_doc_id="sig:v1", action="commit",
                confirmed_date="2026-04-04",
                occurred_at="2026-03-15T10:00:00+00:00"),
            _ev(source_doc_id="sig:v2", source_kind="email.received",
                actor_role="customer", action="request_change",
                proposed_date="2026-04-08", confirmed_date=None,
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-01T09:00:00+00:00"),
            _ev(source_doc_id="sig:v3", action="reaffirm",
                confirmed_date="2026-04-04",
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-02T10:00:00+00:00"),
            _ev(source_doc_id="sig:v4", source_kind="call.transcript",
                action="confirm_change",
                confirmed_date="2026-04-08",
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-03T14:00:00+00:00"),
        ]
        results = replay_events(db, events, recorded_at=TS)
        assert results[0].outcome == OutcomeKind.CREATED
        assert all(r.outcome == OutcomeKind.UPDATED for r in results[1:])
        assert all(r.commitment_id == results[0].commitment_id for r in results)

    def test_final_confirmed_date_is_apr8(self, db: sqlite3.Connection) -> None:
        """After all four events, the confirmed state must be April 8."""
        cid = self._apply_full_sequence(db)
        state = get_current_state(db, cid)
        assert state["confirmed_date"] == "2026-04-08"
        assert state["action"] == "confirm_change"
        assert state["is_confirmed"] is True

    def test_four_versions_in_history(self, db: sqlite3.Connection) -> None:
        """All four versions are preserved in append-only history."""
        cid = self._apply_full_sequence(db)
        versions = get_all_versions(db, cid)
        assert len(versions) == 4
        assert [v["action"] for v in versions] == [
            "commit", "request_change", "reaffirm", "confirm_change"
        ]
        assert [v["source_doc_id"] for v in versions] == [
            "sig:v1", "sig:v2", "sig:v3", "sig:v4"
        ]

    def test_confirmed_dates_in_history(self, db: sqlite3.Connection) -> None:
        """History contains original Apr 4, proposal Apr 8, reaffirm Apr 4, change Apr 8."""
        cid = self._apply_full_sequence(db)
        versions = get_all_versions(db, cid)
        assert versions[0]["confirmed_date"] == "2026-04-04"  # v1 commit
        assert versions[0]["is_confirmed"] is True
        assert versions[1]["confirmed_date"] is None           # v2 request (not confirmed)
        assert versions[1]["proposed_date"] == "2026-04-08"
        assert versions[1]["is_confirmed"] is False
        assert versions[2]["confirmed_date"] == "2026-04-04"  # v3 reaffirm
        assert versions[2]["is_confirmed"] is True
        assert versions[3]["confirmed_date"] == "2026-04-08"  # v4 confirm change
        assert versions[3]["is_confirmed"] is True

    def test_meeting_is_a_separate_commitment(
        self, db: sqlite3.Connection
    ) -> None:
        """A meeting on Apr 4 → Apr 8 must be a separate commitment record."""
        cid_del = self._apply_full_sequence(db)
        # Meeting: same dates, different event_type
        r_m1 = apply_event(
            db,
            _ev(event_type="meeting", source_doc_id="sig:mtg_v1",
                action="commit", confirmed_date="2026-04-04",
                occurred_at="2026-03-20T10:00:00+00:00"),
            recorded_at=TS,
        )
        apply_event(
            db,
            _ev(event_type="meeting", source_doc_id="sig:mtg_v2",
                source_kind="call.transcript",
                action="confirm_change",
                confirmed_date="2026-04-08",
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-04T15:00:00+00:00"),
            recorded_at=TS,
        )
        db.commit()

        assert r_m1.commitment_id != cid_del
        mtg_state = get_current_state(db, r_m1.commitment_id)
        del_state  = get_current_state(db, cid_del)
        assert mtg_state["confirmed_date"] == "2026-04-08"
        assert del_state["confirmed_date"]  == "2026-04-08"
        assert mtg_state["event_type"] == "meeting"
        assert del_state["event_type"]  == "delivery"

    def test_source_references_preserved(self, db: sqlite3.Connection) -> None:
        """Every version retains its source document ID and kind."""
        cid = self._apply_full_sequence(db)
        versions = get_all_versions(db, cid)
        assert versions[0]["source_kind"] == "email.sent"
        assert versions[1]["source_kind"] == "email.received"
        assert versions[3]["source_kind"] == "call.transcript"


# ═══════════════════════════════════════════════════════════════════════════════
# D1 — Proposal never erases confirmed date
# ═══════════════════════════════════════════════════════════════════════════════

class TestD1ProposalNeverErasesConfirmedDate:
    """D1 correctness: get_current_state always returns the latest *confirmed*
    version.  A customer proposal (is_confirmed=0) must not override it."""

    def test_confirmed_date_survives_customer_request(
        self, db: sqlite3.Connection
    ) -> None:
        r = apply_event(
            db, _ev(source_doc_id="sig:conf", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db,
            _ev(source_doc_id="sig:req",
                actor_role="customer", action="request_change",
                proposed_date="2026-04-08", confirmed_date=None,
                prior_confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        # D1: confirmed state must stay Apr 4
        state = get_current_state(db, r.commitment_id)
        assert state["confirmed_date"] == "2026-04-04"
        assert state["is_confirmed"] is True

    def test_pending_proposal_accessible_separately(
        self, db: sqlite3.Connection
    ) -> None:
        r = apply_event(
            db, _ev(source_doc_id="sig:conf", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db,
            _ev(source_doc_id="sig:req",
                actor_role="customer", action="request_change",
                proposed_date="2026-04-08", confirmed_date=None,
                prior_confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        pending = get_pending_proposal(db, r.commitment_id)
        assert pending is not None
        assert pending["proposed_date"] == "2026-04-08"
        assert pending["action"] == "request_change"
        assert pending["is_confirmed"] is False

    def test_reaffirmation_after_request_keeps_original_date(
        self, db: sqlite3.Connection
    ) -> None:
        events = [
            _ev(source_doc_id="sig:conf", confirmed_date="2026-04-04",
                occurred_at="2026-03-15T10:00:00+00:00"),
            _ev(source_doc_id="sig:req",
                actor_role="customer", action="request_change",
                proposed_date="2026-04-08", confirmed_date=None,
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-01T09:00:00+00:00"),
            _ev(source_doc_id="sig:reaffirm",
                action="reaffirm",
                confirmed_date="2026-04-04", prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-02T10:00:00+00:00"),
        ]
        results = replay_events(db, events, recorded_at=TS)
        state = get_current_state(db, results[0].commitment_id)
        assert state["confirmed_date"] == "2026-04-04"
        assert state["action"] == "reaffirm"

    def test_confirmed_date_after_full_sequence(
        self, db: sqlite3.Connection
    ) -> None:
        """After v1→v2→v3→v4, confirmed date = Apr 8 (from v4)."""
        events = [
            _ev(source_doc_id="sig:v1", confirmed_date="2026-04-04",
                occurred_at="2026-03-15T10:00:00+00:00"),
            _ev(source_doc_id="sig:v2", actor_role="customer",
                action="request_change", proposed_date="2026-04-08",
                confirmed_date=None, prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-01T09:00:00+00:00"),
            _ev(source_doc_id="sig:v3", action="reaffirm",
                confirmed_date="2026-04-04", prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-02T10:00:00+00:00"),
            _ev(source_doc_id="sig:v4", source_kind="call.transcript",
                action="confirm_change", confirmed_date="2026-04-08",
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-03T14:00:00+00:00"),
        ]
        results = replay_events(db, events, recorded_at=TS)
        state = get_current_state(db, results[0].commitment_id)
        assert state["confirmed_date"] == "2026-04-08"
        # No pending proposal after the confirmation
        assert state["pending_proposed_date"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# D2 — Non-commit actions with no match → UNRESOLVED, not a fork
# ═══════════════════════════════════════════════════════════════════════════════

class TestD2NoForkOnMissingEvidence:
    """D2 correctness: a confirm_change, reaffirm, request_change, or cancel
    with no matching commitment must return UNRESOLVED, not create a second
    commitment."""

    def test_confirm_change_with_no_prior_date_unresolved(
        self, db: sqlite3.Connection
    ) -> None:
        """Call transcript confirms Apr 8 but carries no prior date.
        There IS one delivery commitment (Apr 4) but date evidence is missing.
        D2: must stay UNRESOLVED, not fork a second commitment."""
        apply_event(
            db, _ev(source_doc_id="sig:orig", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        result = apply_event(
            db,
            _ev(source_doc_id="sig:call_no_prior",
                source_kind="call.transcript",
                action="confirm_change",
                confirmed_date="2026-04-08",
                prior_confirmed_date=None),  # no prior date extracted
            recorded_at=TS,
        )
        db.commit()

        assert result.outcome == OutcomeKind.UNRESOLVED
        assert db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 1

    def test_confirm_change_no_match_zero_commitments_unresolved(
        self, db: sqlite3.Connection
    ) -> None:
        """confirm_change with 0 existing commitments → UNRESOLVED (D2)."""
        result = apply_event(
            db,
            _ev(source_doc_id="sig:orphan",
                action="confirm_change",
                confirmed_date="2026-04-08",
                prior_confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        assert result.outcome == OutcomeKind.UNRESOLVED
        assert db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0

    def test_request_change_with_no_match_unresolved(
        self, db: sqlite3.Connection
    ) -> None:
        """Customer request with no matching commitment → UNRESOLVED (D2)."""
        result = apply_event(
            db,
            _ev(source_doc_id="sig:req_no_match",
                actor_role="customer", action="request_change",
                proposed_date="2026-04-08", confirmed_date=None),
            recorded_at=TS,
        )
        db.commit()

        assert result.outcome == OutcomeKind.UNRESOLVED

    def test_reaffirm_with_no_match_unresolved(
        self, db: sqlite3.Connection
    ) -> None:
        """Business reaffirmation with no matching commitment → UNRESOLVED (D2)."""
        result = apply_event(
            db,
            _ev(source_doc_id="sig:reaffirm_orphan",
                action="reaffirm",
                confirmed_date="2026-04-04",
                prior_confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        assert result.outcome == OutcomeKind.UNRESOLVED

    def test_cancel_with_no_match_unresolved(
        self, db: sqlite3.Connection
    ) -> None:
        """Cancel with no matching commitment → UNRESOLVED (D2)."""
        result = apply_event(
            db,
            _ev(source_doc_id="sig:cancel_orphan",
                action="cancel",
                confirmed_date=None),
            recorded_at=TS,
        )
        db.commit()

        assert result.outcome == OutcomeKind.UNRESOLVED

    def test_only_commit_can_create(self, db: sqlite3.Connection) -> None:
        """Verify that only 'commit' creates a new commitment when no match."""
        for action in ("reaffirm", "request_change", "confirm_change", "cancel"):
            r = apply_event(
                db,
                _ev(source_doc_id=f"sig:{action}",
                    actor_role="customer" if action == "request_change" else "business",
                    action=action,
                    confirmed_date=(
                        "2026-04-04" if action not in ("request_change", "cancel") else None
                    ),
                    # Cancellation contract: the cancelled date is the prior date.
                    prior_confirmed_date="2026-04-04" if action == "cancel" else None,
                    proposed_date="2026-04-08" if action == "request_change" else None),
                recorded_at=TS,
            )
            db.commit()
            assert r.outcome == OutcomeKind.UNRESOLVED, (
                f"action={action!r} with no match should be UNRESOLVED, got {r.outcome}"
            )
        assert db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Actor roles and actions
# ═══════════════════════════════════════════════════════════════════════════════

class TestActorRolesAndActions:
    """Validation of actor_role and action fields."""

    def test_invalid_actor_role_rejected(self, db: sqlite3.Connection) -> None:
        result = apply_event(
            db, _ev(actor_role="system"), recorded_at=TS
        )
        assert result.outcome == OutcomeKind.INVALID
        assert "actor_role" in result.message

    def test_invalid_action_rejected(self, db: sqlite3.Connection) -> None:
        result = apply_event(
            db, _ev(action="update"), recorded_at=TS
        )
        assert result.outcome == OutcomeKind.INVALID
        assert "action" in result.message

    def test_all_valid_actor_roles_accepted(
        self, db: sqlite3.Connection
    ) -> None:
        # business commit
        r1 = apply_event(
            db, _ev(source_doc_id="sig:b", actor_role="business",
                    action="commit", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        assert r1.outcome == OutcomeKind.CREATED

        # customer request_change
        r2 = apply_event(
            db, _ev(source_doc_id="sig:c", actor_role="customer",
                    action="request_change",
                    proposed_date="2026-04-08", confirmed_date=None,
                    prior_confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        assert r2.outcome == OutcomeKind.UPDATED

    def test_customer_cannot_commit(self, db: sqlite3.Connection) -> None:
        """customer + commit is rejected: a customer cannot make a business commitment."""
        result = apply_event(
            db, _ev(source_doc_id="sig:cust_commit",
                    actor_role="customer", action="commit",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        assert result.outcome == OutcomeKind.INVALID
        assert "actor_role" in result.message or "action" in result.message

    def test_customer_cannot_reaffirm(self, db: sqlite3.Connection) -> None:
        """customer + reaffirm is rejected: reaffirmation is a business action."""
        result = apply_event(
            db, _ev(source_doc_id="sig:cust_reaffirm",
                    actor_role="customer", action="reaffirm",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        assert result.outcome == OutcomeKind.INVALID

    def test_customer_cannot_confirm_change(
        self, db: sqlite3.Connection
    ) -> None:
        """customer + confirm_change is rejected: confirmation is a business action."""
        result = apply_event(
            db, _ev(source_doc_id="sig:cust_conf",
                    actor_role="customer", action="confirm_change",
                    confirmed_date="2026-04-08"),
            recorded_at=TS,
        )
        assert result.outcome == OutcomeKind.INVALID

    def test_business_cannot_request_change(
        self, db: sqlite3.Connection
    ) -> None:
        """business + request_change is rejected: requests come from customers."""
        result = apply_event(
            db, _ev(source_doc_id="sig:biz_req",
                    actor_role="business", action="request_change",
                    proposed_date="2026-04-08", confirmed_date=None),
            recorded_at=TS,
        )
        assert result.outcome == OutcomeKind.INVALID

    def test_invalid_combination_does_not_modify_state(
        self, db: sqlite3.Connection
    ) -> None:
        """An invalid actor/action combination leaves the DB unchanged."""
        # Create a valid commitment first
        r = apply_event(
            db, _ev(source_doc_id="sig:orig", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        # Try invalid combination
        apply_event(
            db, _ev(source_doc_id="sig:invalid",
                    actor_role="customer", action="commit",
                    confirmed_date="2026-04-08"),
            recorded_at=TS,
        )
        db.commit()

        # State unchanged
        state = get_current_state(db, r.commitment_id)
        assert state["confirmed_date"] == "2026-04-04"
        assert state["version"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Thread-based linking
# ═══════════════════════════════════════════════════════════════════════════════

class TestThreadLinking:
    """Thread ID provides a strong linking anchor without business_key."""

    def test_thread_links_email_to_transcript(
        self, db: sqlite3.Connection
    ) -> None:
        """Email and transcript in the same thread link to one commitment."""
        THREAD = "thread:order_april_2026"
        r1 = apply_event(
            db,
            _ev(source_doc_id="sig:email",
                action="commit", confirmed_date="2026-04-04",
                thread_id=THREAD),
            recorded_at=TS,
        )
        db.commit()
        r2 = apply_event(
            db,
            _ev(source_doc_id="sig:transcript",
                source_kind="call.transcript",
                action="confirm_change",
                confirmed_date="2026-04-08",
                prior_confirmed_date="2026-04-04",
                thread_id=THREAD),
            recorded_at=TS,
        )
        db.commit()

        assert r1.commitment_id == r2.commitment_id
        assert db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 1

    def test_different_threads_no_cross_link(
        self, db: sqlite3.Connection
    ) -> None:
        """Two events with different threads do not link to each other."""
        apply_event(
            db, _ev(source_doc_id="sig:t1", action="commit",
                    confirmed_date="2026-04-04", thread_id="thread:a"),
            recorded_at=TS,
        )
        db.commit()

        # Another event with a different thread and matching date:
        # thread doesn't match → falls through to date matching → 1 date match → UPDATE
        r2 = apply_event(
            db, _ev(source_doc_id="sig:t2", action="confirm_change",
                    confirmed_date="2026-04-08",
                    prior_confirmed_date="2026-04-04",
                    thread_id="thread:b"),
            recorded_at=TS,
        )
        db.commit()

        # Date match found (Apr 4 in history), so it links via date
        assert r2.outcome == OutcomeKind.UPDATED

    def test_thread_resolves_ambiguity_between_two_commitments(
        self, db: sqlite3.Connection
    ) -> None:
        """When two commitments have the same date but different threads,
        thread anchors the update to the right one."""
        # Two deliveries same date, different threads
        apply_event(
            db, _ev(source_doc_id="sig:order_a", business_key="ORD-A",
                    confirmed_date="2026-04-04", thread_id="thread:a"),
            recorded_at=TS,
        )
        r_b = apply_event(
            db, _ev(source_doc_id="sig:order_b", business_key="ORD-B",
                    confirmed_date="2026-04-04", thread_id="thread:b"),
            recorded_at=TS,
        )
        db.commit()

        # Transcript for thread:b (same date as both, but only matches ORD-B by thread)
        r_trans = apply_event(
            db,
            _ev(source_doc_id="sig:trans_b",
                source_kind="call.transcript",
                action="confirm_change",
                confirmed_date="2026-04-08",
                prior_confirmed_date="2026-04-04",
                thread_id="thread:b"),
            recorded_at=TS,
        )
        db.commit()

        assert r_trans.outcome == OutcomeKind.UPDATED
        assert r_trans.commitment_id == r_b.commitment_id


# ═══════════════════════════════════════════════════════════════════════════════
# Out-of-order arrival
# ═══════════════════════════════════════════════════════════════════════════════

class TestOutOfOrderArrival:
    """Events processed out of chronological order produce consistent state."""

    def test_transcript_before_email_same_final_state(self) -> None:
        """Normal order and reversed order produce the same confirmed date.

        Uses action='commit' for the transcript so it can CREATE a commitment
        when no prior exists (out-of-order scenario). confirm_change with no
        prior commitment would correctly go UNRESOLVED (tested separately in
        TestD2NoForkOnMissingEvidence).
        """

        def run(email_first: bool) -> str:
            conn = sqlite3.connect(":memory:")
            migrate(conn)
            email = _ev(source_doc_id="sig:email",
                        action="commit", confirmed_date="2026-04-04",
                        occurred_at="2026-04-02T10:00:00+00:00")
            trans = _ev(source_doc_id="sig:trans",
                        source_kind="call.transcript",
                        action="commit",           # commit allows creation when arriving first
                        confirmed_date="2026-04-08",
                        prior_confirmed_date="2026-04-04",
                        occurred_at="2026-04-03T14:00:00+00:00")
            seq = [email, trans] if email_first else [trans, email]
            results = replay_events(conn, seq, recorded_at=TS)
            cid = next(r.commitment_id for r in results if r.commitment_id)
            state = get_current_state(conn, cid)
            conn.close()
            return state["confirmed_date"]

        assert run(email_first=True) == run(email_first=False) == "2026-04-08"

    def test_out_of_order_all_versions_present(self) -> None:
        """Out-of-order arrival still records all events in history.
        Uses action='commit' for both so both can link regardless of order."""
        events = [
            _ev(source_doc_id="sig:v4", source_kind="call.transcript",
                action="commit", confirmed_date="2026-04-08",
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-03T14:00:00+00:00"),  # arrives first
            _ev(source_doc_id="sig:v1", action="commit",
                confirmed_date="2026-04-04",
                occurred_at="2026-03-15T10:00:00+00:00"),  # arrives second
        ]
        conn = sqlite3.connect(":memory:")
        migrate(conn)
        results = replay_events(conn, events, recorded_at=TS)
        cid = results[0].commitment_id
        versions = get_all_versions(conn, cid)
        conn.close()
        assert len(versions) == 2
        source_ids = {v["source_doc_id"] for v in versions}
        assert source_ids == {"sig:v4", "sig:v1"}

    def test_confirm_change_out_of_order_requires_resolution(
        self, db: sqlite3.Connection
    ) -> None:
        """When confirm_change arrives before the original commit, D2 fires.
        Once the commit lands, resolution is DenyAll — state stays at Apr 04."""
        # Transcript arrives first (out of order) with no prior commitment → D2
        r_trans = apply_event(
            db,
            _ev(source_doc_id="sig:trans",
                source_kind="call.transcript",
                action="confirm_change",
                confirmed_date="2026-04-08",
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-03T14:00:00+00:00"),
            recorded_at=TS,
        )
        db.commit()
        assert r_trans.outcome == OutcomeKind.UNRESOLVED  # D2: no commitment yet

        # Email arrives next (the original commit)
        r_email = apply_event(
            db,
            _ev(source_doc_id="sig:email",
                action="commit", confirmed_date="2026-04-04",
                occurred_at="2026-04-02T10:00:00+00:00"),
            recorded_at=TS,
        )
        db.commit()
        assert r_email.outcome == OutcomeKind.CREATED

        # Resolution attempt raises AuthorizationError (DenyAll)
        from olo.adapters.contacts import OperatorToken as OT
        with pytest.raises(AuthorizationError):
            resolve_unresolved_match(
                db, "sig:trans", r_email.commitment_id,
                operator=OT(actor="test-operator"),
            )
        db.commit()

        # Delivery commitment shows only the email's confirmed date
        state = get_current_state(db, r_email.commitment_id)
        assert state["confirmed_date"] == "2026-04-04"  # transcript not applied
        assert state["version"] == 1

    def test_missing_prior_date_unresolved_not_fork(
        self, db: sqlite3.Connection
    ) -> None:
        """Call transcript arrives with no prior date and there is one commitment.
        D2: must be UNRESOLVED (not create a second commitment)."""
        apply_event(
            db, _ev(source_doc_id="sig:orig", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        result = apply_event(
            db,
            _ev(source_doc_id="sig:trans_no_prior",
                source_kind="call.transcript",
                action="confirm_change",
                confirmed_date="2026-04-08",
                prior_confirmed_date=None),  # extractor missed prior date
            recorded_at=TS,
        )
        db.commit()

        assert result.outcome == OutcomeKind.UNRESOLVED
        # No fork: still exactly one commitment
        assert db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Duplicate deliveries same date → UNRESOLVED
# ═══════════════════════════════════════════════════════════════════════════════

class TestDuplicateDeliveriesUnresolved:
    """When two delivery commitments share the same date, an ambiguous update
    must be left unresolved rather than applied to either."""

    def test_two_same_date_commitments_ambiguous_update(
        self, db: sqlite3.Connection
    ) -> None:
        apply_event(
            db, _ev(source_doc_id="sig:o1", business_key="ORD-A",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        apply_event(
            db, _ev(source_doc_id="sig:o2", business_key="ORD-B",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        result = apply_event(
            db,
            _ev(source_doc_id="sig:ambig",
                source_kind="call.transcript",
                action="confirm_change",
                confirmed_date="2026-04-08",
                prior_confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        assert result.outcome == OutcomeKind.UNRESOLVED
        # Neither commitment was modified
        for key in ("ORD-A", "ORD-B"):
            row = db.execute(
                "SELECT id FROM commitments WHERE business_key=?", (key,)
            ).fetchone()
            assert get_current_state(db, row[0])["version"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Identity and business isolation
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdentityAndBusinessIsolation:
    """Forged or contested identity is rejected at the boundary.  Different
    customers and different event types never cross-contaminate."""

    def test_non_cid_contact_id_rejected(
        self, db: sqlite3.Connection
    ) -> None:
        """ULID generator entity IDs are rejected."""
        result = apply_event(
            db, _ev(contact_id="ent_01ACME000000000000000000"),
            recorded_at=TS,
        )
        assert result.outcome == OutcomeKind.INVALID
        assert "cid_" in result.message

    def test_empty_contact_id_rejected(self, db: sqlite3.Connection) -> None:
        """Empty contact_id (identity not resolved) is rejected."""
        result = apply_event(db, _ev(contact_id=""), recorded_at=TS)
        assert result.outcome == OutcomeKind.INVALID

    def test_different_customers_isolated(
        self, db: sqlite3.Connection
    ) -> None:
        """Alice's and Bob's deliveries are completely independent."""
        r_alice = apply_event(
            db, _ev(contact_id=ALICE, source_doc_id="sig:alice",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db, _ev(contact_id=BOB, source_doc_id="sig:bob",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        apply_event(
            db, _ev(contact_id=BOB, source_doc_id="sig:bob_update",
                    source_kind="call.transcript",
                    action="confirm_change",
                    confirmed_date="2026-04-10",
                    prior_confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        alice_state = get_current_state(db, r_alice.commitment_id)
        assert alice_state["confirmed_date"] == "2026-04-04"
        assert alice_state["version"] == 1  # untouched

    def test_delivery_and_meeting_never_cross(
        self, db: sqlite3.Connection
    ) -> None:
        """A meeting transcript never updates a delivery commitment."""
        r_del = apply_event(
            db, _ev(event_type="delivery",
                    source_doc_id="sig:del", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db, _ev(event_type="meeting",
                    source_doc_id="sig:mtg_trans",
                    source_kind="call.transcript",
                    action="confirm_change",
                    confirmed_date="2026-04-08",
                    prior_confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        del_state = get_current_state(db, r_del.commitment_id)
        assert del_state["confirmed_date"] == "2026-04-04"
        assert del_state["version"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Authorised resolution of unresolved matches (DenyAll — always raises)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuthorisedResolution:
    """Resolution is DenyAll until a trusted IAM/RBAC integration is wired.

    No caller can currently authorize a commitment resolution by:
    - supplying operator=None (raises AuthorizationError)
    - supplying a plain string (wrong type → raises)
    - constructing OperatorToken(actor=...) directly (DenyAll enforcement
      inside resolve_unresolved_match, not in the token type)

    create_operator_token() — the only legitimate path to an OperatorToken —
    always raises IdentityResolutionError until IAM is wired.

    Unresolved records must remain unchanged after every failed attempt.
    """

    def _make_unresolved(self, db: sqlite3.Connection):
        """Return (source_doc_id, commitment_id) for an unresolved entry."""
        apply_event(
            db, _ev(source_doc_id="sig:orig", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        apply_event(
            db,
            _ev(source_doc_id="sig:unresolved",
                source_kind="call.transcript",
                action="confirm_change",
                confirmed_date="2026-04-08",
                prior_confirmed_date=None),
            recorded_at=TS,
        )
        db.commit()
        cmt_id = db.execute("SELECT id FROM commitments").fetchone()[0]
        return "sig:unresolved", cmt_id

    def test_none_operator_raises(self, db: sqlite3.Connection) -> None:
        """operator=None raises AuthorizationError."""
        source_doc_id, cmt_id = self._make_unresolved(db)
        with pytest.raises(AuthorizationError):
            resolve_unresolved_match(db, source_doc_id, cmt_id, operator=None)

    def test_string_operator_raises(self, db: sqlite3.Connection) -> None:
        """A plain string cannot authorize resolution — wrong type."""
        source_doc_id, cmt_id = self._make_unresolved(db)
        with pytest.raises((AuthorizationError, TypeError)):
            resolve_unresolved_match(
                db, source_doc_id, cmt_id,
                operator="test-operator",  # type: ignore[arg-type]
            )

    def test_integer_operator_raises(self, db: sqlite3.Connection) -> None:
        """Any non-OperatorToken type is rejected."""
        source_doc_id, cmt_id = self._make_unresolved(db)
        with pytest.raises((AuthorizationError, TypeError)):
            resolve_unresolved_match(
                db, source_doc_id, cmt_id,
                operator=42,  # type: ignore[arg-type]
            )

    def test_direct_token_construction_cannot_authorize(
        self, db: sqlite3.Connection
    ) -> None:
        """OperatorToken(actor='...') constructed directly cannot authorize.

        The DenyAll enforcement is in resolve_unresolved_match, not in the
        token type.  Direct construction bypasses create_operator_token() but
        cannot bypass the function-level DenyAll check.
        """
        from olo.adapters.contacts import OperatorToken as OT
        source_doc_id, cmt_id = self._make_unresolved(db)
        fake_token = OT(actor="operator")
        with pytest.raises(AuthorizationError):
            resolve_unresolved_match(
                db, source_doc_id, cmt_id, operator=fake_token
            )

    def test_create_operator_token_always_raises(self) -> None:
        """create_operator_token() always raises (DenyAll integration point)."""
        from olo.adapters.contacts import create_operator_token, IdentityResolutionError
        with pytest.raises(IdentityResolutionError):
            create_operator_token("anyone")

    def test_unresolved_record_unchanged_after_every_forged_attempt(
        self, db: sqlite3.Connection
    ) -> None:
        """Every forged attempt leaves the unresolved_matches row open."""
        from olo.adapters.contacts import OperatorToken as OT
        source_doc_id, cmt_id = self._make_unresolved(db)

        for bad_op in [None, "fake", 42, OT(actor="forged")]:
            try:
                resolve_unresolved_match(
                    db, source_doc_id, cmt_id,
                    operator=bad_op,  # type: ignore[arg-type]
                )
            except (AuthorizationError, TypeError):
                pass
        db.commit()

        row = db.execute(
            "SELECT resolved_at FROM unresolved_matches WHERE source_doc_id=?",
            (source_doc_id,),
        ).fetchone()
        assert row is not None
        assert row[0] is None  # resolved_at must still be NULL

    def test_commitment_state_unchanged_after_forged_resolution(
        self, db: sqlite3.Connection
    ) -> None:
        """Commitment keeps its confirmed date after every failed resolution."""
        from olo.adapters.contacts import OperatorToken as OT
        source_doc_id, cmt_id = self._make_unresolved(db)

        try:
            resolve_unresolved_match(
                db, source_doc_id, cmt_id, operator=OT(actor="forged")
            )
        except AuthorizationError:
            pass
        db.commit()

        state = get_current_state(db, cmt_id)
        assert state["confirmed_date"] == "2026-04-04"


# ═══════════════════════════════════════════════════════════════════════════════
# Deterministic replay and rebuild
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeterministicReplay:
    """Same event sequence applied to identical empty DBs produces identical state."""

    @staticmethod
    def _run_full_sequence() -> dict:
        conn = sqlite3.connect(":memory:")
        migrate(conn)
        events = [
            _ev(source_doc_id="sig:v1", confirmed_date="2026-04-04",
                occurred_at="2026-03-15T10:00:00+00:00"),
            _ev(source_doc_id="sig:v2", actor_role="customer",
                action="request_change",
                proposed_date="2026-04-08", confirmed_date=None,
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-01T09:00:00+00:00"),
            _ev(source_doc_id="sig:v3", action="reaffirm",
                confirmed_date="2026-04-04", prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-02T10:00:00+00:00"),
            _ev(source_doc_id="sig:v4", source_kind="call.transcript",
                action="confirm_change", confirmed_date="2026-04-08",
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-03T14:00:00+00:00"),
        ]
        results = replay_events(conn, events, recorded_at=TS)
        cid = results[0].commitment_id
        state    = get_current_state(conn, cid)
        versions = get_all_versions(conn, cid)
        conn.close()
        return {"confirmed_date": state["confirmed_date"], "v_count": len(versions)}

    def test_three_replays_identical(self) -> None:
        states = [self._run_full_sequence() for _ in range(3)]
        for s in states[1:]:
            assert s["confirmed_date"] == states[0]["confirmed_date"]
            assert s["v_count"]        == states[0]["v_count"]

    def test_outcome_sequence_correct(self) -> None:
        conn = sqlite3.connect(":memory:")
        migrate(conn)
        events = [
            _ev(source_doc_id="sig:v1", confirmed_date="2026-04-04",
                occurred_at="2026-03-15T10:00:00+00:00"),
            _ev(source_doc_id="sig:v2", actor_role="customer",
                action="request_change",
                proposed_date="2026-04-08", confirmed_date=None,
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-01T09:00:00+00:00"),
            _ev(source_doc_id="sig:v3", action="reaffirm",
                confirmed_date="2026-04-04", prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-02T10:00:00+00:00"),
            _ev(source_doc_id="sig:v4", source_kind="call.transcript",
                action="confirm_change", confirmed_date="2026-04-08",
                prior_confirmed_date="2026-04-04",
                occurred_at="2026-04-03T14:00:00+00:00"),
        ]
        results = replay_events(conn, events, recorded_at=TS)
        conn.close()

        assert results[0].outcome == OutcomeKind.CREATED
        assert results[1].outcome == OutcomeKind.UPDATED  # request accepted
        assert results[2].outcome == OutcomeKind.UPDATED  # reaffirm accepted
        assert results[3].outcome == OutcomeKind.UPDATED  # confirm_change accepted
