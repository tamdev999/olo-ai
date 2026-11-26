"""tests/olo/test_write_safety.py — S5 part 1: remaining write-safety gaps.

  * ledger chronology compares UTC instants, never timestamp text (L7);
  * extraction cache keys carry model identity and extraction version;
  * output tokens are priced as output; per-call token limits are enforced
    before a call is made;
  * a year-less date is resolved only through the signal's trusted timestamp
    and only when unambiguous; otherwise it stays unresolved.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from olo import extractor
from olo.extractor import (
    _DEEPSEEK_INPUT_PER_M,
    _DEEPSEEK_OUTPUT_PER_M,
    Budget,
    ExtractionStatus,
    extract_commitment_outcome,
    make_deepseek_model_fn,
    migrate_extraction_cache,
)
from olo.ledger import (
    LedgerEvent,
    OutcomeKind,
    apply_event,
    get_current_state,
    get_pending_proposal,
    migrate,
    utc_instant,
)

pytestmark = pytest.mark.not_integration

S = ExtractionStatus
T = "2026-01-01T00:00:00+00:00"


# ── UTC chronology in the ledger ──────────────────────────────────────────────

def _ev(**kw) -> LedgerEvent:
    base: dict = dict(
        event_type="delivery", contact_id="cid_a", business_key="K", source_doc_id="d1",
        source_kind="email.sent", occurred_at="2026-04-01T00:00:00+00:00",
        actor_role="business", action="commit", confirmed_date="2026-04-10",
    )
    base.update(kw)
    return LedgerEvent(**base)


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    return conn


class TestUtcChronology:
    def test_l7_mixed_offsets_order_by_instant(self, db) -> None:
        # commit at 2026-04-02T04:00Z (written as 23:00-05:00 the day before)
        # is LATER than the confirm_change at 01:00Z; text order says otherwise.
        a = apply_event(db, _ev(occurred_at="2026-04-01T23:00:00-05:00"), recorded_at=T)
        apply_event(db, _ev(source_doc_id="d2", action="confirm_change",
                            confirmed_date="2026-04-20", prior_confirmed_date="2026-04-10",
                            occurred_at="2026-04-02T01:00:00+00:00"), recorded_at=T)
        assert get_current_state(db, a.commitment_id)["scheduled_date"] == "2026-04-10"

    def test_z_and_offset_forms_of_one_instant_tie_break_by_version(self, db) -> None:
        a = apply_event(db, _ev(occurred_at="2026-04-02T01:00:00Z"), recorded_at=T)
        apply_event(db, _ev(source_doc_id="d2", action="confirm_change",
                            confirmed_date="2026-04-20", prior_confirmed_date="2026-04-10",
                            occurred_at="2026-04-02T01:00:00+00:00"), recorded_at=T)
        assert get_current_state(db, a.commitment_id)["scheduled_date"] == "2026-04-20"

    def test_pending_proposal_suppression_uses_instants(self, db) -> None:
        a = apply_event(db, _ev(occurred_at="2026-04-02T10:00:00+00:00"), recorded_at=T)
        # Customer request at 09:00Z (written in +05:00 as 14:00) predates the
        # later confirmed reaffirm at 09:30Z.
        apply_event(db, _ev(source_doc_id="d2", actor_role="customer",
                            action="request_change", confirmed_date=None,
                            proposed_date="2026-04-15", prior_confirmed_date="2026-04-10",
                            occurred_at="2026-04-02T14:00:00+05:00"), recorded_at=T)
        state = get_current_state(db, a.commitment_id)
        assert state["pending_action"] is None      # predates the confirmed commit
        assert get_pending_proposal(db, a.commitment_id)["proposed_date"] == "2026-04-15"

    def test_stored_timestamps_are_not_rewritten(self, db) -> None:
        a = apply_event(db, _ev(occurred_at="2026-04-01T23:00:00-05:00"), recorded_at=T)
        assert get_current_state(db, a.commitment_id)["occurred_at"] == "2026-04-01T23:00:00-05:00"

    @pytest.mark.parametrize("bad", ["2026-04-01T00:00:00", "2026-04-01", "yesterday"])
    def test_timestamp_without_offset_is_invalid(self, db, bad: str) -> None:
        res = apply_event(db, _ev(occurred_at=bad), recorded_at=T)
        assert res.outcome == OutcomeKind.INVALID and "occurred_at" in res.message

    def test_utc_instant(self) -> None:
        assert utc_instant("2026-04-01T23:00:00-05:00") == utc_instant("2026-04-02T04:00:00Z")
        assert utc_instant("2026-04-01T23:00:00") is None


# ── Cache key ─────────────────────────────────────────────────────────────────

BODY = {"subject": "Delivery", "body": "Delivery confirmed for 2024-03-15."}
GOOD = json.dumps({
    "event_type": "delivery", "action": "commit", "business_key": None,
    "confirmed_date": "2024-03-15", "proposed_date": None, "prior_confirmed_date": None,
    "supporting_passage": "Delivery confirmed for 2024-03-15", "confidence": 0.9,
})


def _counting(model_id: str | None):
    calls = []

    def fn(prompt):
        calls.append(1)
        return GOOD, 10
    if model_id is not None:
        fn.model_id = model_id
    return fn, calls


class TestCacheKey:
    def _cache(self):
        conn = sqlite3.connect(":memory:")
        migrate_extraction_cache(conn)
        return conn

    def test_same_model_hits_cache(self) -> None:
        conn = self._cache()
        fn, calls = _counting("model-a")
        for _ in range(2):
            out = extract_commitment_outcome("email.sent", BODY, "s", cache_conn=conn, _model_fn=fn)
            assert out.status == S.EXTRACTED
        assert len(calls) == 1

    def test_different_model_misses_cache(self) -> None:
        conn = self._cache()
        a, calls_a = _counting("model-a")
        b, calls_b = _counting("model-b")
        extract_commitment_outcome("email.sent", BODY, "s", cache_conn=conn, _model_fn=a)
        extract_commitment_outcome("email.sent", BODY, "s", cache_conn=conn, _model_fn=b)
        assert len(calls_a) == len(calls_b) == 1

    def test_explicit_model_id_overrides(self) -> None:
        conn = self._cache()
        fn, calls = _counting("model-a")
        extract_commitment_outcome("email.sent", BODY, "s", cache_conn=conn, _model_fn=fn)
        extract_commitment_outcome("email.sent", BODY, "s", cache_conn=conn, _model_fn=fn,
                                   model_id="model-a-v2")
        assert len(calls) == 2

    def test_extraction_version_is_part_of_the_key(self, monkeypatch) -> None:
        conn = self._cache()
        fn, calls = _counting("model-a")
        extract_commitment_outcome("email.sent", BODY, "s", cache_conn=conn, _model_fn=fn)
        monkeypatch.setattr(extractor, "EXTRACTION_VERSION", "999")
        extract_commitment_outcome("email.sent", BODY, "s", cache_conn=conn, _model_fn=fn)
        assert len(calls) == 2

    def test_deepseek_fn_declares_identity_and_output_cap(self) -> None:
        fn = make_deepseek_model_fn(api_key="sk-test-not-real", max_tokens=321, temperature=0)
        assert fn.model_id.startswith("deepseek:deepseek-flash@")
        assert ":thinking=disabled:" in fn.model_id and "max_tokens=321" in fn.model_id
        assert fn.max_output_tokens == 321
        other = make_deepseek_model_fn(api_key="sk-test-not-real", model="deepseek-reasoner")
        assert other.model_id != fn.model_id


# ── Cost accounting and per-call limits ───────────────────────────────────────

class TestTokenAccounting:
    def test_split_usage_priced_exactly(self) -> None:
        b = Budget()
        extract_commitment_outcome("email.sent", BODY, "s", budget=b,
                                   _model_fn=lambda p: (GOOD, 1000, 100))
        assert b.tokens_used == 1100
        assert b.cost_usd == pytest.approx(
            1000 * _DEEPSEEK_INPUT_PER_M / 1e6 + 100 * _DEEPSEEK_OUTPUT_PER_M / 1e6)

    def test_combined_usage_priced_as_output_upper_bound(self) -> None:
        b = Budget()
        extract_commitment_outcome("email.sent", BODY, "s", budget=b,
                                   _model_fn=lambda p: (GOOD, 1100))
        assert b.tokens_used == 1100
        assert b.cost_usd == pytest.approx(1100 * _DEEPSEEK_OUTPUT_PER_M / 1e6)
        exact = 1000 * _DEEPSEEK_INPUT_PER_M / 1e6 + 100 * _DEEPSEEK_OUTPUT_PER_M / 1e6
        assert b.cost_usd >= exact

    def test_per_call_limit_blocks_before_the_call(self) -> None:
        b = Budget(max_tokens_per_call=100)
        fn, calls = _counting("m")
        out = extract_commitment_outcome("email.sent", BODY, "s", budget=b, _model_fn=fn)
        assert out.status == S.BUDGET_EXHAUSTED and out.retryable
        assert "per-call limit" in out.reason
        assert calls == [] and b.calls_used == 0

    def test_declared_output_cap_counts_toward_the_limit(self) -> None:
        def fn(prompt):
            return GOOD, 10
        fn.max_output_tokens = 5_000
        out = extract_commitment_outcome("email.sent", BODY, "s",
                                         budget=Budget(max_tokens_per_call=3_000), _model_fn=fn)
        assert out.status == S.BUDGET_EXHAUSTED

    def test_total_budget_counts_the_estimate(self) -> None:
        b = Budget(max_total_tokens=500)
        out = extract_commitment_outcome("email.sent", BODY, "s", budget=b,
                                         _model_fn=lambda p: (GOOD, 10))
        assert out.status == S.BUDGET_EXHAUSTED

    def test_reported_overrun_is_counted(self) -> None:
        b = Budget(max_tokens_per_call=3_000)
        extract_commitment_outcome("email.sent", BODY, "s", budget=b,
                                   _model_fn=lambda p: (GOOD, 4_000, 100))
        assert b.per_call_overruns == 1 and b.report()["per_call_overruns"] == 1

    def test_malformed_reply_is_model_error(self) -> None:
        out = extract_commitment_outcome("email.sent", BODY, "s", _model_fn=lambda p: GOOD)
        assert out.status == S.MODEL_ERROR


# ── Year-less dates need a trusted, unambiguous reference ─────────────────────

def _yearless(text: str, iso: str, occurred_at=None):
    resp = json.dumps({
        "event_type": "delivery", "action": "commit", "business_key": None,
        "confirmed_date": iso, "proposed_date": None, "prior_confirmed_date": None,
        "supporting_passage": text.rstrip("."), "confidence": 0.9,
    })
    return extract_commitment_outcome(
        "email.sent", {"subject": "Delivery", "body": text}, "s",
        _model_fn=lambda p: (resp, 10), occurred_at=occurred_at,
    )


class TestYearlessDates:
    def test_resolved_from_trusted_timestamp(self) -> None:
        out = _yearless("Delivery booked for April 10.", "2024-04-10", "2024-03-20T15:30:00Z")
        assert out.status == S.EXTRACTED

    def test_unresolved_without_timestamp(self) -> None:
        assert _yearless("Delivery booked for April 10.", "2024-04-10").status == S.REJECTED

    def test_wrong_year_rejected(self) -> None:
        out = _yearless("Delivery booked for April 10.", "2025-04-10", "2024-03-20T15:30:00Z")
        assert out.status == S.REJECTED

    def test_recent_past_date_resolves_to_same_year(self) -> None:
        out = _yearless("Delivery booked for March 15.", "2024-03-15", "2024-03-20T15:30:00Z")
        assert out.status == S.EXTRACTED

    def test_year_boundary_resolves_forward(self) -> None:
        out = _yearless("Delivery booked for January 5.", "2025-01-05", "2024-12-20T09:00:00Z")
        assert out.status == S.EXTRACTED

    def test_half_year_away_is_ambiguous(self) -> None:
        # 2023-12-28 and 2024-12-28 are both 183 days from 2024-06-28.
        for iso in ("2023-12-28", "2024-12-28"):
            out = _yearless("Delivery booked for December 28.", iso, "2024-06-28T12:00:00Z")
            assert out.status == S.REJECTED

    def test_naive_timestamp_is_not_trusted(self) -> None:
        out = _yearless("Delivery booked for April 10.", "2024-04-10", "2024-03-20T15:30:00")
        assert out.status == S.REJECTED

    def test_numeric_ambiguity_stays_unresolved(self) -> None:
        out = _yearless("Delivery booked for 3/4/2024.", "2024-03-04", "2024-03-01T00:00:00Z")
        assert out.status == S.REJECTED
