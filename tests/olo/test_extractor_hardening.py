"""tests/olo/test_extractor_hardening.py — S1–S4 hardening: source-safe extraction.

Regression tests for the reproduced audit cases (baseline 7e0a56d324):

  E1  a date absent from the supporting passage was accepted
  E2  a one-word passage let a date found nowhere in the text through
  E3  alternatives ("X or Y") became a definite date (business and customer)
  E4  quoted customer text in a business reply became a business commitment
  E8  an impossible calendar date (2024-02-30) was accepted
  E9  model failure and "no commitment" were indistinguishable

The DeepSeek prompt is unchanged; these checks run on the model's answer.
All model responses are injected mocks — no network.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from olo import extractor, ledger
from olo.extractor import (
    Budget,
    ExtractionStatus,
    extract_commitment_outcome,
    extract_commitment_update,
    migrate_extraction_cache,
)

pytestmark = pytest.mark.not_integration

S = ExtractionStatus


def _resp(**kw) -> str:
    d = {"event_type": "delivery", "action": "commit", "business_key": None,
         "confirmed_date": None, "proposed_date": None, "prior_confirmed_date": None,
         "supporting_passage": None, "confidence": 0.9}
    d.update(kw)
    return json.dumps(d)


def _run(kind: str, body: str, resp: str, **kw):
    return extract_commitment_outcome(
        kind, {"subject": "Delivery", "body": body}, "gmail_biz",
        _model_fn=lambda p: (resp, 10), **kw,
    )


# ── E1 / E2: dates must be stated in the quoted passage ───────────────────────

class TestDateGrounding:
    BODY = "Your delivery REF-1 is scheduled for 2024-03-15."

    def test_date_in_passage_is_extracted(self) -> None:
        o = _run("email.sent", self.BODY, _resp(
            confirmed_date="2024-03-15",
            supporting_passage="Your delivery REF-1 is scheduled for 2024-03-15"))
        assert o.status == S.EXTRACTED and o.result.confirmed_date == "2024-03-15"

    def test_e1_date_absent_from_passage_is_rejected(self) -> None:
        o = _run("email.sent", self.BODY, _resp(
            confirmed_date="2024-03-16",
            supporting_passage="Your delivery REF-1 is scheduled for 2024-03-15"))
        assert o.status == S.REJECTED and "not stated explicitly" in o.reason
        assert o.result is None

    def test_e2_one_word_passage_is_rejected(self) -> None:
        o = _run("email.sent", self.BODY, _resp(
            confirmed_date="2024-07-04", supporting_passage="delivery"))
        assert o.status == S.REJECTED

    def test_date_elsewhere_in_text_but_not_passage_is_rejected(self) -> None:
        body = "Please cancel order ORD-7. The delivery was set for 2024-03-18."
        o = _run("email.received", body, _resp(
            action="cancel", prior_confirmed_date="2024-03-18",
            supporting_passage="cancel order ORD-7"))
        assert o.status == S.REJECTED

    @pytest.mark.parametrize("text,iso", [
        ("Delivery is booked for March 15, 2024.", "2024-03-15"),
        ("Delivery is booked for 15 March 2024.", "2024-03-15"),
        ("Delivery is booked for Mar. 15th 2024.", "2024-03-15"),
        ("Delivery is booked for 4/15/2024.", "2024-04-15"),
    ])
    def test_explicit_textual_dates_ground(self, text: str, iso: str) -> None:
        o = _run("email.sent", text, _resp(
            confirmed_date=iso, supporting_passage=text.rstrip(".")))
        assert o.status == S.EXTRACTED, o.reason

    @pytest.mark.parametrize("text,iso", [
        ("Delivery is booked for March 15.", "2024-03-15"),      # no year stated
        ("Delivery is booked for 3/4/2024.", "2024-03-04"),      # 3 Apr or 4 Mar
        ("Delivery is booked for next Tuesday.", "2024-03-19"),  # relative
    ])
    def test_unstated_or_ambiguous_dates_are_rejected(self, text: str, iso: str) -> None:
        o = _run("email.sent", text, _resp(
            confirmed_date=iso, supporting_passage=text.rstrip(".")))
        assert o.status == S.REJECTED


# ── E3: alternatives, ranges and competing dates ──────────────────────────────

class TestAlternativeDates:
    def test_e3a_business_alternatives_rejected(self) -> None:
        body = "We can deliver on 2024-04-10 or 2024-04-17, whichever suits you."
        o = _run("email.sent", body, _resp(
            confirmed_date="2024-04-10",
            supporting_passage="We can deliver on 2024-04-10 or 2024-04-17"))
        assert o.status == S.REJECTED and "alternative" in o.reason

    def test_e3a_short_passage_cannot_hide_the_alternative(self) -> None:
        body = "We can deliver on 2024-04-10 or 2024-04-17, whichever suits you."
        o = _run("email.sent", body, _resp(
            confirmed_date="2024-04-10", supporting_passage="deliver on 2024-04-10"))
        assert o.status == S.REJECTED

    def test_e3b_customer_alternatives_rejected(self) -> None:
        body = "Would either 2024-04-10 or 2024-04-17 work?"
        o = _run("email.received", body, _resp(
            event_type="meeting", action="request_change", proposed_date="2024-04-10",
            supporting_passage="either 2024-04-10 or 2024-04-17", confidence=0.8))
        assert o.status == S.REJECTED

    def test_range_rejected(self) -> None:
        body = "Delivery will happen between 2024-04-10 and 2024-04-12."
        o = _run("email.sent", body, _resp(
            confirmed_date="2024-04-10",
            supporting_passage="Delivery will happen between 2024-04-10 and 2024-04-12"))
        assert o.status == S.REJECTED

    def test_competing_date_in_same_sentence_rejected_for_commit(self) -> None:
        body = "Delivery is 2024-03-15, invoice due 2024-03-30."
        o = _run("email.sent", body, _resp(
            confirmed_date="2024-03-15", supporting_passage="Delivery is 2024-03-15"))
        assert o.status == S.REJECTED and "compete" in o.reason

    def test_date_in_another_sentence_does_not_compete(self) -> None:
        body = "Delivery is confirmed for 2024-03-15. Your invoice is due 2024-03-30."
        o = _run("email.sent", body, _resp(
            confirmed_date="2024-03-15",
            supporting_passage="Delivery is confirmed for 2024-03-15"))
        assert o.status == S.EXTRACTED

    def test_change_may_mention_the_superseded_date(self) -> None:
        body = "We moved your delivery from 2024-03-15 to 2024-03-22."
        o = _run("email.sent", body, _resp(
            action="confirm_change", confirmed_date="2024-03-22",
            supporting_passage="moved your delivery from 2024-03-15 to 2024-03-22"))
        assert o.status == S.EXTRACTED

    def test_change_with_two_unexplained_dates_rejected(self) -> None:
        body = "Moved from 2024-03-15 (was 2024-03-10) to 2024-03-22."
        o = _run("email.sent", body, _resp(
            action="confirm_change", confirmed_date="2024-03-22",
            supporting_passage=body.rstrip(".")))
        assert o.status == S.REJECTED


# ── E4: quoted correspondence is not the author's statement ───────────────────

class TestQuotedText:
    def test_e4_gt_quoted_customer_text_rejected(self) -> None:
        body = ("Thanks, we will check.\n\n> On Mon, Bob wrote:\n"
                "> Can you deliver on 2024-05-02?")
        o = _run("email.sent", body, _resp(
            confirmed_date="2024-05-02", supporting_passage="Can you deliver on 2024-05-02?",
            confidence=0.8))
        assert o.status == S.REJECTED and "quoted" in o.reason

    @pytest.mark.parametrize("header", [
        "On Mon, 4 Mar 2024 at 10:00, Bob <bob@x.com> wrote:",
        "-----Original Message-----",
        "From: Bob <bob@x.com>",
        "---------- Forwarded message ---------",
    ])
    def test_reply_header_blocks_are_not_authored(self, header: str) -> None:
        body = f"Noted.\n{header}\nPlease deliver on 2024-05-02 as agreed."
        o = _run("email.sent", body, _resp(
            confirmed_date="2024-05-02", supporting_passage="Please deliver on 2024-05-02"))
        assert o.status == S.REJECTED

    def test_authored_statement_with_quote_below_is_extracted(self) -> None:
        body = ("Confirmed: delivery on 2024-05-02.\n\n> On Mon, Bob wrote:\n"
                "> Can you deliver on 2024-05-02?")
        o = _run("email.sent", body, _resp(
            confirmed_date="2024-05-02", supporting_passage="Confirmed: delivery on 2024-05-02"))
        assert o.status == S.EXTRACTED and o.result.actor_role == "business"

    def test_business_key_must_be_in_authored_text(self) -> None:
        body = "Delivery confirmed for 2024-05-02."
        o = _run("email.sent", body, _resp(
            confirmed_date="2024-05-02", business_key="ORD-404",
            supporting_passage="Delivery confirmed for 2024-05-02"))
        assert o.status == S.REJECTED and "business_key" in o.reason


# ── E8: dates must be real calendar dates ─────────────────────────────────────

class TestCalendarValidity:
    def test_e8_impossible_date_rejected(self) -> None:
        o = _run("email.sent", "Delivery booked for 2024-02-30.", _resp(
            confirmed_date="2024-02-30", supporting_passage="Delivery booked for 2024-02-30"))
        assert o.status == S.REJECTED and "valid date" in o.reason

    def test_leap_day_accepted(self) -> None:
        o = _run("email.sent", "Delivery booked for 2024-02-29.", _resp(
            confirmed_date="2024-02-29", supporting_passage="Delivery booked for 2024-02-29"))
        assert o.status == S.EXTRACTED

    def test_dated_action_without_date_rejected(self) -> None:
        o = _run("email.sent", "Delivery booked.", _resp(
            confirmed_date=None, supporting_passage="Delivery booked"))
        assert o.status == S.REJECTED

    def test_shares_the_ledger_validator(self) -> None:
        assert extractor.is_valid_iso_date is ledger.is_valid_iso_date


# ── E9: failure is never "no commitment" ──────────────────────────────────────

class TestOutcomeStatuses:
    BODY = "Your delivery REF-1 is scheduled for 2024-03-15."

    def test_model_exception_is_model_error(self) -> None:
        def boom(p):
            raise RuntimeError("network down: secret-token-123")
        o = extract_commitment_outcome("email.sent", {"body": self.BODY}, "s", _model_fn=boom)
        assert o.status == S.MODEL_ERROR and o.retryable
        assert "secret-token-123" not in o.reason

    def test_null_answer_is_no_commitment(self) -> None:
        o = _run("email.sent", self.BODY, _resp(event_type=None, action=None, confidence=0.0))
        assert o.status == S.NO_COMMITMENT and not o.retryable

    def test_invalid_json_is_model_error_and_not_cached(self) -> None:
        conn = sqlite3.connect(":memory:")
        migrate_extraction_cache(conn)
        calls = []

        def bad(p):
            calls.append(1)
            return "not json", 10

        for _ in range(2):
            o = extract_commitment_outcome("email.sent", {"body": self.BODY}, "s",
                                           cache_conn=conn, _model_fn=bad)
            assert o.status == S.MODEL_ERROR
        assert len(calls) == 2
        assert conn.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0] == 0

    def test_empty_response_is_model_error(self) -> None:
        o = _run("email.sent", self.BODY, "")
        assert o.status == S.MODEL_ERROR

    def test_budget_exhausted_is_retryable(self) -> None:
        o = _run("email.sent", self.BODY, _resp(), budget=Budget(max_calls=0))
        assert o.status == S.BUDGET_EXHAUSTED and o.retryable

    def test_no_text_is_no_input(self) -> None:
        o = extract_commitment_outcome("email.sent", {}, "s", _model_fn=lambda p: ("{}", 1))
        assert o.status == S.NO_INPUT

    def test_low_confidence_is_rejected_not_no_commitment(self) -> None:
        o = _run("email.sent", self.BODY, _resp(
            confirmed_date="2024-03-15", confidence=0.5,
            supporting_passage="Your delivery REF-1 is scheduled for 2024-03-15"))
        assert o.status == S.REJECTED and not o.retryable

    def test_verified_answer_is_cached_and_reused(self) -> None:
        conn = sqlite3.connect(":memory:")
        migrate_extraction_cache(conn)
        good = _resp(confirmed_date="2024-03-15",
                     supporting_passage="Your delivery REF-1 is scheduled for 2024-03-15")
        first = extract_commitment_outcome("email.sent", {"body": self.BODY}, "s",
                                           cache_conn=conn, _model_fn=lambda p: (good, 10))
        second = extract_commitment_outcome("email.sent", {"body": self.BODY}, "s",
                                            cache_conn=conn, _model_fn=lambda p: 1 / 0)
        assert first.status == second.status == S.EXTRACTED
        assert second.result.cache_hit

    def test_compat_wrapper_returns_result_only_when_extracted(self) -> None:
        def boom(p):
            raise RuntimeError("x")
        assert extract_commitment_update("email.sent", {"body": self.BODY}, "s",
                                         _model_fn=boom) is None
        good = _resp(confirmed_date="2024-03-15",
                     supporting_passage="Your delivery REF-1 is scheduled for 2024-03-15")
        res = extract_commitment_update("email.sent", {"body": self.BODY}, "s",
                                        _model_fn=lambda p: (good, 10))
        assert res is not None and res.confirmed_date == "2024-03-15"


# ── Only verified results can become ledger events ────────────────────────────

class TestLedgerBoundary:
    def test_only_extracted_outcomes_carry_a_result(self) -> None:
        cases = [
            _resp(confirmed_date="2024-03-16", supporting_passage="scheduled for 2024-03-15"),
            _resp(confirmed_date="2024-02-30", supporting_passage="scheduled for 2024-03-15"),
            _resp(event_type=None, action=None),
            "not json",
        ]
        for resp in cases:
            o = _run("email.sent", "Your delivery is scheduled for 2024-03-15.", resp)
            assert o.status != S.EXTRACTED and o.result is None

    def test_ledger_independently_rejects_invalid_dates(self) -> None:
        conn = sqlite3.connect(":memory:")
        ledger.migrate(conn)
        ev = ledger.LedgerEvent(
            event_type="delivery", contact_id="cid_a", business_key="", source_doc_id="d",
            source_kind="email.sent", occurred_at="2026-04-01T00:00:00+00:00",
            actor_role="business", action="commit", confirmed_date="2024-02-30",
        )
        assert ledger.apply_event(conn, ev).outcome == ledger.OutcomeKind.INVALID
