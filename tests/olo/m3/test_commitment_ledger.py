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
# U10 — Cross-channel linking without a shared business key
# ═══════════════════════════════════════════════════════════════════════════════

class TestU10CrossChannelLinking:
    """U10: A delivery commitment created from an email (no order ID) must be
    linkable to a later call transcript that names the order — without the email
    carrying the order ID.

    The claim rule: when an event names a specific order (business_key != '')
    and no commitment with that exact key exists, but exactly one unnamed
    commitment (business_key='') exists for the same customer + event_type,
    the event *claims* that unnamed commitment by assigning the business_key.
    If multiple unnamed commitments exist the event is left UNRESOLVED.

    Out-of-order arrival is also covered: when the transcript arrives first
    (creating a named commitment) and the email arrives later (no key), the
    email matches the single named commitment via the empty-key rule.
    """

    def test_email_then_transcript_claim(self, db: sqlite3.Connection) -> None:
        """Email (no order ID) then transcript (names order) → one commitment."""
        # Email confirmation: delivery scheduled for April 04; no order ID known
        r_email = apply_event(
            db,
            _ev(
                source_doc_id="sig:conf_email",
                source_kind="email.sent",
                business_key="",
                confirmed_date="2026-04-04",
                is_confirmation=True,
                occurred_at="2026-04-02T00:00:00+00:00",
            ),
            recorded_at=TS,
        )
        db.commit()
        assert r_email.outcome == OutcomeKind.CREATED

        # Transcript: same delivery, names the order, confirms April 08
        r_trans = apply_event(
            db,
            _ev(
                source_doc_id="sig:call_transcript",
                source_kind="call.transcript",
                business_key="ORD-ACME-001",
                confirmed_date="2026-04-08",
                is_confirmation=True,
                occurred_at="2026-04-03T00:00:00+00:00",
            ),
            recorded_at=TS,
        )
        db.commit()

        # Transcript claims the email commitment (not a new one)
        assert r_trans.outcome == OutcomeKind.UPDATED
        assert r_trans.commitment_id == r_email.commitment_id

        # Exactly one commitment total
        total = db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        assert total == 1

        # Commitment is now named
        state = get_current_state(db, r_email.commitment_id)
        assert state["business_key"] == "ORD-ACME-001"
        assert state["version"] == 2

    def test_email_then_transcript_claim_preserves_history(
        self, db: sqlite3.Connection
    ) -> None:
        """The email version (original April 04) survives after the transcript claim."""
        r_email = apply_event(
            db,
            _ev(
                source_doc_id="sig:conf_email",
                source_kind="email.sent",
                business_key="",
                confirmed_date="2026-04-04",
                is_confirmation=True,
                occurred_at="2026-04-02T00:00:00+00:00",
            ),
            recorded_at=TS,
        )
        db.commit()
        apply_event(
            db,
            _ev(
                source_doc_id="sig:call_transcript",
                source_kind="call.transcript",
                business_key="ORD-ACME-001",
                confirmed_date="2026-04-08",
                is_confirmation=True,
                occurred_at="2026-04-03T00:00:00+00:00",
            ),
            recorded_at=TS,
        )
        db.commit()

        versions = get_all_versions(db, r_email.commitment_id)
        assert len(versions) == 2
        # Version 1: email (original April 04)
        assert versions[0]["source_doc_id"] == "sig:conf_email"
        assert versions[0]["confirmed_date"] == "2026-04-04"
        assert versions[0]["source_kind"] == "email.sent"
        # Version 2: transcript (April 08 confirmed)
        assert versions[1]["source_doc_id"] == "sig:call_transcript"
        assert versions[1]["confirmed_date"] == "2026-04-08"
        assert versions[1]["source_kind"] == "call.transcript"

    def test_out_of_order_transcript_first_email_second(
        self, db: sqlite3.Connection
    ) -> None:
        """Transcript (names order) arrives first, email (no key) arrives second.
        Email should append to the named commitment — same result as email-first."""
        # Transcript arrives first (out-of-order processing)
        r_trans = apply_event(
            db,
            _ev(
                source_doc_id="sig:call_transcript",
                source_kind="call.transcript",
                business_key="ORD-ACME-001",
                confirmed_date="2026-04-08",
                is_confirmation=True,
                occurred_at="2026-04-03T00:00:00+00:00",
            ),
            recorded_at=TS,
        )
        db.commit()
        assert r_trans.outcome == OutcomeKind.CREATED

        # Email arrives later (occurred earlier but processed later)
        r_email = apply_event(
            db,
            _ev(
                source_doc_id="sig:conf_email",
                source_kind="email.sent",
                business_key="",
                confirmed_date="2026-04-04",
                is_confirmation=True,
                occurred_at="2026-04-02T00:00:00+00:00",
            ),
            recorded_at=TS,
        )
        db.commit()

        # Email appends to the transcript commitment (single delivery)
        assert r_email.outcome == OutcomeKind.UPDATED
        assert r_email.commitment_id == r_trans.commitment_id
        total = db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        assert total == 1

    def test_two_channels_no_key_link_naturally(
        self, db: sqlite3.Connection
    ) -> None:
        """When BOTH channels carry no business_key, the second links to the first
        automatically (empty-key rule: matches all same-type commitments)."""
        r1 = apply_event(
            db,
            _ev(
                source_doc_id="sig:email",
                source_kind="email.sent",
                business_key="",
                confirmed_date="2026-04-04",
                is_confirmation=True,
            ),
            recorded_at=TS,
        )
        db.commit()
        r2 = apply_event(
            db,
            _ev(
                source_doc_id="sig:transcript",
                source_kind="call.transcript",
                business_key="",
                confirmed_date="2026-04-08",
                is_confirmation=True,
            ),
            recorded_at=TS,
        )
        db.commit()

        assert r1.outcome == OutcomeKind.CREATED
        assert r2.outcome == OutcomeKind.UPDATED
        assert r1.commitment_id == r2.commitment_id
        total = db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        assert total == 1

    def test_exact_named_match_takes_precedence_over_claim(
        self, db: sqlite3.Connection
    ) -> None:
        """When an exact named commitment already exists for the transcript's order,
        the named match wins — no claim of any unnamed commitment needed."""
        # Unnamed commitment created first (email, no order ID)
        apply_event(
            db,
            _ev(source_doc_id="sig:anon_email", business_key="",
                confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        # Named commitment for a DIFFERENT order created separately
        apply_event(
            db,
            _ev(source_doc_id="sig:named_email", source_kind="email.sent",
                business_key="ORD-ACME-002", confirmed_date="2026-05-01",
                is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()
        # At this point: 1 unnamed (from anon_email) and 1 named ORD-ACME-002.
        # The named email claimed the unnamed one first if it found it.
        # In practice, the named email arrives with ORD-ACME-002:
        #   - no exact match initially → claims unnamed → 1 commitment ORD-ACME-002
        # So now there's exactly 1 commitment (ORD-ACME-002).

        # A transcript naming ORD-ACME-002 → exact match → update
        r = apply_event(
            db,
            _ev(source_doc_id="sig:trans_002", source_kind="call.transcript",
                business_key="ORD-ACME-002", confirmed_date="2026-05-08",
                is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()

        assert r.outcome == OutcomeKind.UPDATED
        # Verify exactly one delivery commitment total (the named one)
        total = db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        assert total == 1
        state = get_current_state(db, r.commitment_id)
        assert state["business_key"] == "ORD-ACME-002"

    def test_claim_idempotent_on_second_arrival(
        self, db: sqlite3.Connection
    ) -> None:
        """A claiming transcript arriving twice is idempotent after the first claim."""
        apply_event(
            db,
            _ev(source_doc_id="sig:email", business_key="",
                confirmed_date="2026-04-04", is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()
        r1 = apply_event(
            db,
            _ev(source_doc_id="sig:trans", source_kind="call.transcript",
                business_key="ORD-ACME-001", confirmed_date="2026-04-08",
                is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()
        # Second arrival of the same transcript
        r2 = apply_event(
            db,
            _ev(source_doc_id="sig:trans", source_kind="call.transcript",
                business_key="ORD-ACME-001", confirmed_date="2026-04-08",
                is_confirmation=True),
            recorded_at=TS,
        )
        db.commit()

        assert r1.outcome == OutcomeKind.UPDATED
        assert r2.outcome == OutcomeKind.UNCHANGED
        assert r2.version == 2
        total_versions = db.execute(
            "SELECT COUNT(*) FROM commitment_versions"
        ).fetchone()[0]
        assert total_versions == 2


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
