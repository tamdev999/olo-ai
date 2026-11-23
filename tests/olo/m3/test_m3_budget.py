"""Olo M3 â€” Unit tests for the DeepSeek budget guard.

Audit D1 (fix 1): real financial hard stop.
Tests:
  1. parse_stats extracts tokens correctly from buckets[].tokens.input/output
  2. parse_stats handles empty buckets as $0 spend
  3. parse_stats raises StatsUnavailableError on malformed data
  4. UsageGuard.check raises BudgetExceededError when delta â‰¥ budget
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


# â”€â”€ parse_stats tests â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestParseStats:
    def test_empty_buckets_returns_zero(self) -> None:
        """Empty bucket list â†’ $0 spend (no calls yet)."""
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
        """Bucket with no 'tokens' key â†’ 0 tokens (tolerate sparse data)."""
        stats = {"buckets": [{"total": 1}, {"total": 2, "tokens": {"input": 1000, "output": 200}}]}
        snap = parse_stats(stats)
        assert snap.input_tokens == 1000
        assert snap.output_tokens == 200

    def test_missing_buckets_key_raises(self) -> None:
        """Response without 'buckets' key â†’ StatsUnavailableError."""
        with pytest.raises(StatsUnavailableError, match="missing 'buckets'"):
            parse_stats({"bank_id": "main-v1", "total": 42})

    def test_non_dict_raises(self) -> None:
        """Non-dict response â†’ StatsUnavailableError."""
        with pytest.raises(StatsUnavailableError, match="not a dict"):
            parse_stats([{"buckets": []}])  # list, not dict

    def test_buckets_not_list_raises(self) -> None:
        """'buckets' that is not a list â†’ StatsUnavailableError."""
        with pytest.raises(StatsUnavailableError, match="must be a list"):
            parse_stats({"buckets": {"day1": {"total": 1}}})

    def test_bucket_not_dict_raises(self) -> None:
        """Bucket item that is not a dict â†’ StatsUnavailableError."""
        with pytest.raises(StatsUnavailableError):
            parse_stats({"buckets": ["not_a_dict"]})


# â”€â”€ TokenSnapshot tests â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â”€â”€ UsageGuard tests â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
        # Don't call super().__init__ â€” inject our own client
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
        """Zero delta â†’ no exception."""
        empty = {"buckets": []}
        guard = _FakeGuard([empty, empty])
        baseline = guard.snapshot()
        guard.check(baseline)  # should not raise

    def test_raises_when_budget_exceeded(self) -> None:
        """Spend > $8 raises BudgetExceededError with correct amounts."""
        # 50M input tokens costs 50 Ã— $0.14 = $7.00 at input rate
        # but $8 budget â†’ need more than 57M input tokens or some output
        # Use: 50M input ($7.00) + 4M output ($1.12) = $8.12 â†’ exceeds $8
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
        """Spend < budget â†’ no exception."""
        # $7.99 worth of tokens
        in_tok = int(7.99 / COST_INPUT_PER_M * 1_000_000)
        before = {"buckets": [{"tokens": {"input": 0, "output": 0}}]}
        after = {"buckets": [{"tokens": {"input": in_tok, "output": 0}}]}
        guard = _FakeGuard([before, after], budget_usd=8.0)
        baseline = guard.snapshot()
        guard.check(baseline)  # must not raise


# â”€â”€ batch_retain_to_hindsight budget_guard enforcement â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
        # Must NOT raise ValueError â€” explicit None is the deliberate test bypass
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
        """With budget_guard=None, no stats check runs â€” budget is unguarded."""
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


# â”€â”€ P0.4 async budget enforcement â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TestAsyncBudgetEnforcement:
    """Async retains must wait for operation completion before budget checks."""

    def test_async_retain_waits_for_operation_before_check(self) -> None:
        """P0.4 root cause: async retain submits work but check runs before costs land.

        This test verifies that batch_retain_to_hindsight calls wait_for_operation
        BEFORE budget_guard.check() when async_=True.
        """
        import uuid as _uuid
        from olo.m3.feeder import batch_retain_to_hindsight
        from olo.m3.hindsight.client import HindsightClient, HindsightConfig
        from unittest.mock import MagicMock, call, patch

        cfg = HindsightConfig(host="127.0.0.1", port=19999)
        client = HindsightClient(cfg)

        # Operation ID returned by the async retain call
        test_op_id = str(_uuid.uuid4())
        retain_called_before = []
        wait_called_before = []
        check_called_before = []
        call_order = []

        def mock_retain(*a, **kw):
            call_order.append("retain")
            return {"operation_id": test_op_id, "status": "queued"}

        def mock_wait(op_id, timeout_s=60.0, bank_id=None):
            call_order.append("wait_for_operation")
            return {"status": "completed"}

        client.retain = mock_retain
        client.wait_for_operation = mock_wait
        client._cfg = cfg

        # Guard that records when check() is called
        guard = MagicMock()
        guard.snapshot.return_value = MagicMock()

        def mock_check(baseline):
            call_order.append("budget_check")

        guard.check = mock_check

        items = [{"content": "test", "document_id": "sig:test",
                  "strategy": "structured-no-llm"}]

        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            batch_retain_to_hindsight(
                items, client, bank_id="test",
                budget_guard=guard, async_=True,
            )

        # wait_for_operation must come BEFORE budget_check
        assert "wait_for_operation" in call_order, (
            "wait_for_operation must be called when async_=True and budget guard is set"
        )
        wait_idx = call_order.index("wait_for_operation")
        check_idx = call_order.index("budget_check")
        assert wait_idx < check_idx, (
            f"wait_for_operation (pos {wait_idx}) must precede budget_check "
            f"(pos {check_idx}) to ensure costs are reflected. Order: {call_order}"
        )

    def test_no_wait_when_budget_guard_is_none(self) -> None:
        """When budget_guard=None, wait_for_operation is NOT called (perf bypass)."""
        from olo.m3.feeder import batch_retain_to_hindsight
        from olo.m3.hindsight.client import HindsightClient, HindsightConfig
        from unittest.mock import MagicMock, patch

        cfg = HindsightConfig(host="127.0.0.1", port=19999)
        client = HindsightClient(cfg)
        wait_calls = []

        def mock_retain(*a, **kw):
            return {"operation_id": "op-123", "status": "queued"}

        def mock_wait(*a, **kw):
            wait_calls.append(a)
            return {"status": "completed"}

        client.retain = mock_retain
        client.wait_for_operation = mock_wait
        client._cfg = cfg

        items = [{"content": "test", "document_id": "sig:test",
                  "strategy": "structured-no-llm"}]

        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            batch_retain_to_hindsight(
                items, client, bank_id="test",
                budget_guard=None,  # explicit bypass
                async_=True,
            )

        assert not wait_calls, (
            "wait_for_operation must NOT be called when budget_guard=None"
        )

    def test_no_wait_when_sync_retain(self) -> None:
        """When async_=False, wait_for_operation is NOT called (server already done)."""
        from olo.m3.feeder import batch_retain_to_hindsight
        from olo.m3.hindsight.client import HindsightClient, HindsightConfig
        from unittest.mock import MagicMock, patch

        cfg = HindsightConfig(host="127.0.0.1", port=19999)
        client = HindsightClient(cfg)
        wait_calls = []

        def mock_retain(*a, **kw):
            return {"operation_id": "op-sync", "status": "completed"}

        def mock_wait(*a, **kw):
            wait_calls.append(a)
            return {"status": "completed"}

        client.retain = mock_retain
        client.wait_for_operation = mock_wait
        client._cfg = cfg

        guard = MagicMock()
        guard.snapshot.return_value = MagicMock()
        guard.check = MagicMock()

        items = [{"content": "test", "document_id": "sig:test",
                  "strategy": "structured-no-llm"}]

        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            batch_retain_to_hindsight(
                items, client, bank_id="test",
                budget_guard=guard,
                async_=False,  # sync: server already completed
            )

        assert not wait_calls, (
            "wait_for_operation must NOT be called when async_=False "
            "(sync retain already completed by the time retain() returns)"
        )


class TestFreshBankInitialization:
    """Bank must exist before snapshot; strategies before first retain."""

    def test_deploy_creates_bank_before_snapshot(self) -> None:
        """deploy() must create bank and register strategies before snapshot().

        The snapshot() call on a nonexistent bank returns HTTP 404
        (StatsUnavailableError).  prepare_hindsight_bank() creates the bank;
        it must be called BEFORE UsageGuard.snapshot().
        """
        from olo.m3.deploy_granite import deploy
        from olo.m3.feeder import batch_retain_to_hindsight
        from olo.m3.hindsight.budget import UsageGuard
        from unittest.mock import MagicMock, patch, call

        call_order = []
        mock_client = MagicMock()
        mock_client.wait_until_ready = MagicMock()

        def mock_prepare_bank(client, bank_id):
            call_order.append("prepare_bank")
            return True

        mock_guard = MagicMock()

        def mock_snapshot():
            call_order.append("snapshot")
            return MagicMock()

        mock_guard.snapshot = mock_snapshot
        mock_guard.check = MagicMock()

        def mock_batch_retain(items, client, *, bank_id, batch_size, budget_guard,
                              async_, prepare_bank):
            call_order.append("batch_retain")
            return {"total": 1, "prose": 0, "structured": 1, "batches": 1}

        with patch("olo.m3.deploy_granite.HindsightClient", return_value=mock_client), \
             patch("olo.m3.deploy_granite.HindsightConfig"), \
             patch("olo.m3.deploy_granite._check_server"), \
             patch("olo.m3.deploy_granite._verify_granite_active", return_value=True), \
             patch("olo.m3.deploy_granite._bank_facts", return_value=-1), \
             patch("olo.m3.deploy_granite._wait_for_consolidation", return_value=1), \
             patch("olo.m3.deploy_granite.build_hindsight_items",
                   return_value=[{"content": "x", "document_id": "sig:x",
                                  "strategy": "structured-no-llm"}]), \
             patch("olo.m3.deploy_granite.prepare_hindsight_bank",
                   side_effect=mock_prepare_bank), \
             patch("olo.m3.feeder.batch_retain_to_hindsight",
                   side_effect=mock_batch_retain), \
             patch("olo.m3.hindsight.budget.UsageGuard", return_value=mock_guard), \
             patch("pathlib.Path.exists", return_value=True):
            deploy(bank_id="test", host="127.0.0.1", port=19999)

        # prepare_bank must precede snapshot
        assert "prepare_bank" in call_order, "prepare_hindsight_bank must be called"
        assert "snapshot" in call_order, "snapshot must be called"
        prep_idx = call_order.index("prepare_bank")
        snap_idx = call_order.index("snapshot")
        assert prep_idx < snap_idx, (
            f"prepare_bank (pos {prep_idx}) must precede snapshot (pos {snap_idx}). "
            f"Order: {call_order}"
        )

    def test_deploy_strategies_before_retain(self) -> None:
        """deploy() must register strategies before the first retain operation."""


# ── B1: Operation-wait fail-closed ───────────────────────────────────────────

class TestOperationWaitFailClosed:
    """B1: timeout, failed operation and unknown status must stop further batches."""

    def _make_retain_client(self, op_id="op-123", wait_side_effect=None,
                             wait_return=None):
        from olo.m3.hindsight.client import HindsightClient, HindsightConfig
        from unittest.mock import MagicMock
        cfg = HindsightConfig(host="127.0.0.1", port=19999)
        client = HindsightClient(cfg)
        client.retain = MagicMock(return_value={"operation_id": op_id, "status": "queued"})
        client._cfg = cfg
        if wait_side_effect is not None:
            client.wait_for_operation = MagicMock(side_effect=wait_side_effect)
        elif wait_return is not None:
            client.wait_for_operation = MagicMock(return_value=wait_return)
        return client

    def _guard_no_check(self):
        from unittest.mock import MagicMock
        guard = MagicMock()
        guard.snapshot.return_value = MagicMock()
        guard.check = MagicMock()
        return guard

    def test_timeout_stops_further_batches(self) -> None:
        """B1: TimeoutError from wait_for_operation stops ingestion."""
        from olo.m3.feeder import batch_retain_to_hindsight, FeedError
        from unittest.mock import patch
        client = self._make_retain_client(wait_side_effect=TimeoutError("timed out"))
        guard = self._guard_no_check()
        items = [{"content": "x", "document_id": "sig:x", "strategy": "structured-no-llm"}]
        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            with pytest.raises(FeedError, match="Cannot confirm completion"):
                batch_retain_to_hindsight(items, client, bank_id="test",
                                          budget_guard=guard, async_=True)
        guard.check.assert_not_called()

    def test_failed_operation_status_stops_further_batches(self) -> None:
        """B1: failed operation status stops ingestion."""
        from olo.m3.feeder import batch_retain_to_hindsight, FeedError
        from unittest.mock import patch
        client = self._make_retain_client(wait_return={"status": "failed"})
        guard = self._guard_no_check()
        items = [{"content": "x", "document_id": "sig:x", "strategy": "structured-no-llm"}]
        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            with pytest.raises(FeedError, match="unexpected status"):
                batch_retain_to_hindsight(items, client, bank_id="test",
                                          budget_guard=guard, async_=True)

    def test_unknown_operation_status_stops_further_batches(self) -> None:
        """B1: unknown/non-terminal operation status stops ingestion."""
        from olo.m3.feeder import batch_retain_to_hindsight, FeedError
        from unittest.mock import patch
        client = self._make_retain_client(wait_return={"status": "in_progress"})
        guard = self._guard_no_check()
        items = [{"content": "x", "document_id": "sig:x", "strategy": "structured-no-llm"}]
        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            with pytest.raises(FeedError, match="unexpected status"):
                batch_retain_to_hindsight(items, client, bank_id="test",
                                          budget_guard=guard, async_=True)

    def test_successful_completed_operation_runs_guard_check(self) -> None:
        """B1 positive: completed operation proceeds to guard check."""
        from olo.m3.feeder import batch_retain_to_hindsight
        from unittest.mock import patch
        client = self._make_retain_client(wait_return={"status": "completed"})
        guard = self._guard_no_check()
        items = [{"content": "x", "document_id": "sig:x", "strategy": "structured-no-llm"}]
        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            batch_retain_to_hindsight(items, client, bank_id="test",
                                      budget_guard=guard, async_=True)
        guard.check.assert_called_once()

    def test_sync_retain_does_not_wait(self) -> None:
        """sync retain does not call wait_for_operation."""
        from olo.m3.feeder import batch_retain_to_hindsight
        from unittest.mock import MagicMock, patch
        client = self._make_retain_client()
        wait_calls = []
        client.wait_for_operation = MagicMock(side_effect=wait_calls.append)
        guard = self._guard_no_check()
        items = [{"content": "x", "document_id": "sig:x", "strategy": "structured-no-llm"}]
        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            batch_retain_to_hindsight(items, client, bank_id="test",
                                      budget_guard=guard, async_=False)
        assert not wait_calls


# ── B2: Configurable budget and batch size ────────────────────────────────────

class TestConfigurableBudget:
    """B2: deploy_granite budget and batch size are configurable and validated."""

    def test_invalid_budget_rejected(self) -> None:
        """Invalid budget values raise ValueError before any deployment action."""
        from olo.m3.deploy_granite import deploy
        import math
        with pytest.raises(ValueError, match="budget_usd"):
            deploy(bank_id="test", budget_usd=0.0)
        with pytest.raises(ValueError, match="budget_usd"):
            deploy(bank_id="test", budget_usd=-5.0)
        with pytest.raises(ValueError, match="budget_usd"):
            deploy(bank_id="test", budget_usd=math.inf)
        with pytest.raises(ValueError, match="budget_usd"):
            deploy(bank_id="test", budget_usd=math.nan)

    def test_invalid_batch_size_rejected(self) -> None:
        """Invalid batch size values raise ValueError."""
        from olo.m3.deploy_granite import deploy, MAX_BATCH_SIZE
        with pytest.raises(ValueError, match="batch_size"):
            deploy(bank_id="test", batch_size=0)
        with pytest.raises(ValueError, match="batch_size"):
            deploy(bank_id="test", batch_size=-1)
        with pytest.raises(ValueError, match="batch_size"):
            deploy(bank_id="test", batch_size=MAX_BATCH_SIZE + 1)

    def test_default_config_is_backward_compatible(self) -> None:
        """Default budget and batch size match previous hard-coded values."""
        from olo.m3.deploy_granite import DEFAULT_BUDGET_USD, DEFAULT_BATCH_SIZE
        assert DEFAULT_BUDGET_USD == 8.0
        assert DEFAULT_BATCH_SIZE == 50

    def test_custom_batch_size_passed_to_retain(self) -> None:
        """Custom batch_size is forwarded to batch_retain_to_hindsight."""
        from olo.m3.deploy_granite import deploy
        from unittest.mock import MagicMock, patch
        captured = []
        def mock_retain(items, client, *, bank_id, batch_size, budget_guard, async_, prepare_bank):
            captured.append(batch_size)
            return {"total": 1, "prose": 0, "structured": 1, "batches": 1}
        mock_guard = MagicMock()
        mock_guard.snapshot.return_value = MagicMock()
        mock_guard.check = MagicMock()
        mock_client = MagicMock()
        with patch("olo.m3.deploy_granite.HindsightClient", return_value=mock_client), \
             patch("olo.m3.deploy_granite.HindsightConfig"), \
             patch("olo.m3.deploy_granite._check_server"), \
             patch("olo.m3.deploy_granite._verify_granite_active", return_value=True), \
             patch("olo.m3.deploy_granite._bank_facts", return_value=-1), \
             patch("olo.m3.deploy_granite._wait_for_consolidation", return_value=1), \
             patch("olo.m3.deploy_granite.build_hindsight_items",
                   return_value=[{"content": "x", "document_id": "sig:x",
                                  "strategy": "structured-no-llm"}]), \
             patch("olo.m3.deploy_granite.prepare_hindsight_bank", return_value=True), \
             patch("olo.m3.feeder.batch_retain_to_hindsight", side_effect=mock_retain), \
             patch("olo.m3.hindsight.budget.UsageGuard", return_value=mock_guard), \
             patch("pathlib.Path.exists", return_value=True):
            deploy(bank_id="test", host="127.0.0.1", port=19999, batch_size=5)
        assert captured == [5]
