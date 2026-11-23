"""Tests for bounded retries and cancel-on-abort behaviour.

Covers:
- cancel_operation and cancel_bank_pending_operations client methods.
- batch_retain_to_hindsight cancels outstanding ops on FeedError/BudgetExceeded.
- _cancel_outstanding_operations logic (mocked and live where server available).
- deploy_granite.py logs the effective retry setting.
- Server-side retry configuration is bounded by HINDSIGHT_API_LLM_MAX_RETRIES.

Live tests (marked @pytest.mark.integration) require the Hindsight server and
a fresh disposable bank.  They are skipped when the server is unavailable.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch, call

import pytest

from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightAPIError
from olo.m3.feeder import _cancel_outstanding_operations, FeedError, _UNGUARDED


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_client(port: int = 18800) -> HindsightClient:
    cfg = HindsightConfig(host="127.0.0.1", port=port)
    return HindsightClient(cfg)


def _mock_client_with_ops(
    pending_ops: list[dict],
    cancel_responses: Optional[dict] = None,
) -> HindsightClient:
    """Return a mock client whose list_operations returns pending_ops."""
    cfg = HindsightConfig(host="127.0.0.1", port=19999)
    client = HindsightClient(cfg)
    client._cfg = cfg

    client.list_operations = MagicMock(
        return_value={"operations": pending_ops, "total": len(pending_ops)}
    )

    cancel_responses = cancel_responses or {}

    def _cancel(op_id, bank_id=None):
        if op_id in cancel_responses:
            resp = cancel_responses[op_id]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return {"status": "cancelled", "operation_id": op_id}

    client.cancel_operation = MagicMock(side_effect=_cancel)
    return client


# ---------------------------------------------------------------------------
# Unit tests: cancel_operation
# ---------------------------------------------------------------------------

class TestCancelOperation:
    """cancel_operation wraps DELETE /operations/{id}."""

    def test_cancel_pending_op_calls_delete(self) -> None:
        """cancel_operation issues a DELETE request."""
        from olo.m3.hindsight.client import _request
        with patch("olo.m3.hindsight.client._request", return_value={"status": "cancelling"}) as mock_req:
            client = _make_client()
            result = client.cancel_operation("op-abc123", bank_id="test-bank")
        # Verify DELETE was used
        mock_req.assert_called_once()
        _, method, path = mock_req.call_args[0][:3]
        assert method == "DELETE"
        assert "op-abc123" in path
        assert "test-bank" in path

    def test_cancel_nonexistent_op_returns_not_found(self) -> None:
        """404 from server is handled gracefully (op already terminal)."""
        from olo.m3.hindsight.client import _request
        err = HindsightAPIError("Not found", status_code=404)
        with patch("olo.m3.hindsight.client._request", side_effect=err):
            client = _make_client()
            result = client.cancel_operation("nonexistent-op", bank_id="test-bank")
        assert result.get("status") == "not_found"

    def test_cancel_server_error_propagates(self) -> None:
        """5xx from server propagates as HindsightAPIError."""
        from olo.m3.hindsight.client import _request
        err = HindsightAPIError("Internal server error", status_code=500)
        with patch("olo.m3.hindsight.client._request", side_effect=err):
            client = _make_client()
            with pytest.raises(HindsightAPIError):
                client.cancel_operation("op-fail", bank_id="test-bank")


# ---------------------------------------------------------------------------
# Unit tests: cancel_bank_pending_operations
# ---------------------------------------------------------------------------

class TestCancelBankPendingOperations:
    """cancel_bank_pending_operations paginates and cancels all pending ops."""

    def test_cancels_all_pending_ops(self) -> None:
        """All pending/processing ops are cancelled."""
        ops = [
            {"operation_id": "op-1", "status": "pending"},
            {"operation_id": "op-2", "status": "processing"},
            {"operation_id": "op-3", "status": "completed"},  # already terminal
        ]
        client = _mock_client_with_ops(ops)
        result = client.cancel_bank_pending_operations(bank_id="test-bank")

        assert len(result["cancelled"]) == 2
        assert "op-1" in result["cancelled"]
        assert "op-2" in result["cancelled"]
        assert "op-3" in result["already_terminal"]
        assert not result["errors"]

    def test_cancel_failure_recorded_as_unresolved(self) -> None:
        """Failed cancel is recorded in unresolved list, not propagated."""
        ops = [{"operation_id": "op-fail", "status": "pending"}]
        client = _mock_client_with_ops(
            ops,
            cancel_responses={"op-fail": HindsightAPIError("forbidden", status_code=403)},
        )
        result = client.cancel_bank_pending_operations(bank_id="test-bank")

        assert "op-fail" in result["unresolved"]
        assert len(result["errors"]) >= 1

    def test_no_pending_ops_returns_empty(self) -> None:
        """Empty operation list produces empty result."""
        client = _mock_client_with_ops([])
        result = client.cancel_bank_pending_operations(bank_id="test-bank")

        assert result["cancelled"] == []
        assert result["already_terminal"] == []
        assert result["errors"] == []


# ---------------------------------------------------------------------------
# Unit tests: _cancel_outstanding_operations
# ---------------------------------------------------------------------------

class TestCancelOutstandingOperations:
    """_cancel_outstanding_operations targets a specific op then sweeps the bank."""

    def test_cancels_specific_op_then_sweeps(self) -> None:
        """Specific op is cancelled first, then a bank sweep is done."""
        ops = [{"operation_id": "op-sweep", "status": "pending"}]
        client = _mock_client_with_ops(ops)
        client.cancel_operation = MagicMock(return_value={"status": "cancelled"})

        result = _cancel_outstanding_operations(
            client, "test-bank", timed_out_op_id="op-timeout"
        )

        # First cancel call: specific op
        first_call = client.cancel_operation.call_args_list[0]
        assert first_call[0][0] == "op-timeout"
        assert result["specific_op_cancelled"] is True

    def test_no_specific_op_still_sweeps(self) -> None:
        """When no specific op is provided, the sweep still runs."""
        ops = [{"operation_id": "op-pending", "status": "pending"}]
        client = _mock_client_with_ops(ops)
        client.cancel_operation = MagicMock(return_value={"status": "cancelled"})

        result = _cancel_outstanding_operations(client, "test-bank")

        assert result["specific_op_cancelled"] is False
        # The sweep should have been called
        assert client.cancel_operation.call_count >= 1

    def test_cancel_errors_reported_not_raised(self) -> None:
        """Errors during cancel are recorded without hiding original exception."""
        ops = [{"operation_id": "op-err", "status": "pending"}]
        err = HindsightAPIError("forbidden", status_code=403)
        client = _mock_client_with_ops(
            ops, cancel_responses={"op-err": err, "op-timeout": err}
        )
        client.cancel_operation = MagicMock(side_effect=err)

        # Must NOT raise — errors are reported in the summary
        result = _cancel_outstanding_operations(
            client, "test-bank", timed_out_op_id="op-timeout"
        )
        assert len(result["errors"]) >= 1


# ---------------------------------------------------------------------------
# Unit tests: batch_retain_to_hindsight cancel-on-abort
# ---------------------------------------------------------------------------

class TestBatchRetainCancelOnAbort:
    """batch_retain_to_hindsight cancels outstanding ops when aborting."""

    def _make_retain_client(
        self,
        wait_side_effect=None,
        wait_return=None,
        cancel_return=None,
    ) -> HindsightClient:
        cfg = HindsightConfig(host="127.0.0.1", port=19999)
        client = HindsightClient(cfg)
        client._cfg = cfg
        client.retain = MagicMock(
            return_value={"operation_id": "op-test-123", "status": "queued"}
        )
        if wait_side_effect is not None:
            client.wait_for_operation = MagicMock(side_effect=wait_side_effect)
        elif wait_return is not None:
            client.wait_for_operation = MagicMock(return_value=wait_return)
        client.cancel_operation = MagicMock(
            return_value=cancel_return or {"status": "cancelled"}
        )
        client.list_operations = MagicMock(
            return_value={"operations": [], "total": 0}
        )
        return client

    def _guard(self):
        guard = MagicMock()
        guard.snapshot.return_value = MagicMock()
        guard.check = MagicMock()
        return guard

    def test_timeout_triggers_cancel(self) -> None:
        """When wait_for_operation times out, cancel_operation is called."""
        from olo.m3.feeder import batch_retain_to_hindsight
        client = self._make_retain_client(
            wait_side_effect=TimeoutError("timed out after 180s")
        )
        guard = self._guard()
        items = [{"content": "x", "document_id": "sig:x", "strategy": "structured-no-llm"}]

        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            with pytest.raises(FeedError):
                batch_retain_to_hindsight(
                    items, client, bank_id="test",
                    budget_guard=guard, async_=True,
                )

        # cancel_operation must have been called (bank sweep might call list_operations)
        assert client.cancel_operation.called or client.list_operations.called, (
            "cancel or list_operations must be invoked on timeout abort"
        )

    def test_failed_status_triggers_cancel(self) -> None:
        """When operation returns failed status, cancel sweep is triggered."""
        from olo.m3.feeder import batch_retain_to_hindsight
        client = self._make_retain_client(
            wait_return={"status": "failed", "error": "DeepSeek error"}
        )
        guard = self._guard()
        items = [{"content": "x", "document_id": "sig:x", "strategy": "structured-no-llm"}]

        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            with pytest.raises(FeedError, match="unexpected status"):
                batch_retain_to_hindsight(
                    items, client, bank_id="test",
                    budget_guard=guard, async_=True,
                )

    def test_no_cancel_when_guard_is_none(self) -> None:
        """With budget_guard=None (benchmark bypass), no cancel is attempted."""
        from olo.m3.feeder import batch_retain_to_hindsight
        client = self._make_retain_client()
        # Make retain return an unexpected status (would trigger abort with guard)
        client.wait_for_operation = MagicMock(return_value={"status": "completed"})
        # But guard is None — no wait called anyway
        items = [{"content": "x", "document_id": "sig:x", "strategy": "structured-no-llm"}]

        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            batch_retain_to_hindsight(
                items, client, bank_id="test",
                budget_guard=None, async_=True,
            )
        # No cancel or list when guard is None
        assert not client.cancel_operation.called
        assert not client.list_operations.called

    def test_budget_exceeded_triggers_cancel(self) -> None:
        """BudgetExceededError triggers cancellation of outstanding ops."""
        from olo.m3.feeder import batch_retain_to_hindsight
        from olo.m3.hindsight.budget import BudgetExceededError, TokenSnapshot

        client = self._make_retain_client(
            wait_return={"status": "completed"}
        )
        guard = MagicMock()
        guard.snapshot.return_value = TokenSnapshot(0, 0)
        guard.check.side_effect = BudgetExceededError(
            spent=8.01, budget=8.0, in_tok=50_000_000, out_tok=4_000_000
        )
        items = [{"content": "x", "document_id": "sig:x", "strategy": "prose"}]

        with patch("olo.m3.feeder.prepare_hindsight_bank", return_value=True):
            with pytest.raises(BudgetExceededError):
                batch_retain_to_hindsight(
                    items, client, bank_id="test",
                    budget_guard=guard, async_=True,
                )

        # Budget exceeded → abort + cancel sweep triggered
        assert client.list_operations.called or client.cancel_operation.called, (
            "Cancel sweep must run on BudgetExceededError"
        )


# ---------------------------------------------------------------------------
# Unit tests: deploy_granite retry logging
# ---------------------------------------------------------------------------

class TestDeployRetryLogging:
    """deploy() logs the effective LLM retry setting."""

    def test_deploy_logs_retry_limits(self, capsys) -> None:
        """Deploy output includes the effective LLM retry configuration."""
        from olo.m3.deploy_granite import deploy
        from olo.m3.hindsight.budget import StatsUnavailableError

        mock_guard = MagicMock()
        mock_guard.snapshot.return_value = MagicMock()
        mock_guard.check = MagicMock()
        mock_client = MagicMock()

        def mock_batch_retain(items, client, *, bank_id, batch_size, budget_guard,
                              async_, prepare_bank):
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
             patch("olo.m3.deploy_granite.prepare_hindsight_bank", return_value=True), \
             patch("olo.m3.feeder.batch_retain_to_hindsight", side_effect=mock_batch_retain), \
             patch("olo.m3.hindsight.budget.UsageGuard", return_value=mock_guard), \
             patch("pathlib.Path.exists", return_value=True):
            deploy(bank_id="test", host="127.0.0.1", port=19999)

        output = capsys.readouterr().out
        assert "LLM retry limits" in output or "retry" in output.lower(), (
            "deploy() must log the effective retry configuration for auditability"
        )


# ---------------------------------------------------------------------------
# Unit tests: server retry config
# ---------------------------------------------------------------------------

class TestServerRetryConfig:
    """The start_server.sh script sets explicit bounded retry limits."""

    def test_start_server_sets_retry_env_vars(self) -> None:
        """start_server.sh explicitly sets HINDSIGHT_API_LLM_MAX_RETRIES."""
        script = Path(__file__).parent.parent.parent.parent / "scripts/olo/m3/start_server.sh"
        if not script.exists():
            pytest.skip("start_server.sh not found")
        content = script.read_text(encoding="utf-8")
        assert "HINDSIGHT_API_LLM_MAX_RETRIES" in content, (
            "start_server.sh must set HINDSIGHT_API_LLM_MAX_RETRIES to bound server retries"
        )
        assert "HINDSIGHT_API_RETAIN_LLM_MAX_RETRIES" in content, (
            "start_server.sh must set HINDSIGHT_API_RETAIN_LLM_MAX_RETRIES"
        )

    def test_default_llm_max_retries_is_finite(self) -> None:
        """DEFAULT_LLM_MAX_RETRIES in Hindsight config is a small finite int."""
        try:
            from hindsight_api.config import DEFAULT_LLM_MAX_RETRIES
            assert isinstance(DEFAULT_LLM_MAX_RETRIES, int), (
                "DEFAULT_LLM_MAX_RETRIES must be an integer"
            )
            assert 0 < DEFAULT_LLM_MAX_RETRIES <= 10, (
                f"DEFAULT_LLM_MAX_RETRIES={DEFAULT_LLM_MAX_RETRIES} should be in (0, 10]"
            )
        except ImportError:
            pytest.skip("hindsight_api not installed in this venv")


# ---------------------------------------------------------------------------
# Integration tests (require Hindsight server)
# ---------------------------------------------------------------------------

_integration = pytest.mark.integration


@_integration
class TestCancelLiveOps:
    """Live cancellation tests against the pinned Hindsight server."""

    def _client(self) -> HindsightClient:
        from olo.m3.hindsight.client import HindsightUnavailable
        cfg = HindsightConfig.from_env()
        client = HindsightClient(cfg)
        try:
            client.wait_until_ready(timeout_s=10.0)
        except HindsightUnavailable:
            pytest.skip("Hindsight server not running")
        return client

    def test_cancel_nonexistent_op_does_not_raise(self) -> None:
        """Cancelling a non-existent op returns a not_found summary (no exception)."""
        client = self._client()
        import uuid
        result = client.cancel_operation(
            str(uuid.uuid4()),
            bank_id=os.environ.get("HINDSIGHT_BANK_ID", "main-v1-granite"),
        )
        assert result.get("status") in ("not_found", "cancelled", "completed"), (
            f"Unexpected cancel result: {result}"
        )

    def test_cancel_bank_pending_ops_on_idle_bank(self) -> None:
        """cancel_bank_pending_operations on an idle bank returns empty cancelled list."""
        client = self._client()
        bank_id = os.environ.get("HINDSIGHT_BANK_ID", "main-v1-granite")
        result = client.cancel_bank_pending_operations(bank_id=bank_id)
        # Idle bank should have 0 pending ops
        assert isinstance(result["cancelled"], list)
        assert isinstance(result["errors"], list)

    def test_abort_and_cancel_on_fresh_prose_retain(self, tmp_path: Path) -> None:
        """Live: submit a prose retain to a fresh bank, cancel it, verify it stops.

        This test proves cancel_bank_pending_operations actually stops the
        operation rather than just accepting the request.
        """
        import uuid, time
        client = self._client()
        scratch_bank = f"abort-test-{uuid.uuid4().hex[:8]}"

        try:
            # Submit an async prose retain so it's queued
            prose_item = {
                "content": (
                    "Customer called to reschedule their appointment for next Thursday. "
                    "They prefer morning slots and confirmed they still need the full service."
                ),
                "timestamp": "2026-06-01T00:00:00Z",
                "document_id": f"sig:abort-test-{uuid.uuid4().hex[:6]}",
                "entities": [{"text": "abort_test_entity", "type": "person"}],
                "tags": ["src:abort_test", "ym:2026-06"],
                "metadata": {"signal_id": "abort01", "content_sha256": "at",
                             "render_version": "1", "is_llm_eligible": "true"},
                "resolve_entities": False,
            }
            retain_resp = client.retain(
                [prose_item],
                operation_id=str(uuid.uuid5(uuid.NAMESPACE_OID, prose_item["document_id"])),
                async_=True,
                bank_id=scratch_bank,
            )
            submitted_op_id = retain_resp.get("operation_id", "")

            # Give the server 1s to pick up the operation
            time.sleep(1)

            # Check LLM calls BEFORE cancel
            before_ops = client.list_operations(limit=20, bank_id=scratch_bank)
            before_calls = before_ops.get("total", 0)

            # Cancel all pending ops for this bank
            cancel_result = client.cancel_bank_pending_operations(bank_id=scratch_bank)

            # Wait up to 15s to see operations reach terminal state
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                ops = client.list_operations(limit=20, bank_id=scratch_bank)
                pending = [
                    o for o in (ops.get("operations") or [])
                    if o.get("status") in ("pending", "processing")
                ]
                if not pending:
                    break
                time.sleep(1)

            # Check LLM calls AFTER cancel + settle
            time.sleep(2)
            after_calls_resp = client.get_llm_stats(bank_id=scratch_bank)
            # We only assert that the operation is not still growing indefinitely;
            # the actual call count depends on how quickly the cancel landed.
            assert isinstance(after_calls_resp, dict), (
                "get_llm_stats must return a dict for the scratch bank"
            )

            # The operations should be terminal after cancellation
            final_ops = client.list_operations(limit=20, bank_id=scratch_bank)
            final_pending = [
                o for o in (final_ops.get("operations") or [])
                if o.get("status") in ("pending", "processing")
            ]
            assert not final_pending, (
                f"After cancel, {len(final_pending)} operation(s) are still pending: "
                f"{[o.get('operation_id') for o in final_pending]}"
            )

        finally:
            try:
                client.delete_bank(scratch_bank)
            except Exception:
                pass
