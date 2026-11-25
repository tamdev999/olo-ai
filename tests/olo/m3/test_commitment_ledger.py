"""Deterministic unit tests for olo/m3/commitment_ledger.py.

Covers U1–U11 from the Opus architecture review.
All tests use injected events over in-memory SQLite — no LLM, no Hindsight,
no network, no file I/O.

U10 specifically verifies cross-channel linking without a shared business key,
using the ``prior_confirmed_date`` field as verified date evidence.  Multiple
unnamed commitments may now coexist for the same customer; the matcher only
merges when evidence is unambiguous.
"""
from __future__ import annotations

import sqlite3

import pytest

from olo.ledger import (
    ApplyResult,
    LedgerEvent,
    OutcomeKind,
    apply_event,
    get_all_versions,
    get_current_state,
    get_pending_proposal,
    migrate,
    replay_events,
)

# ── Test constants ─────────────────────────────────────────────────────────────

ALICE = "cid_64c1dd94041b33ab9245"   # Alice Chen / Acme Corp
BOB   = "cid_8baee186a88a5a300f68"   # Bob Torres / Beta Tech
TS    = "2026-01-01T00:00:00+00:00"  # fixed recorded_at for determinism


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def db() -> sqlite3.Connection:
    """In-memory SQLite with fresh ledger schema per test."""
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    yield conn
    conn.close()


# ── Event factory ──────────────────────────────────────────────────────────────

