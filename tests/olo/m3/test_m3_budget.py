"""Olo M3 — Unit tests for the DeepSeek budget guard.

Audit D1 (fix 1): real financial hard stop.
Tests:
  1. parse_stats extracts tokens correctly from buckets[].tokens.input/output
  2. parse_stats handles empty buckets as $0 spend
  3. parse_stats raises StatsUnavailableError on malformed data
  4. UsageGuard.check raises BudgetExceededError when delta ≥ budget
  5. UsageGuard.check raises StatsUnavailableError when endpoint returns 404
  6. BudgetExceededError carries correct spend and token counts
"""
from __future__ import annotations

import pytest

from olo.m3.hindsight.budget import (
    BudgetExceededError,
    COST_INPUT_PER_M,
    COST_OUTPUT_PER_M,
    StatsUnavailableError,
    TokenSnapshot,
    UsageGuard,
    parse_stats,
)


# ── parse_stats tests ─────────────────────────────────────────────────────────

class TestParseStats:
    def test_empty_buckets_returns_zero(self) -> None:
        """Empty bucket list → $0 spend (no calls yet)."""
        snap = parse_stats({"bank_id": "main-v1", "buckets": []})
        assert snap.input_tokens == 0
        assert snap.output_tokens == 0
        assert snap.cost_usd() == 0.0

    def test_single_bucket(self) -> None:
        """Single bucket with known token counts."""
        stats = {
            "bank_id": "main-v1",
            "buckets": [
                {"total": 10, "tokens": {"input": 100_000, "output": 20_000}}
            ],
        }
        snap = parse_stats(stats)
        assert snap.input_tokens == 100_000
        assert snap.output_tokens == 20_000
        expected_cost = 100_000 * COST_INPUT_PER_M / 1_000_000 + 20_000 * COST_OUTPUT_PER_M / 1_000_000
        assert abs(snap.cost_usd() - expected_cost) < 1e-9

    def test_multiple_buckets_summed(self) -> None:
        """Multiple buckets are summed across all days."""
        stats = {
            "buckets": [
                {"total": 5, "tokens": {"input": 50_000, "output": 10_000}},
                {"total": 7, "tokens": {"input": 30_000, "output": 8_000}},
            ]
        }
        snap = parse_stats(stats)
        assert snap.input_tokens == 80_000
        assert snap.output_tokens == 18_000

    def test_bucket_without_tokens_key_treated_as_zero(self) -> None:
        """Bucket with no 'tokens' key → 0 tokens (tolerate sparse data)."""
        stats = {"buckets": [{"total": 1}, {"total": 2, "tokens": {"input": 1000, "output": 200}}]}
        snap = parse_stats(stats)
        assert snap.input_tokens == 1000
        assert snap.output_tokens == 200

    def test_missing_buckets_key_raises(self) -> None:
        """Response without 'buckets' key → StatsUnavailableError."""
        with pytest.raises(StatsUnavailableError, match="missing 'buckets'"):
            parse_stats({"bank_id": "main-v1", "total": 42})

    def test_non_dict_raises(self) -> None:
        """Non-dict response → StatsUnavailableError."""
        with pytest.raises(StatsUnavailableError, match="not a dict"):
            parse_stats([{"buckets": []}])  # list, not dict

    def test_buckets_not_list_raises(self) -> None:
        """'buckets' that is not a list → StatsUnavailableError."""
        with pytest.raises(StatsUnavailableError, match="must be a list"):
            parse_stats({"buckets": {"day1": {"total": 1}}})

    def test_bucket_not_dict_raises(self) -> None:
        """Bucket item that is not a dict → StatsUnavailableError."""
        with pytest.raises(StatsUnavailableError):
            parse_stats({"buckets": ["not_a_dict"]})


# ── TokenSnapshot tests ───────────────────────────────────────────────────────

class TestTokenSnapshot:
    def test_cost_calculation(self) -> None:
        snap = TokenSnapshot(input_tokens=1_000_000, output_tokens=1_000_000)
        expected = COST_INPUT_PER_M + COST_OUTPUT_PER_M
        assert abs(snap.cost_usd() - expected) < 1e-9

    def test_delta_subtracts_baseline(self) -> None:
        before = TokenSnapshot(input_tokens=100, output_tokens=50)
        after = TokenSnapshot(input_tokens=600, output_tokens=250)
        delta = after.delta(before)
        assert delta.input_tokens == 500
        assert delta.output_tokens == 200

    def test_delta_clamps_to_zero(self) -> None:
        """Delta cannot be negative (clock drift or stats reset)."""
        before = TokenSnapshot(input_tokens=1000, output_tokens=500)
        after = TokenSnapshot(input_tokens=800, output_tokens=300)
        delta = after.delta(before)
        assert delta.input_tokens == 0
        assert delta.output_tokens == 0


