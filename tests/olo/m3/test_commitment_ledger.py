"""Deterministic unit tests for olo/m3/commitment_ledger.py.

Covers U1–U9 and U11 from the Opus architecture review.
All tests use injected events over in-memory SQLite — no LLM, no Hindsight,
no network, no file I/O.
"""
from __future__ import annotations

import sqlite3

import pytest

from olo.m3.commitment_ledger import (
    ApplyResult,
    LedgerEvent,
    OutcomeKind,
    apply_event,
    get_all_versions,
    get_current_state,
    migrate,
    replay_events,
)

# ── Test constants ─────────────────────────────────────────────────────────────

ALICE = "cid_64c1dd94041b33ab9245"   # Alice Chen / Acme Corp (from CRM fixture)
BOB   = "cid_8baee186a88a5a300f68"   # Bob Torres / Beta Tech
# Fixed recorded_at for all deterministic tests
TS    = "2026-01-01T00:00:00+00:00"


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
        proposed_date=None,
        confirmed_date="2026-04-04",
        is_confirmation=True,
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
            db,
            _ev(source_doc_id="sig:conf_orig", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        assert result.outcome == OutcomeKind.CREATED
        assert result.version == 1
        assert result.commitment_id is not None

    def test_state_reflects_confirmed_date(self, db: sqlite3.Connection) -> None:
        result = apply_event(
            db,
            _ev(source_doc_id="sig:conf_orig", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        state = get_current_state(db, result.commitment_id)
        assert state["confirmed_date"] == "2026-04-04"
        assert state["is_confirmed"] is True
        assert state["version"] == 1
        assert state["contact_id"] == ALICE
        assert state["event_type"] == "delivery"
        assert state["source_doc_id"] == "sig:conf_orig"


# ═══════════════════════════════════════════════════════════════════════════════
# U2 — Customer request is a proposal, not a confirmed change
# ═══════════════════════════════════════════════════════════════════════════════

class TestU2CustomerRequestIsProposal:
    """U2: A customer reschedule request (is_confirmation=False) records a
    proposed_date but does not set confirmed_date in the new version."""

    def test_request_records_proposal_not_confirmation(
        self, db: sqlite3.Connection
    ) -> None:
        # Establish confirmed commitment
        r_conf = apply_event(
            db,
            _ev(source_doc_id="sig:orig", confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()

        # Customer request to reschedule
        r_req = apply_event(
            db,
            _ev(
                source_doc_id="sig:request",
                source_kind="email.received",
                proposed_date="2026-04-08",
                confirmed_date=None,
                is_confirmation=False,
            ),
            recorded_at=TS,
        )
        db.commit()

        assert r_req.outcome == OutcomeKind.UPDATED
        state = get_current_state(db, r_conf.commitment_id)
        # Latest version carries the proposal
        assert state["proposed_date"] == "2026-04-08"
        # Request version carries no business confirmation
        assert state["is_confirmed"] is False
        assert state["confirmed_date"] is None

    def test_prior_confirmed_date_still_in_history(
        self, db: sqlite3.Connection
    ) -> None:
        """The original April 4 confirmation must still be retrievable."""
        r = apply_event(
            db,
            _ev(source_doc_id="sig:orig", confirmed_date="2026-04-04", is_confirmation=True),
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
                is_confirmation=False,
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
    """U3: A call-transcript confirmation appends a new version with the
    updated confirmed date."""

    def test_confirmation_appends_new_version(
        self, db: sqlite3.Connection
    ) -> None:
        events = [
            _ev(source_doc_id="sig:orig",   confirmed_date="2026-04-04", is_confirmation=True),
            _ev(source_doc_id="sig:req",    source_kind="email.received",
                proposed_date="2026-04-08", confirmed_date=None, is_confirmation=False),
            _ev(source_doc_id="sig:trans",  source_kind="call.transcript",
                confirmed_date="2026-04-08", is_confirmation=True),
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
            db,
            _ev(source_doc_id="sig:orig",  confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()
        r2 = apply_event(
            db,
            _ev(source_doc_id="sig:trans", source_kind="call.transcript",
                confirmed_date="2026-04-08", is_confirmation=True),
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
            _ev(source_doc_id="sig:v1", confirmed_date="2026-04-04", is_confirmation=True),
            _ev(source_doc_id="sig:v2", source_kind="email.received",
                proposed_date="2026-04-08", confirmed_date=None, is_confirmation=False),
            _ev(source_doc_id="sig:v3", source_kind="call.transcript",
                confirmed_date="2026-04-08", is_confirmation=True),
        ]
        results = replay_events(db, events, recorded_at=TS)
        versions = get_all_versions(db, results[0].commitment_id)

        assert len(versions) == 3
        assert [v["source_doc_id"] for v in versions] == ["sig:v1", "sig:v2", "sig:v3"]

    def test_version_numbers_monotonically_increasing(
        self, db: sqlite3.Connection
    ) -> None:
        events = [
            _ev(source_doc_id="sig:v1", confirmed_date="2026-04-04", is_confirmation=True),
            _ev(source_doc_id="sig:v2", source_kind="call.transcript",
                confirmed_date="2026-04-08", is_confirmation=True),
            _ev(source_doc_id="sig:v3", source_kind="call.transcript",
                confirmed_date="2026-04-10", is_confirmation=True),
        ]
        results = replay_events(db, events, recorded_at=TS)
        versions = get_all_versions(db, results[0].commitment_id)

        for i, v in enumerate(versions):
            assert v["version"] == i + 1

    def test_original_date_retrievable_from_history(
        self, db: sqlite3.Connection
    ) -> None:
        """April 4 must still be in version 1 after the commitment is updated."""
        r = apply_event(
            db,
            _ev(source_doc_id="sig:orig", confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db,
            _ev(source_doc_id="sig:update", source_kind="call.transcript",
                confirmed_date="2026-04-08", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()

        versions = get_all_versions(db, r.commitment_id)
        assert versions[0]["confirmed_date"] == "2026-04-04"
        assert versions[1]["confirmed_date"] == "2026-04-08"


# ═══════════════════════════════════════════════════════════════════════════════
# U5 — Cross-customer isolation
# ═══════════════════════════════════════════════════════════════════════════════

class TestU5CrossCustomerIsolation:
    """U5: Events for different customers create independent commitments; updates
    to one customer's commitment cannot affect another's."""

    def test_different_contacts_separate_commitments(
        self, db: sqlite3.Connection
    ) -> None:
        r_a = apply_event(
            db,
            _ev(contact_id=ALICE, source_doc_id="sig:alice", confirmed_date="2026-04-04"),
            recorded_at=TS,
        )
        db.commit()
        r_b = apply_event(
            db,
            _ev(contact_id=BOB, source_doc_id="sig:bob", confirmed_date="2026-05-01"),
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
            db,
            _ev(contact_id=ALICE, source_doc_id="sig:a_orig",
                confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db,
            _ev(contact_id=BOB, source_doc_id="sig:b_orig",
                confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db,
            _ev(contact_id=BOB, source_doc_id="sig:b_update",
                source_kind="call.transcript",
                confirmed_date="2026-04-10", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()

        alice_state = get_current_state(db, r_a.commitment_id)
        assert alice_state["confirmed_date"] == "2026-04-04"
        assert alice_state["version"] == 1  # Alice untouched

    def test_ulid_contact_id_rejected(self, db: sqlite3.Connection) -> None:
        """Legacy ULID entity IDs are rejected at the boundary."""
        result = apply_event(
            db,
            _ev(contact_id="ent_01ACME000000000000000000"),
            recorded_at=TS,
        )
        assert result.outcome == OutcomeKind.INVALID
        assert "cid_" in result.message


# ═══════════════════════════════════════════════════════════════════════════════
# U6 — Idempotent application
# ═══════════════════════════════════════════════════════════════════════════════

class TestU6Idempotency:
    """U6: Applying the same source document twice returns UNCHANGED on the
    second call without writing any additional version."""

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
        """The source_doc_id guard fires even if the caller re-sends with
        different notes (simulating a retry with slightly changed metadata)."""
        r1 = apply_event(db, _ev(source_doc_id="sig:idem", notes="v1"), recorded_at=TS)
        db.commit()
        r2 = apply_event(db, _ev(source_doc_id="sig:idem", notes="v2"), recorded_at=TS)
        db.commit()

        assert r2.outcome == OutcomeKind.UNCHANGED
        state = get_current_state(db, r1.commitment_id)
        assert state["notes"] == "v1"  # original notes preserved


# ═══════════════════════════════════════════════════════════════════════════════
# U7 — Multiple matches → unresolved
# ═══════════════════════════════════════════════════════════════════════════════

class TestU7MultipleMatchesUnresolved:
    """U7: When a no-key event matches two or more commitments, the outcome is
    UNRESOLVED and no version is written."""

    def test_two_commitments_gives_unresolved(
        self, db: sqlite3.Connection
    ) -> None:
        # Create two distinct delivery commitments for Alice with different keys
        apply_event(
            db,
            _ev(source_doc_id="sig:o1", business_key="ORD-001",
                confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        apply_event(
            db,
            _ev(source_doc_id="sig:o2", business_key="ORD-002",
                confirmed_date="2026-05-01", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()

        # New event with no business key — could be either order
        result = apply_event(
            db,
            _ev(
                source_doc_id="sig:ambiguous",
                business_key="",
                source_kind="email.received",
                proposed_date="2026-04-08",
                confirmed_date=None,
                is_confirmation=False,
            ),
            recorded_at=TS,
        )
        db.commit()

        assert result.outcome == OutcomeKind.UNRESOLVED
        assert result.commitment_id is None
        assert result.version is None

    def test_unresolved_does_not_write_version(
        self, db: sqlite3.Connection
    ) -> None:
        apply_event(
            db,
            _ev(source_doc_id="sig:o1", business_key="ORD-A",
                confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        apply_event(
            db,
            _ev(source_doc_id="sig:o2", business_key="ORD-B",
                confirmed_date="2026-05-01", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db,
            _ev(source_doc_id="sig:amb", business_key="",
                confirmed_date="2026-04-08", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()

        # Confirm neither existing commitment was updated
        for key in ("ORD-A", "ORD-B"):
            row = db.execute(
                "SELECT id FROM commitments WHERE business_key=?", (key,)
            ).fetchone()
            assert row is not None
            state = get_current_state(db, row[0])
            assert state["version"] == 1  # unchanged


# ═══════════════════════════════════════════════════════════════════════════════
# U8 — Business-key matching
# ═══════════════════════════════════════════════════════════════════════════════

class TestU8BusinessKeyMatching:
    """U8: An event with a specific business_key matches the correct commitment
    while leaving other commitments untouched."""

    def test_key_matches_exact_commitment(self, db: sqlite3.Connection) -> None:
        r1 = apply_event(
            db,
            _ev(source_doc_id="sig:o1", business_key="ORD-ACME-001",
                confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        r2 = apply_event(
            db,
            _ev(source_doc_id="sig:o2", business_key="ORD-ACME-002",
                confirmed_date="2026-05-01", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()

        # Update specifically ORD-ACME-001 by key
        r3 = apply_event(
            db,
            _ev(source_doc_id="sig:trans_001", source_kind="call.transcript",
                business_key="ORD-ACME-001",
                confirmed_date="2026-04-08", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()

        assert r3.outcome == OutcomeKind.UPDATED
        assert r3.commitment_id == r1.commitment_id
        # ORD-ACME-002 is untouched
        state_002 = get_current_state(db, r2.commitment_id)
        assert state_002["confirmed_date"] == "2026-05-01"
        assert state_002["version"] == 1

    def test_no_match_creates_new_commitment_with_key(
        self, db: sqlite3.Connection
    ) -> None:
        result = apply_event(
            db,
            _ev(source_doc_id="sig:new", business_key="ORD-BRAND-NEW",
                confirmed_date="2026-06-01", is_confirmation=True),
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
    """U9: Applying the same event sequence to identical empty databases always
    produces identical final state."""

    @staticmethod
    def _run_sequence() -> dict:
        """Run the standard 3-event P1-like sequence; return final state."""
        conn = sqlite3.connect(":memory:")
        migrate(conn)
        events = [
            _ev(source_doc_id="sig:conf",  confirmed_date="2026-04-04", is_confirmation=True),
            _ev(source_doc_id="sig:req",   source_kind="email.received",
                proposed_date="2026-04-08", confirmed_date=None, is_confirmation=False),
            _ev(source_doc_id="sig:trans", source_kind="call.transcript",
                confirmed_date="2026-04-08", is_confirmation=True),
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
        """Results must be CREATED, UPDATED, UPDATED in that order."""
        conn = sqlite3.connect(":memory:")
        migrate(conn)
        events = [
            _ev(source_doc_id="sig:v1", confirmed_date="2026-04-04", is_confirmation=True),
            _ev(source_doc_id="sig:v2", source_kind="email.received",
                proposed_date="2026-04-08", confirmed_date=None, is_confirmation=False),
            _ev(source_doc_id="sig:v3", source_kind="call.transcript",
                confirmed_date="2026-04-08", is_confirmation=True),
        ]
        results = replay_events(conn, events, recorded_at=TS)
        conn.close()

        assert results[0].outcome == OutcomeKind.CREATED
        assert results[1].outcome == OutcomeKind.UPDATED
        assert results[2].outcome == OutcomeKind.UPDATED

    def test_recorded_at_does_not_affect_logical_state(self) -> None:
        """Changing recorded_at must not change business-visible state."""
        def run(ts: str) -> dict:
            conn = sqlite3.connect(":memory:")
            migrate(conn)
            events = [
                _ev(source_doc_id="sig:v1", confirmed_date="2026-04-04",
                    is_confirmation=True),
                _ev(source_doc_id="sig:v2", source_kind="call.transcript",
                    confirmed_date="2026-04-08", is_confirmation=True),
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
# U11 — Business isolation (delivery ≠ meeting)
# ═══════════════════════════════════════════════════════════════════════════════

class TestU11BusinessIsolation:
    """U11: Delivery and meeting commitments are always separate business objects,
    even when they share the same customer and the same dates."""

    def test_delivery_and_meeting_create_separate_commitments(
        self, db: sqlite3.Connection
    ) -> None:
        r_d = apply_event(
            db,
            _ev(event_type="delivery", source_doc_id="sig:delivery_conf",
                confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()
        r_m = apply_event(
            db,
            _ev(event_type="meeting", source_doc_id="sig:meeting_conf",
                confirmed_date="2026-04-04",  # same date — deliberately
                is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()

        assert r_d.outcome == OutcomeKind.CREATED
        assert r_m.outcome == OutcomeKind.CREATED
        assert r_d.commitment_id != r_m.commitment_id
        total = db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        assert total == 2

    def test_meeting_update_does_not_affect_delivery(
        self, db: sqlite3.Connection
    ) -> None:
        r_d = apply_event(
            db,
            _ev(event_type="delivery", source_doc_id="sig:d_orig",
                confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db,
            _ev(event_type="meeting", source_doc_id="sig:m_orig",
                confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db,
            _ev(event_type="meeting", source_doc_id="sig:m_update",
                source_kind="call.transcript",
                confirmed_date="2026-04-08", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()

        delivery_state = get_current_state(db, r_d.commitment_id)
        assert delivery_state["confirmed_date"] == "2026-04-04"
        assert delivery_state["version"] == 1

    def test_invalid_event_type_rejected(self, db: sqlite3.Connection) -> None:
        """Unsupported event types are rejected at the boundary."""
        result = apply_event(db, _ev(event_type="invoice"), recorded_at=TS)
        assert result.outcome == OutcomeKind.INVALID
        assert "delivery" in result.message or "meeting" in result.message

    def test_delivery_event_does_not_match_meeting_commitment(
        self, db: sqlite3.Connection
    ) -> None:
        """A delivery event must never match an existing meeting commitment."""
        apply_event(
            db,
            _ev(event_type="meeting", source_doc_id="sig:mtg",
                confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()

        # Delivery event for the same customer — must NOT match the meeting
        r = apply_event(
            db,
            _ev(event_type="delivery", source_doc_id="sig:del",
                confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()

        assert r.outcome == OutcomeKind.CREATED  # new commitment, not update
        total = db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        assert total == 2
