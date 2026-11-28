"""Every model request counts toward the call cap (olo.extractor).

Regression for the Mode B preparation of 2026-11-27: a model call that
raised was never charged, so a failing API was called once for every
pending email in a run whatever the Budget allowed; and the OpenAI SDK
retried each failed request twice more on its own, unseen by the Budget.
A hard maximum of model requests needs both closed: one call is one HTTP
request, and every attempt is charged.
"""
from __future__ import annotations

import pytest

from olo.extractor import (
    DEEPSEEK_MAX_RETRIES,
    Budget,
    ExtractionStatus,
    extract_commitment_outcome,
    make_deepseek_model_fn,
)
from tests.olo.fake_s5_services import FakeDeepSeek

pytestmark = pytest.mark.not_integration

BODIES = [f"Order ORD-{n} ships on 2026-12-1{n}." for n in range(5)]


def test_a_failing_model_is_called_no_more_than_the_cap() -> None:
    calls: list[str] = []

    def down(prompt: str):
        calls.append(prompt)
        raise ConnectionError("model unavailable")

    budget = Budget(max_calls=2, max_total_tokens=100_000)
    outcomes = [extract_commitment_outcome("email.sent", {"body": b}, "s", budget=budget,
                                           _model_fn=down).status for b in BODIES]
    assert len(calls) == 2
    assert outcomes == [ExtractionStatus.MODEL_ERROR] * 2 + [ExtractionStatus.BUDGET_EXHAUSTED] * 3
    report = budget.report()
    assert report["calls_used"] == 2 and report["failed_calls"] == 2
    assert report["cost_usd"] > 0                      # charged at its conservative estimate


def test_one_model_call_is_one_http_request() -> None:
    """The SDK's own retries are off: a 500 costs one request, not three."""
    assert DEEPSEEK_MAX_RETRIES == 0
    with FakeDeepSeek({}) as deepseek:
        deepseek.fail_status = 500
        fn = make_deepseek_model_fn(api_key="sk-test-not-a-real-key", base_url=deepseek.base_url)
        budget = Budget(max_calls=3)
        for b in BODIES[:3]:
            assert extract_commitment_outcome("email.sent", {"body": b}, "s", budget=budget,
                                              _model_fn=fn).status == ExtractionStatus.MODEL_ERROR
        assert len(deepseek.requests) == 3 and budget.calls_used == 3
        assert extract_commitment_outcome("email.sent", {"body": BODIES[3]}, "s", budget=budget,
                                          _model_fn=fn).status == ExtractionStatus.BUDGET_EXHAUSTED
        assert len(deepseek.requests) == 3