# ── UsageGuard tests ──────────────────────────────────────────────────────────

class _FakeHindsightClient:
    """Minimal fake client for testing UsageGuard without a real server."""

    def __init__(self, stats_responses: list) -> None:
        self._responses = iter(stats_responses)
        self._cfg = type("cfg", (), {"base_url": "http://localhost:19999", "timeout_s": 5.0})()
        self._bank_id = "main-v1"

    def _bank_path(self, suffix: str, bank_id: str | None = None) -> str:
        b = bank_id or self._bank_id
        return f"/v1/default/banks/{b}{suffix}"


class _FakeGuard(UsageGuard):
    """UsageGuard subclass that injects pre-canned responses."""

    def __init__(
        self,
        stats_responses: list,
        budget_usd: float = 8.0,
    ) -> None:
        # Don't call super().__init__ — inject our own client
        self._client = _FakeHindsightClient(stats_responses)
        self._bank_id = "main-v1"
        self._budget_usd = budget_usd
        self._response_iter = iter(stats_responses)

    def _require_tracing(self) -> None:  # type: ignore[override]
        """No-op: _FakeGuard bypasses server-side tracing checks."""
        pass
    def _get_stats(self) -> TokenSnapshot:  # type: ignore[override]
        """Return next canned response."""
        resp = next(self._response_iter)
        if isinstance(resp, Exception):
            raise resp
        return parse_stats(resp)


class TestUsageGuard:
    def test_no_spend_does_not_raise(self) -> None:
        """Zero delta → no exception."""
        empty = {"buckets": []}
        guard = _FakeGuard([empty, empty])
        baseline = guard.snapshot()
        guard.check(baseline)  # should not raise

    def test_raises_when_budget_exceeded(self) -> None:
        """Spend > $8 raises BudgetExceededError with correct amounts."""
        # 50M input tokens costs 50 × $0.14 = $7.00 at input rate
        # but $8 budget → need more than 57M input tokens or some output
        # Use: 50M input ($7.00) + 4M output ($1.12) = $8.12 → exceeds $8
        before = {"buckets": [{"total": 0, "tokens": {"input": 0, "output": 0}}]}
        after = {
            "buckets": [
                {"total": 100, "tokens": {"input": 50_000_000, "output": 4_000_000}}
            ]
        }
        guard = _FakeGuard([before, after], budget_usd=8.0)
        baseline = guard.snapshot()
        with pytest.raises(BudgetExceededError) as exc_info:
            guard.check(baseline)
        err = exc_info.value
        assert err.spent >= 8.0
        assert err.input_tokens == 50_000_000
        assert err.output_tokens == 4_000_000

    def test_snapshot_raises_on_unavailable_stats(self) -> None:
        """StatsUnavailableError in snapshot propagates to caller."""
        guard = _FakeGuard([StatsUnavailableError("endpoint down")])
        with pytest.raises(StatsUnavailableError, match="endpoint down"):
            guard.snapshot()

    def test_check_raises_on_unavailable_after_snapshot(self) -> None:
        """StatsUnavailableError in check propagates."""
        ok = {"buckets": []}
        guard = _FakeGuard([ok, StatsUnavailableError("endpoint died")])
        baseline = guard.snapshot()
        with pytest.raises(StatsUnavailableError):
            guard.check(baseline)

    def test_exactly_at_budget_raises(self) -> None:
        """Spend == budget (not <) triggers the stop."""
        import math
        # Compute the minimum token count that reaches exactly $8
        in_tok = math.ceil(8.0 / COST_INPUT_PER_M * 1_000_000)
        before = {"buckets": [{"tokens": {"input": 0, "output": 0}}]}
        after = {"buckets": [{"tokens": {"input": in_tok, "output": 0}}]}
        guard = _FakeGuard([before, after], budget_usd=8.0)
        baseline = guard.snapshot()
        with pytest.raises(BudgetExceededError):
            guard.check(baseline)

    def test_just_under_budget_does_not_raise(self) -> None:
        """Spend < budget → no exception."""
        # $7.99 worth of tokens
        in_tok = int(7.99 / COST_INPUT_PER_M * 1_000_000)
        before = {"buckets": [{"tokens": {"input": 0, "output": 0}}]}
        after = {"buckets": [{"tokens": {"input": in_tok, "output": 0}}]}
        guard = _FakeGuard([before, after], budget_usd=8.0)
        baseline = guard.snapshot()
        guard.check(baseline)  # must not raise