def _ev(**kw) -> LedgerEvent:
    """Build a LedgerEvent with sensible defaults; override via keyword args."""
    defaults: dict = dict(
        event_type="delivery",
        contact_id=ALICE,
        business_key="",
        source_doc_id="sig:test_default",
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
# U1 — Initial confirmed commitment
# ═══════════════════════════════════════════════════════════════════════════════

class TestU1InitialConfirmedCommitment:
    """U1: A confirmed email creates a new delivery commitment at version 1."""

    def test_outcome_is_created(self, db: sqlite3.Connection) -> None:
        result = apply_event(
            db, _ev(source_doc_id="sig:conf_orig", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        assert result.outcome == OutcomeKind.CREATED
        assert result.version == 1
        assert result.commitment_id is not None

    def test_state_reflects_confirmed_date(self, db: sqlite3.Connection) -> None:
        result = apply_event(
            db, _ev(source_doc_id="sig:conf_orig", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        state = get_current_state(db, result.commitment_id)
        assert state["confirmed_date"] == "2026-04-04"
        assert state["is_confirmed"] is True
        assert state["version"] == 1
        assert state["contact_id"] == ALICE
        assert state["event_type"] == "delivery"


# ═══════════════════════════════════════════════════════════════════════════════
# U2 — Customer request is a proposal
# ═══════════════════════════════════════════════════════════════════════════════

class TestU2CustomerRequestIsProposal:
    """U2: A customer reschedule request records a proposed_date, not a
    confirmed change."""

    def test_request_records_proposal_not_confirmation(
        self, db: sqlite3.Connection
    ) -> None:
        r_conf = apply_event(
            db,
            _ev(source_doc_id="sig:orig", confirmed_date="2026-04-04",
                actor_role="business", action="commit"),
            recorded_at=TS,
        )
        db.commit()
        r_req = apply_event(
            db,
            _ev(
                source_doc_id="sig:request",
                source_kind="email.received",
                proposed_date="2026-04-08",
                confirmed_date=None,
                prior_confirmed_date="2026-04-04",
                actor_role="customer", action="request_change",
            ),
            recorded_at=TS,
        )
        db.commit()

        assert r_req.outcome == OutcomeKind.UPDATED

        # D1 fix: confirmed state must NOT be overridden by a customer proposal.
        # get_current_state returns the latest CONFIRMED version.
        state = get_current_state(db, r_conf.commitment_id)
        assert state["confirmed_date"] == "2026-04-04"  # still confirmed Apr 04
        assert state["is_confirmed"] is True             # still confirmed

        # The pending proposal is accessible via get_pending_proposal.
        pending = get_pending_proposal(db, r_conf.commitment_id)
        assert pending is not None
        assert pending["proposed_date"] == "2026-04-08"
        assert pending["action"] == "request_change"

    def test_prior_confirmed_date_still_in_history(
        self, db: sqlite3.Connection
    ) -> None:
        r = apply_event(
            db, _ev(source_doc_id="sig:orig", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db,
            _ev(
                source_doc_id="sig:req",
                source_kind="email.received",
                proposed_date="2026-04-08",
                confirmed_date=None,
                prior_confirmed_date="2026-04-04",
                actor_role="customer", action="request_change",
            ),
            recorded_at=TS,
        )
        db.commit()
        versions = get_all_versions(db, r.commitment_id)
        assert len(versions) == 2
        assert versions[0]["confirmed_date"] == "2026-04-04"
        assert versions[0]["is_confirmed"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# U3 — Business confirmation supersedes confirmed date
# ═══════════════════════════════════════════════════════════════════════════════

class TestU3ConfirmationSupersedes:
    """U3: A call-transcript confirmation appends a new version."""

    def test_confirmation_appends_new_version(self, db: sqlite3.Connection) -> None:
        events = [
            _ev(source_doc_id="sig:orig",
                confirmed_date="2026-04-04", actor_role="business", action="commit"),
            _ev(source_doc_id="sig:req",
                source_kind="email.received",
                proposed_date="2026-04-08", confirmed_date=None,
                prior_confirmed_date="2026-04-04", actor_role="customer", action="request_change"),
            _ev(source_doc_id="sig:trans",
                source_kind="call.transcript",
                prior_confirmed_date="2026-04-04",
                confirmed_date="2026-04-08", actor_role="business", action="commit"),
        ]
        results = replay_events(db, events, recorded_at=TS)
        assert results[2].outcome == OutcomeKind.UPDATED
        state = get_current_state(db, results[0].commitment_id)
        assert state["confirmed_date"] == "2026-04-08"
        assert state["is_confirmed"] is True
        assert state["version"] == 3

    def test_confirmation_links_to_same_commitment(
        self, db: sqlite3.Connection
    ) -> None:
        r1 = apply_event(
            db, _ev(source_doc_id="sig:orig", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        r2 = apply_event(
            db,
            _ev(source_doc_id="sig:trans", source_kind="call.transcript",
                prior_confirmed_date="2026-04-04",
                confirmed_date="2026-04-08"),
            recorded_at=TS,
        )
        db.commit()
        assert r1.commitment_id == r2.commitment_id


# ═══════════════════════════════════════════════════════════════════════════════
# U4 — History preserved after supersession
# ═══════════════════════════════════════════════════════════════════════════════

class TestU4HistoryPreserved:
    """U4: Previous versions are always present and correct after updates."""

    def test_all_versions_in_history(self, db: sqlite3.Connection) -> None:
        events = [
            _ev(source_doc_id="sig:v1", confirmed_date="2026-04-04"),
            _ev(source_doc_id="sig:v2", source_kind="email.received",
                proposed_date="2026-04-08", confirmed_date=None,
                prior_confirmed_date="2026-04-04", actor_role="customer", action="request_change"),
            _ev(source_doc_id="sig:v3", source_kind="call.transcript",
                prior_confirmed_date="2026-04-04",
                confirmed_date="2026-04-08"),
        ]
        results = replay_events(db, events, recorded_at=TS)
        versions = get_all_versions(db, results[0].commitment_id)
        assert len(versions) == 3
        assert [v["source_doc_id"] for v in versions] == ["sig:v1", "sig:v2", "sig:v3"]

    def test_version_numbers_monotonically_increasing(
        self, db: sqlite3.Connection
    ) -> None:
        events = [
            _ev(source_doc_id="sig:v1", confirmed_date="2026-04-04"),
            _ev(source_doc_id="sig:v2", source_kind="call.transcript",
                prior_confirmed_date="2026-04-04",
                confirmed_date="2026-04-08"),
            _ev(source_doc_id="sig:v3", source_kind="call.transcript",
                prior_confirmed_date="2026-04-08",
                confirmed_date="2026-04-10"),
        ]
        results = replay_events(db, events, recorded_at=TS)
        versions = get_all_versions(db, results[0].commitment_id)
        for i, v in enumerate(versions):
            assert v["version"] == i + 1

    def test_original_date_retrievable_from_history(
        self, db: sqlite3.Connection
    ) -> None:
        r = apply_event(
            db, _ev(source_doc_id="sig:orig", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db,
            _ev(source_doc_id="sig:update", source_kind="call.transcript",
                prior_confirmed_date="2026-04-04",
                confirmed_date="2026-04-08"),
            recorded_at=TS,
        )
        db.commit()
        versions = get_all_versions(db, r.commitment_id)
        assert versions[0]["confirmed_date"] == "2026-04-04"
        assert versions[1]["confirmed_date"] == "2026-04-08"
        assert versions[1]["prior_confirmed_date"] == "2026-04-04"


# ═══════════════════════════════════════════════════════════════════════════════
# U5 — Cross-customer isolation
# ═══════════════════════════════════════════════════════════════════════════════

class TestU5CrossCustomerIsolation:
    """U5: Different customers have independent commitment records."""

    def test_different_contacts_separate_commitments(
        self, db: sqlite3.Connection
    ) -> None:
        r_a = apply_event(
            db, _ev(contact_id=ALICE, source_doc_id="sig:alice",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        r_b = apply_event(
            db, _ev(contact_id=BOB, source_doc_id="sig:bob",
                    confirmed_date="2026-05-01"),
            recorded_at=TS,
        )
        db.commit()
        assert r_a.commitment_id != r_b.commitment_id
        assert get_current_state(db, r_a.commitment_id)["confirmed_date"] == "2026-04-04"
        assert get_current_state(db, r_b.commitment_id)["confirmed_date"] == "2026-05-01"

    def test_bob_update_does_not_affect_alice(
        self, db: sqlite3.Connection
    ) -> None:
        r_a = apply_event(
            db, _ev(contact_id=ALICE, source_doc_id="sig:a_orig",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db, _ev(contact_id=BOB, source_doc_id="sig:b_orig",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db,
            _ev(contact_id=BOB, source_doc_id="sig:b_update",
                source_kind="call.transcript",
                prior_confirmed_date="2026-04-04",
                confirmed_date="2026-04-10"),
            recorded_at=TS,
        )
        db.commit()
        alice_state = get_current_state(db, r_a.commitment_id)
        assert alice_state["confirmed_date"] == "2026-04-04"
        assert alice_state["version"] == 1

    def test_ulid_contact_id_rejected(self, db: sqlite3.Connection) -> None:
        result = apply_event(
            db, _ev(contact_id="ent_01ACME000000000000000000"),
            recorded_at=TS,
        )
        assert result.outcome == OutcomeKind.INVALID
        assert "cid_" in result.message


# ═══════════════════════════════════════════════════════════════════════════════
# U6 — Idempotent application
# ═══════════════════════════════════════════════════════════════════════════════

class TestU6Idempotency:
    """U6: Same source_doc_id applied twice → UNCHANGED on the second call."""

    def test_duplicate_returns_unchanged(self, db: sqlite3.Connection) -> None:
        event = _ev(source_doc_id="sig:once")
        r1 = apply_event(db, event, recorded_at=TS)
        db.commit()
        r2 = apply_event(db, event, recorded_at=TS)
        db.commit()
        assert r1.outcome == OutcomeKind.CREATED
        assert r2.outcome == OutcomeKind.UNCHANGED
        assert r2.commitment_id == r1.commitment_id
        assert r2.version == 1

    def test_repeated_apply_does_not_add_versions(
        self, db: sqlite3.Connection
    ) -> None:
        event = _ev(source_doc_id="sig:repeat")
        r = apply_event(db, event, recorded_at=TS)
        db.commit()
        for _ in range(3):
            apply_event(db, event, recorded_at=TS)
            db.commit()
        assert len(get_all_versions(db, r.commitment_id)) == 1

    def test_idempotent_even_with_different_notes(
        self, db: sqlite3.Connection
    ) -> None:
        r1 = apply_event(db, _ev(source_doc_id="sig:idem", notes="v1"), recorded_at=TS)
        db.commit()
        r2 = apply_event(db, _ev(source_doc_id="sig:idem", notes="v2"), recorded_at=TS)
        db.commit()
        assert r2.outcome == OutcomeKind.UNCHANGED
        state = get_current_state(db, r1.commitment_id)
        assert state["notes"] == "v1"


# ═══════════════════════════════════════════════════════════════════════════════
# U7 — Multiple matches → unresolved
# ═══════════════════════════════════════════════════════════════════════════════

class TestU7MultipleMatchesUnresolved:
    """U7: When a no-evidence event matches two or more commitments, the
    outcome is UNRESOLVED and no version is written."""

    def test_two_commitments_gives_unresolved(
        self, db: sqlite3.Connection
    ) -> None:
        # Two distinct deliveries (different named keys, same date)
        apply_event(
            db, _ev(source_doc_id="sig:o1", business_key="ORD-001",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        apply_event(
            db, _ev(source_doc_id="sig:o2", business_key="ORD-002",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        # No-key event with prior_confirmed_date='2026-04-04' → matches both
        result = apply_event(
            db,
            _ev(
                source_doc_id="sig:ambiguous",
                business_key="",
                source_kind="call.transcript",
                prior_confirmed_date="2026-04-04",
                confirmed_date="2026-04-08",
            ),
            recorded_at=TS,
        )
        db.commit()
        assert result.outcome == OutcomeKind.UNRESOLVED
        assert result.commitment_id is None

    def test_unresolved_does_not_write_version(
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
        apply_event(
            db,
            _ev(source_doc_id="sig:amb", business_key="",
                prior_confirmed_date="2026-04-04",
                confirmed_date="2026-04-08"),
            recorded_at=TS,
        )
        db.commit()
        for key in ("ORD-A", "ORD-B"):
            row = db.execute(
                "SELECT id FROM commitments WHERE business_key=?", (key,)
            ).fetchone()
            assert row is not None
            assert get_current_state(db, row[0])["version"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# U8 — Business-key matching
# ═══════════════════════════════════════════════════════════════════════════════

class TestU8BusinessKeyMatching:
    """U8: Named event matches the correct commitment by exact key."""

    def test_key_matches_exact_commitment(self, db: sqlite3.Connection) -> None:
        r1 = apply_event(
            db, _ev(source_doc_id="sig:o1", business_key="ORD-ACME-001",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        r2 = apply_event(
            db, _ev(source_doc_id="sig:o2", business_key="ORD-ACME-002",
                    confirmed_date="2026-05-01"),
            recorded_at=TS,
        )
        db.commit()
        r3 = apply_event(
            db,
            _ev(source_doc_id="sig:trans_001", source_kind="call.transcript",
                business_key="ORD-ACME-001",
                prior_confirmed_date="2026-04-04",
                confirmed_date="2026-04-08"),
            recorded_at=TS,
        )
        db.commit()
        assert r3.outcome == OutcomeKind.UPDATED
        assert r3.commitment_id == r1.commitment_id
        state_002 = get_current_state(db, r2.commitment_id)
        assert state_002["confirmed_date"] == "2026-05-01"
        assert state_002["version"] == 1

    def test_no_match_creates_new_commitment_with_key(
        self, db: sqlite3.Connection
    ) -> None:
        result = apply_event(
            db, _ev(source_doc_id="sig:new", business_key="ORD-BRAND-NEW",
                    confirmed_date="2026-06-01"),
            recorded_at=TS,
        )
        db.commit()
        assert result.outcome == OutcomeKind.CREATED
        state = get_current_state(db, result.commitment_id)
        assert state["business_key"] == "ORD-BRAND-NEW"


# ═══════════════════════════════════════════════════════════════════════════════
# U9 — Deterministic replay
# ═══════════════════════════════════════════════════════════════════════════════

class TestU9DeterministicReplay:
    """U9: Same event sequence always produces identical final state."""

    @staticmethod
    def _run_sequence() -> dict:
        conn = sqlite3.connect(":memory:")
        migrate(conn)
        events = [
            _ev(source_doc_id="sig:conf",
                confirmed_date="2026-04-04", actor_role="business", action="commit"),
            _ev(source_doc_id="sig:req",
                source_kind="email.received",
                proposed_date="2026-04-08", confirmed_date=None,
                prior_confirmed_date="2026-04-04", actor_role="customer", action="request_change"),
            _ev(source_doc_id="sig:trans",
                source_kind="call.transcript",
                prior_confirmed_date="2026-04-04",
                confirmed_date="2026-04-08"),
        ]
        results = replay_events(conn, events, recorded_at=TS)
        state = get_current_state(conn, results[0].commitment_id)
        conn.close()
        return state

    def test_identical_state_across_three_replays(self) -> None:
        states = [self._run_sequence() for _ in range(3)]
        for s in states[1:]:
            assert s["version"]        == states[0]["version"]
            assert s["confirmed_date"] == states[0]["confirmed_date"]
            assert s["proposed_date"]  == states[0]["proposed_date"]
            assert s["is_confirmed"]   == states[0]["is_confirmed"]

    def test_replay_outcome_sequence(self) -> None:
        conn = sqlite3.connect(":memory:")
        migrate(conn)
        events = [
            _ev(source_doc_id="sig:v1", confirmed_date="2026-04-04"),
            _ev(source_doc_id="sig:v2", source_kind="email.received",
                proposed_date="2026-04-08", confirmed_date=None,
                prior_confirmed_date="2026-04-04", actor_role="customer", action="request_change"),
            _ev(source_doc_id="sig:v3", source_kind="call.transcript",
                prior_confirmed_date="2026-04-04",
                confirmed_date="2026-04-08"),
        ]
        results = replay_events(conn, events, recorded_at=TS)
        conn.close()
        assert results[0].outcome == OutcomeKind.CREATED
        assert results[1].outcome == OutcomeKind.UPDATED
        assert results[2].outcome == OutcomeKind.UPDATED

    def test_recorded_at_does_not_affect_logical_state(self) -> None:
        def run(ts: str) -> dict:
            conn = sqlite3.connect(":memory:")
            migrate(conn)
            events = [
                _ev(source_doc_id="sig:v1", confirmed_date="2026-04-04"),
                _ev(source_doc_id="sig:v2", source_kind="call.transcript",
                    prior_confirmed_date="2026-04-04",
                    confirmed_date="2026-04-08"),
            ]
            results = replay_events(conn, events, recorded_at=ts)
            state = get_current_state(conn, results[0].commitment_id)
            conn.close()
            return state
        s1 = run("2026-01-01T00:00:00+00:00")
        s2 = run("2027-12-31T23:59:59+00:00")
        assert s1["confirmed_date"] == s2["confirmed_date"]
        assert s1["version"]        == s2["version"]


# ═══════════════════════════════════════════════════════════════════════════════
# U10 — Cross-channel linking without a shared business key
# ═══════════════════════════════════════════════════════════════════════════════

class TestU10CrossChannelLinking:
    """U10: Opus acceptance scenarios for cross-channel commitment linking.

    Two unnamed deliveries for one customer may coexist when their dates
    differ.  Cross-channel update (email → transcript) uses the transcript's
    ``prior_confirmed_date`` as evidence.  Out-of-order arrival (transcript
    first, email second) produces the same final state as normal order.
    Two plausible matches with the same date are left unresolved.
    """

    # ── Scenario 1: two unnamed deliveries for one customer ───────────────────

    def test_two_unnamed_deliveries_coexist(self, db: sqlite3.Connection) -> None:
        """Different dates → two distinct unnamed commitments."""
        r_apr = apply_event(
            db, _ev(source_doc_id="sig:apr04",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        r_may = apply_event(
            db, _ev(source_doc_id="sig:may01",
                    confirmed_date="2026-05-01"),
            recorded_at=TS,
        )
        db.commit()

        assert r_apr.outcome == OutcomeKind.CREATED
        assert r_may.outcome == OutcomeKind.CREATED
        assert r_apr.commitment_id != r_may.commitment_id
        total = db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        assert total == 2

    # ── Scenario 2: same customer, same dates, delivery ≠ meeting ─────────────

    def test_delivery_and_meeting_same_date_separate(
        self, db: sqlite3.Connection
    ) -> None:
        """Delivery and meeting on the same date must remain separate business
        objects even when both are unnamed and share a customer."""
        r_del = apply_event(
            db, _ev(event_type="delivery",
                    source_doc_id="sig:del_apr04",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        r_mtg = apply_event(
            db, _ev(event_type="meeting",
                    source_doc_id="sig:mtg_apr04",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        assert r_del.outcome == OutcomeKind.CREATED
        assert r_mtg.outcome == OutcomeKind.CREATED
        assert r_del.commitment_id != r_mtg.commitment_id
        del_state = get_current_state(db, r_del.commitment_id)
        mtg_state = get_current_state(db, r_mtg.commitment_id)
        assert del_state["event_type"] == "delivery"
        assert mtg_state["event_type"] == "meeting"

    # ── Scenario 3: unambiguous cross-channel update without order ID ──────────

    def test_unambiguous_cross_channel_update_no_order_id(
        self, db: sqlite3.Connection
    ) -> None:
        """Email (Apr 04, no key) then transcript (prior=Apr04, new=Apr08, no key)
        → one commitment, two versions, correct final state."""
        r_email = apply_event(
            db, _ev(source_doc_id="sig:conf_email",
                    source_kind="email.sent",
                    business_key="",
                    confirmed_date="2026-04-04",
                    occurred_at="2026-04-02T00:00:00+00:00"),
            recorded_at=TS,
        )
        db.commit()
        r_trans = apply_event(
            db, _ev(source_doc_id="sig:call_transcript",
                    source_kind="call.transcript",
                    business_key="",
                    prior_confirmed_date="2026-04-04",
                    confirmed_date="2026-04-08",
                    occurred_at="2026-04-03T00:00:00+00:00"),
            recorded_at=TS,
        )
        db.commit()

        assert r_email.outcome == OutcomeKind.CREATED
        assert r_trans.outcome == OutcomeKind.UPDATED
        assert r_trans.commitment_id == r_email.commitment_id
        total = db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        assert total == 1

        state = get_current_state(db, r_email.commitment_id)
        assert state["confirmed_date"] == "2026-04-08"
        assert state["prior_confirmed_date"] == "2026-04-04"
        assert state["version"] == 2

    def test_cross_channel_update_preserves_history(
        self, db: sqlite3.Connection
    ) -> None:
        """The original Apr 04 version is accessible in history after update."""
        r_email = apply_event(
            db, _ev(source_doc_id="sig:email",
                    confirmed_date="2026-04-04",
                    occurred_at="2026-04-02T00:00:00+00:00"),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db, _ev(source_doc_id="sig:trans",
                    source_kind="call.transcript",
                    business_key="",
                    prior_confirmed_date="2026-04-04",
                    confirmed_date="2026-04-08",
                    occurred_at="2026-04-03T00:00:00+00:00"),
            recorded_at=TS,
        )
        db.commit()

        versions = get_all_versions(db, r_email.commitment_id)
        assert len(versions) == 2
        assert versions[0]["confirmed_date"] == "2026-04-04"
        assert versions[0]["source_kind"] == "email.sent"
        assert versions[1]["confirmed_date"] == "2026-04-08"
        assert versions[1]["prior_confirmed_date"] == "2026-04-04"
        assert versions[1]["source_kind"] == "call.transcript"

    # ── Scenario 4: two plausible matches left unresolved ─────────────────────

    def test_two_same_date_deliveries_unresolved(
        self, db: sqlite3.Connection
    ) -> None:
        """Two deliveries for the same date (different named keys) + a no-key
        transcript with prior=Apr04 → UNRESOLVED (can't tell which one)."""
        apply_event(
            db, _ev(source_doc_id="sig:order_a", business_key="ORD-A",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        apply_event(
            db, _ev(source_doc_id="sig:order_b", business_key="ORD-B",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        result = apply_event(
            db, _ev(source_doc_id="sig:trans",
                    source_kind="call.transcript",
                    business_key="",
                    prior_confirmed_date="2026-04-04",
                    confirmed_date="2026-04-08"),
            recorded_at=TS,
        )
        db.commit()

        assert result.outcome == OutcomeKind.UNRESOLVED
        # Neither existing commitment was modified
        for key in ("ORD-A", "ORD-B"):
            row = db.execute(
                "SELECT id FROM commitments WHERE business_key=?", (key,)
            ).fetchone()
            assert get_current_state(db, row[0])["version"] == 1

    def test_different_dates_disambiguate_two_unnamed_deliveries(
        self, db: sqlite3.Connection
    ) -> None:
        """When two unnamed deliveries have different dates, the transcript's
        prior_confirmed_date uniquely identifies the right one."""
        r_apr = apply_event(
            db, _ev(source_doc_id="sig:apr04", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        r_may = apply_event(
            db, _ev(source_doc_id="sig:may01", confirmed_date="2026-05-01"),
            recorded_at=TS,
        )
        db.commit()

        # Transcript for the April delivery only
        r_trans = apply_event(
            db, _ev(source_doc_id="sig:trans_apr",
                    source_kind="call.transcript",
                    business_key="",
                    prior_confirmed_date="2026-04-04",
                    confirmed_date="2026-04-08"),
            recorded_at=TS,
        )
        db.commit()

        assert r_trans.outcome == OutcomeKind.UPDATED
        assert r_trans.commitment_id == r_apr.commitment_id
        # May commitment untouched
        may_state = get_current_state(db, r_may.commitment_id)
        assert may_state["version"] == 1
        assert may_state["confirmed_date"] == "2026-05-01"

    # ── Scenario 5: out-of-order arrival same final state ─────────────────────

    def test_out_of_order_transcript_first_email_second(
        self, db: sqlite3.Connection
    ) -> None:
        """Transcript arrives before email and both have no business key.
        The email (confirmed=Apr04) finds the transcript via its stored
        prior_confirmed_date='2026-04-04' → one commitment, two versions."""
        # Transcript arrives first (out-of-order)
        r_trans = apply_event(
            db, _ev(source_doc_id="sig:trans",
                    source_kind="call.transcript",
                    business_key="",
                    prior_confirmed_date="2026-04-04",
                    confirmed_date="2026-04-08",
                    occurred_at="2026-04-03T00:00:00+00:00"),
            recorded_at=TS,
        )
        db.commit()

        # Email arrives later (occurred earlier but processed second)
        r_email = apply_event(
            db, _ev(source_doc_id="sig:email",
                    source_kind="email.sent",
                    business_key="",
                    confirmed_date="2026-04-04",
                    occurred_at="2026-04-02T00:00:00+00:00"),
            recorded_at=TS,
        )
        db.commit()

        assert r_trans.outcome == OutcomeKind.CREATED
        assert r_email.outcome == OutcomeKind.UPDATED
        assert r_email.commitment_id == r_trans.commitment_id
        total = db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        assert total == 1

    def test_out_of_order_same_state_as_normal_order(self) -> None:
        """Email→transcript and transcript→email produce the same commitment
        content (version count and final confirmed_date)."""
        def _run(email_first: bool) -> dict:
            conn = sqlite3.connect(":memory:")
            migrate(conn)
            email_ev = _ev(source_doc_id="sig:email",
                           source_kind="email.sent",
                           business_key="",
                           confirmed_date="2026-04-04",
                           occurred_at="2026-04-02T00:00:00+00:00")
            trans_ev = _ev(source_doc_id="sig:trans",
                           source_kind="call.transcript",
                           business_key="",
                           prior_confirmed_date="2026-04-04",
                           confirmed_date="2026-04-08",
                           occurred_at="2026-04-03T00:00:00+00:00")
            seq = [email_ev, trans_ev] if email_first else [trans_ev, email_ev]
            results = replay_events(conn, seq, recorded_at=TS)
            # Find the single commitment
            cid = (results[0] if results[0].commitment_id else results[1]).commitment_id
            state = get_current_state(conn, cid)
            vcount = len(get_all_versions(conn, cid))
            conn.close()
            return {"confirmed_date": state["confirmed_date"], "versions": vcount}

        normal   = _run(email_first=True)
        reversed = _run(email_first=False)
        assert normal["confirmed_date"] == reversed["confirmed_date"] == "2026-04-08"
        assert normal["versions"]       == reversed["versions"]       == 2

    # ── Scenario 6: replay and idempotency ────────────────────────────────────

    def test_cross_channel_claim_idempotent(self, db: sqlite3.Connection) -> None:
        """Transcript that claims an unnamed commitment is idempotent on retry."""
        apply_event(
            db, _ev(source_doc_id="sig:email", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        r1 = apply_event(
            db, _ev(source_doc_id="sig:trans",
                    source_kind="call.transcript",
                    business_key="ORD-ACME-001",
                    prior_confirmed_date="2026-04-04",
                    confirmed_date="2026-04-08"),
            recorded_at=TS,
        )
        db.commit()
        r2 = apply_event(
            db, _ev(source_doc_id="sig:trans",
                    source_kind="call.transcript",
                    business_key="ORD-ACME-001",
                    prior_confirmed_date="2026-04-04",
                    confirmed_date="2026-04-08"),
            recorded_at=TS,
        )
        db.commit()

        assert r1.outcome == OutcomeKind.UPDATED
        assert r2.outcome == OutcomeKind.UNCHANGED
        assert r2.version == 2
        assert db.execute(
            "SELECT COUNT(*) FROM commitment_versions"
        ).fetchone()[0] == 2

    # ── Scenario 7: cross-business isolation ─────────────────────────────────

    def test_meeting_transcript_does_not_affect_delivery(
        self, db: sqlite3.Connection
    ) -> None:
        """A meeting-reschedule transcript must not update a delivery
        commitment, even when the customer and date match."""
        r_del = apply_event(
            db, _ev(event_type="delivery",
                    source_doc_id="sig:del",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()

        # Meeting transcript for the same customer, same date
        apply_event(
            db, _ev(event_type="meeting",
                    source_doc_id="sig:mtg_trans",
                    source_kind="call.transcript",
                    prior_confirmed_date="2026-04-04",
                    confirmed_date="2026-04-08"),
            recorded_at=TS,
        )
        db.commit()

        # Delivery commitment unchanged
        del_state = get_current_state(db, r_del.commitment_id)
        assert del_state["confirmed_date"] == "2026-04-04"
        assert del_state["version"] == 1
        # Meeting got its own commitment
        total = db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        assert total == 2

    def test_named_exact_match_wins_over_unnamed(
        self, db: sqlite3.Connection
    ) -> None:
        """When an exact named commitment exists, the transcript updates it,
        leaving any other unnamed commitment untouched."""
        # Named commitment for ORD-ACME-001
        r_named = apply_event(
            db, _ev(source_doc_id="sig:order_001",
                    business_key="ORD-ACME-001",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        # Unnamed commitment for a different delivery (May)
        r_unnamed = apply_event(
            db, _ev(source_doc_id="sig:anon_may",
                    business_key="",
                    confirmed_date="2026-05-01"),
            recorded_at=TS,
        )
        db.commit()

        # Transcript naming ORD-ACME-001 → exact match
        r_trans = apply_event(
            db, _ev(source_doc_id="sig:trans_001",
                    source_kind="call.transcript",
                    business_key="ORD-ACME-001",
                    prior_confirmed_date="2026-04-04",
                    confirmed_date="2026-04-08"),
            recorded_at=TS,
        )
        db.commit()

        assert r_trans.outcome == OutcomeKind.UPDATED
        assert r_trans.commitment_id == r_named.commitment_id
        named_state = get_current_state(db, r_named.commitment_id)
        assert named_state["confirmed_date"] == "2026-04-08"
        # Unnamed May commitment untouched
        unnamed_state = get_current_state(db, r_unnamed.commitment_id)
        assert unnamed_state["version"] == 1
        assert unnamed_state["confirmed_date"] == "2026-05-01"


# ═══════════════════════════════════════════════════════════════════════════════
# U11 — Business isolation (delivery ≠ meeting)
# ═══════════════════════════════════════════════════════════════════════════════

class TestU11BusinessIsolation:
    """U11: Delivery and meeting commitments are always separate business objects."""

    def test_delivery_and_meeting_create_separate_commitments(
        self, db: sqlite3.Connection
    ) -> None:
        r_d = apply_event(
            db, _ev(event_type="delivery",
                    source_doc_id="sig:delivery_conf",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        r_m = apply_event(
            db, _ev(event_type="meeting",
                    source_doc_id="sig:meeting_conf",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        assert r_d.outcome == OutcomeKind.CREATED
        assert r_m.outcome == OutcomeKind.CREATED
        assert r_d.commitment_id != r_m.commitment_id
        assert db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 2

    def test_meeting_update_does_not_affect_delivery(
        self, db: sqlite3.Connection
    ) -> None:
        r_d = apply_event(
            db, _ev(event_type="delivery",
                    source_doc_id="sig:d_orig",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db, _ev(event_type="meeting",
                    source_doc_id="sig:m_orig",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db, _ev(event_type="meeting",
                    source_doc_id="sig:m_update",
                    source_kind="call.transcript",
                    prior_confirmed_date="2026-04-04",
                    confirmed_date="2026-04-08"),
            recorded_at=TS,
        )
        db.commit()
        delivery_state = get_current_state(db, r_d.commitment_id)
        assert delivery_state["confirmed_date"] == "2026-04-04"
        assert delivery_state["version"] == 1

    def test_invalid_event_type_rejected(self, db: sqlite3.Connection) -> None:
        result = apply_event(db, _ev(event_type="invoice"), recorded_at=TS)
        assert result.outcome == OutcomeKind.INVALID
        assert "delivery" in result.message or "meeting" in result.message

    def test_delivery_event_does_not_match_meeting_commitment(
        self, db: sqlite3.Connection
    ) -> None:
        apply_event(
            db, _ev(event_type="meeting",
                    source_doc_id="sig:mtg",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        r = apply_event(
            db, _ev(event_type="delivery",
                    source_doc_id="sig:del",
                    confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        assert r.outcome == OutcomeKind.CREATED
        assert db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 2
