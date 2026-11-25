"""tests/olo/test_ledger_cancellation.py — S4 cancellation contract (C1–C4).

Contract (olo.ledger.cancellation_field_error, shared with olo.extractor):
  * the cancelled date belongs in prior_confirmed_date;
  * confirmed_date and proposed_date must be null for a cancellation;
  * violations are rejected (INVALID / no extraction), never remapped.

Ledger state:
  * business cancel → status 'cancelled', no current scheduled date,
    full history preserved;
  * customer cancel → pending request, status stays 'scheduled';
  * a later matching business recommit restores status 'scheduled'.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from olo import extractor
from olo.extractor import extract_commitment_update
from olo.ledger import (
    STATUS_CANCELLED,
    STATUS_SCHEDULED,
    LedgerEvent,
    OutcomeKind,
    apply_event,
    cancellation_field_error,
    get_all_versions,
    get_current_state,
    get_pending_proposal,
    migrate,
)

pytestmark = pytest.mark.not_integration

ALICE = "cid_alice"
T0 = "2026-01-01T00:00:00+00:00"


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    return conn


def _ev(**kw) -> LedgerEvent:
    base: dict = dict(
        event_type="delivery", contact_id=ALICE, business_key="ORD-1",
        source_doc_id="sig:commit", source_kind="email.sent",
        occurred_at="2026-04-01T00:00:00+00:00",
        actor_role="business", action="commit",
        confirmed_date="2026-04-10",
    )
    base.update(kw)
    return LedgerEvent(**base)


def _cancel(**kw) -> LedgerEvent:
    base: dict = dict(
        source_doc_id="sig:cancel", occurred_at="2026-04-03T00:00:00+00:00",
        action="cancel", confirmed_date=None, prior_confirmed_date="2026-04-10",
    )
    base.update(kw)
    return _ev(**base)


def _commit(db: sqlite3.Connection) -> str:
    res = apply_event(db, _ev(), recorded_at=T0)
    assert res.outcome == OutcomeKind.CREATED
    return res.commitment_id


# ── Shared rule ───────────────────────────────────────────────────────────────

class TestCancellationFieldRule:
    def test_valid_cancellation(self) -> None:
        assert cancellation_field_error(
            confirmed_date=None, proposed_date=None, prior_confirmed_date="2026-04-10",
        ) is None

    def test_cancellation_without_prior_date_is_allowed(self) -> None:
        # Key or thread evidence may identify the commitment without a date.
        assert cancellation_field_error(
            confirmed_date=None, proposed_date=None, prior_confirmed_date=None,
        ) is None

    @pytest.mark.parametrize("field", ["confirmed_date", "proposed_date"])
    def test_date_in_forbidden_field_is_rejected(self, field: str) -> None:
        kw = dict(confirmed_date=None, proposed_date=None, prior_confirmed_date=None)
        kw[field] = "2026-04-10"
        err = cancellation_field_error(**kw)
        assert err and field in err

    @pytest.mark.parametrize("bad", ["next Tuesday", "2026/04/10", "", 20260410])
    def test_malformed_prior_date_is_rejected(self, bad: object) -> None:
        assert cancellation_field_error(
            confirmed_date=None, proposed_date=None, prior_confirmed_date=bad,
        )

    def test_extractor_uses_the_ledger_rule(self) -> None:
        assert extractor.cancellation_field_error is cancellation_field_error


# ── Ledger enforcement ────────────────────────────────────────────────────────

class TestLedgerRejectsInvalidCancellations:
    @pytest.mark.parametrize("actor", ["business", "customer"])
    @pytest.mark.parametrize("field", ["confirmed_date", "proposed_date"])
    def test_forbidden_field_is_invalid_and_not_written(
        self, db: sqlite3.Connection, actor: str, field: str,
    ) -> None:
        cid = _commit(db)
        ev = _cancel(actor_role=actor, source_kind="email.received"
                     if actor == "customer" else "email.sent",
                     **{field: "2026-04-10"})
        res = apply_event(db, ev, recorded_at=T0)
        assert res.outcome == OutcomeKind.INVALID
        assert field in res.message
        assert len(get_all_versions(db, cid)) == 1
        assert get_current_state(db, cid)["status"] == STATUS_SCHEDULED

    def test_v1_style_cancel_is_not_remapped(self, db: sqlite3.Connection) -> None:
        """The pre-contract shape (date in confirmed_date) is rejected as-is."""
        cid = _commit(db)
        res = apply_event(
            db, _cancel(confirmed_date="2026-04-10", prior_confirmed_date=None),
            recorded_at=T0,
        )
        assert res.outcome == OutcomeKind.INVALID
        assert [v["action"] for v in get_all_versions(db, cid)] == ["commit"]


# ── Ledger state ──────────────────────────────────────────────────────────────

class TestCancellationState:
    def test_scheduled_after_commit(self, db: sqlite3.Connection) -> None:
        state = get_current_state(db, _commit(db))
        assert state["status"] == STATUS_SCHEDULED
        assert state["scheduled_date"] == "2026-04-10"

    def test_business_cancel_sets_cancelled_without_date(
        self, db: sqlite3.Connection,
    ) -> None:
        cid = _commit(db)
        res = apply_event(db, _cancel(), recorded_at=T0)
        assert res.outcome == OutcomeKind.UPDATED and res.commitment_id == cid
        state = get_current_state(db, cid)
        assert state["status"] == STATUS_CANCELLED
        assert state["scheduled_date"] is None
        assert state["confirmed_date"] is None
        assert state["prior_confirmed_date"] == "2026-04-10"
        assert state["action"] == "cancel" and state["is_confirmed"] is True

    def test_business_cancel_preserves_history(self, db: sqlite3.Connection) -> None:
        cid = _commit(db)
        apply_event(db, _cancel(), recorded_at=T0)
        versions = get_all_versions(db, cid)
        assert [(v["action"], v["confirmed_date"], v["prior_confirmed_date"])
                for v in versions] == [
            ("commit", "2026-04-10", None),
            ("cancel", None, "2026-04-10"),
        ]

    def test_cancel_matches_by_prior_date_without_key(
        self, db: sqlite3.Connection,
    ) -> None:
        res = apply_event(db, _ev(business_key=""), recorded_at=T0)
        cid = res.commitment_id
        res2 = apply_event(db, _cancel(business_key=""), recorded_at=T0)
        assert res2.outcome == OutcomeKind.UPDATED and res2.commitment_id == cid
        assert get_current_state(db, cid)["status"] == STATUS_CANCELLED

    def test_customer_cancel_stays_pending(self, db: sqlite3.Connection) -> None:
        cid = _commit(db)
        res = apply_event(
            db, _cancel(actor_role="customer", source_kind="email.received"),
            recorded_at=T0,
        )
        assert res.outcome == OutcomeKind.UPDATED
        state = get_current_state(db, cid)
        assert state["status"] == STATUS_SCHEDULED
        assert state["scheduled_date"] == "2026-04-10"
        assert state["pending_action"] == "cancel"
        pending = get_pending_proposal(db, cid)
        assert pending["action"] == "cancel" and pending["is_confirmed"] is False
        assert pending["prior_confirmed_date"] == "2026-04-10"

    def test_business_confirms_customer_cancel(self, db: sqlite3.Connection) -> None:
        cid = _commit(db)
        apply_event(db, _cancel(actor_role="customer", source_kind="email.received",
                                source_doc_id="sig:cust_cancel"), recorded_at=T0)
        apply_event(db, _cancel(source_doc_id="sig:biz_cancel",
                                occurred_at="2026-04-04T00:00:00+00:00"),
                    recorded_at=T0)
        state = get_current_state(db, cid)
        assert state["status"] == STATUS_CANCELLED
        assert state["pending_action"] is None  # request superseded

    def test_recommit_restores_scheduled(self, db: sqlite3.Connection) -> None:
        cid = _commit(db)
        apply_event(db, _cancel(), recorded_at=T0)
        res = apply_event(
            db, _ev(source_doc_id="sig:recommit", confirmed_date="2026-04-20",
                    occurred_at="2026-04-05T00:00:00+00:00"),
            recorded_at=T0,
        )
        assert res.outcome == OutcomeKind.UPDATED and res.commitment_id == cid
        state = get_current_state(db, cid)
        assert state["status"] == STATUS_SCHEDULED
        assert state["scheduled_date"] == "2026-04-20"
        assert [v["action"] for v in get_all_versions(db, cid)] == [
            "commit", "cancel", "commit",
        ]


# ── Extractor enforcement ─────────────────────────────────────────────────────

def _cancel_resp(**overrides) -> str:
    data = {
        "event_type": "delivery", "action": "cancel", "business_key": "REF-1",
        "confirmed_date": None, "proposed_date": None,
        "prior_confirmed_date": "2024-03-20",
        "supporting_passage": "cancellation of your delivery REF-1",
        "confidence": 0.9,
    }
    data.update(overrides)
    return json.dumps(data)


_PAYLOAD = {
    "subject": "Cancellation REF-1",
    "body": "We have processed the cancellation of your delivery REF-1 "
            "previously scheduled for 2024-03-20.",
}


def _extract(resp: str):
    return extract_commitment_update(
        "email.sent", _PAYLOAD, "gmail_biz", _model_fn=lambda p: (resp, 10),
    )


class TestExtractorCancellationContract:
    def test_valid_cancellation_extracted(self) -> None:
        res = _extract(_cancel_resp())
        assert res is not None
        assert res.action == "cancel"
        assert res.prior_confirmed_date == "2024-03-20"
        assert res.confirmed_date is None and res.proposed_date is None

    @pytest.mark.parametrize("field", ["confirmed_date", "proposed_date"])
    def test_forbidden_field_rejected_not_remapped(self, field: str) -> None:
        assert _extract(_cancel_resp(**{field: "2024-03-20"})) is None
        assert _extract(_cancel_resp(**{field: "2024-03-20",
                                        "prior_confirmed_date": None})) is None

    @pytest.mark.parametrize("bad", ["next week", "2024/03/20"])
    def test_malformed_prior_date_rejected(self, bad: str) -> None:
        assert _extract(_cancel_resp(prior_confirmed_date=bad)) is None

    def test_non_cancel_actions_unaffected(self) -> None:
        resp = _cancel_resp(action="commit", confirmed_date="2024-03-20",
                            prior_confirmed_date=None)
        res = _extract(resp)
        assert res is not None and res.confirmed_date == "2024-03-20"

    def test_extracted_business_cancel_applies_as_cancelled(
        self, db: sqlite3.Connection,
    ) -> None:
        apply_event(db, _ev(business_key="REF-1", confirmed_date="2024-03-20"),
                    recorded_at=T0)
        res = _extract(_cancel_resp())
        ev = LedgerEvent(
            event_type=res.event_type, contact_id=ALICE,
            business_key=res.business_key or "", source_doc_id="sig:x",
            source_kind="email.sent", occurred_at="2026-04-03T00:00:00+00:00",
            actor_role=res.actor_role, action=res.action,
            confirmed_date=res.confirmed_date, proposed_date=res.proposed_date,
            prior_confirmed_date=res.prior_confirmed_date,
        )
        out = apply_event(db, ev, recorded_at=T0)
        assert out.outcome == OutcomeKind.UPDATED
        assert get_current_state(db, out.commitment_id)["status"] == STATUS_CANCELLED