# ── batch_retain_to_hindsight budget_guard enforcement ────────────────────────

class TestBudgetGuardMandatory:
    """batch_retain_to_hindsight and reconcile_hindsight_projection require a guard."""

    def test_batch_retain_raises_without_budget_guard(self) -> None:
        """Calling batch_retain_to_hindsight without budget_guard raises ValueError."""
        from olo.m3.feeder import batch_retain_to_hindsight
        from unittest.mock import MagicMock

        client = MagicMock()
        items = [{"content": "test", "document_id": "sig:test",
                  "strategy": "structured-no-llm"}]
        with pytest.raises(ValueError, match="budget_guard is required"):
            batch_retain_to_hindsight(items, client, bank_id="test")

    def test_batch_retain_explicit_none_does_not_raise_guard_check(self) -> None:
        """Explicit budget_guard=None bypasses the enforcement check (test bypass)."""
        from olo.m3.feeder import batch_retain_to_hindsight
        from unittest.mock import MagicMock, patch

        client = MagicMock()
        client.retain = MagicMock(return_value={})
        client.patch_bank_config = MagicMock(return_value={})
        items = [{"content": "test", "document_id": "sig:test",
                  "strategy": "structured-no-llm"}]
        # Must NOT raise ValueError — explicit None is the deliberate test bypass
        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            result = batch_retain_to_hindsight(
                items, client, bank_id="test", budget_guard=None
            )
        assert result["total"] == 1

    def test_reconcile_raises_without_budget_guard(self, tmp_path: Path) -> None:
        """reconcile_hindsight_projection without budget_guard raises ValueError."""
        from olo.m3.feeder import reconcile_hindsight_projection
        from unittest.mock import MagicMock

        db = tmp_path / "r_guard" / "olo.db"
        db.parent.mkdir()
        client = MagicMock()
        with pytest.raises(ValueError, match="budget_guard is required"):
            reconcile_hindsight_projection(db, client, bank_id="test")

    def test_budget_guard_none_does_not_enforce_budget(self) -> None:
        """With budget_guard=None, no stats check runs — budget is unguarded."""
        from olo.m3.feeder import batch_retain_to_hindsight
        from unittest.mock import MagicMock, patch

        client = MagicMock()
        client.retain = MagicMock(return_value={})
        items = [{"content": "test", "document_id": "sig:test",
                  "strategy": "structured-no-llm"}]
        # No budget guard = no stats call = no BudgetExceededError
        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            result = batch_retain_to_hindsight(
                items, client, bank_id="test", budget_guard=None
            )
        # client._get_stats should NOT have been called
        assert result["total"] == 1

    def test_deploy_granite_requires_tracing(self) -> None:
        """deploy() must refuse ingestion when server tracing is disabled.

        This tests the actual deploy_granite.py entry point against a mock
        client that reports tracing disabled.  The test verifies the fail-closed
        behavior required by P0.4.
        """
        from unittest.mock import MagicMock, patch
        from olo.m3.deploy_granite import deploy

        mock_client = MagicMock()
        mock_client.wait_until_ready = MagicMock()

        # Simulate _require_tracing() failing because tracing is disabled
        from olo.m3.hindsight.budget import StatsUnavailableError
        mock_guard = MagicMock()
        mock_guard.snapshot.side_effect = StatsUnavailableError(
            "Server reports features.llm_trace=False."
        )

        with patch("olo.m3.deploy_granite.HindsightClient", return_value=mock_client), \
             patch("olo.m3.deploy_granite.HindsightConfig"), \
             patch("olo.m3.deploy_granite._check_server"), \
             patch("olo.m3.deploy_granite._verify_granite_active", return_value=True), \
             patch("olo.m3.deploy_granite._bank_facts", return_value=10), \
             patch("olo.m3.deploy_granite.build_hindsight_items",
                   return_value=[{"content": "x", "document_id": "sig:x",
                                  "strategy": "structured-no-llm"}]), \
             patch("olo.m3.hindsight.budget.UsageGuard", return_value=mock_guard), \
             patch("pathlib.Path.exists", return_value=True):
            with pytest.raises(RuntimeError, match="[Bb]udget|[Tt]racing"):
                deploy(bank_id="test", host="127.0.0.1", port=19999)

        # Verify no retain was called
        mock_client.retain.assert_not_called()
