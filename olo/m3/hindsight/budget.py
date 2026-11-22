"""Olo M3 — DeepSeek budget guard: $8 hard stop.

Polls ``GET /banks/{id}/llm-requests/stats`` before and after each batch.
Uses ``buckets[].tokens.input`` / ``buckets[].tokens.output`` for cost.
Fails closed: missing or malformed stats raise ``StatsUnavailableError``.

Pricing (DeepSeek V3, peak list price):
    Input:   $0.14 / 1 M tokens
    Output:  $0.28 / 1 M tokens

Audit correction D1 (fix 1): real financial hard stop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Pricing ──────────────────────────────────────────────────────────────────

#: Input token cost in USD per 1 M tokens (DeepSeek V3 peak list price).
COST_INPUT_PER_M: float = 0.14
#: Output token cost in USD per 1 M tokens (DeepSeek V3 peak list price).
COST_OUTPUT_PER_M: float = 0.28


# ── Exceptions ────────────────────────────────────────────────────────────────

class BudgetExceededError(Exception):
    """Raised when the cumulative spend reaches or exceeds the budget limit."""
    def __init__(self, spent: float, budget: float, in_tok: int, out_tok: int) -> None:
        super().__init__(
            f"Budget exceeded: spent ${spent:.4f} ≥ limit ${budget:.2f} "
            f"({in_tok:,} input tokens, {out_tok:,} output tokens)"
        )
        self.spent = spent
        self.budget = budget
        self.input_tokens = in_tok
        self.output_tokens = out_tok


class StatsUnavailableError(Exception):
    """Raised when the stats endpoint returns invalid or unparseable data."""


# ── Token snapshot ────────────────────────────────────────────────────────────

@dataclass
class TokenSnapshot:
    """Cumulative token counts at a point in time."""
    input_tokens: int = 0
    output_tokens: int = 0

    def cost_usd(self) -> float:
        return (
            self.input_tokens * COST_INPUT_PER_M / 1_000_000
            + self.output_tokens * COST_OUTPUT_PER_M / 1_000_000
        )

    def delta(self, baseline: "TokenSnapshot") -> "TokenSnapshot":
        """Return the difference from a baseline snapshot."""
        return TokenSnapshot(
            input_tokens=max(0, self.input_tokens - baseline.input_tokens),
            output_tokens=max(0, self.output_tokens - baseline.output_tokens),
        )

    def __add__(self, other: "TokenSnapshot") -> "TokenSnapshot":
        return TokenSnapshot(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_stats(stats: object) -> TokenSnapshot:
    """Extract total token counts from a ``/llm-requests/stats`` response.

    Expected format::

        {
          "bank_id": "...",
          "buckets": [
            {
              "total": 42,
              "tokens": {"input": 12345, "output": 6789}
            },
            ...
          ]
        }

    Returns a :class:`TokenSnapshot` with the sum across all buckets.

    Raises:
        StatsUnavailableError: if ``stats`` is not a dict, or if ``buckets``
            is present but malformed (not a list of dicts with a ``tokens`` key).
    """
    if not isinstance(stats, dict):
        raise StatsUnavailableError(
            f"Stats response is not a dict: {type(stats).__name__}"
        )

    buckets = stats.get("buckets")
    if buckets is None:
        raise StatsUnavailableError(
            "Stats response missing 'buckets' key — "
            "verify the endpoint is /llm-requests/stats"
        )
    if not isinstance(buckets, list):
        raise StatsUnavailableError(
            f"'buckets' must be a list, got {type(buckets).__name__}"
        )

    # Empty buckets is valid — no calls yet
    if not buckets:
        return TokenSnapshot(0, 0)

    total_in = 0
    total_out = 0
    for i, bucket in enumerate(buckets):
        if not isinstance(bucket, dict):
            raise StatsUnavailableError(
                f"bucket[{i}] is not a dict: {type(bucket).__name__}"
            )
        tokens = bucket.get("tokens")
        if tokens is None:
            # Bucket with no tokens key → treat as 0 (tolerate sparse data)
            continue
        if not isinstance(tokens, dict):
            raise StatsUnavailableError(
                f"bucket[{i}].tokens is not a dict: {type(tokens).__name__}"
            )
        total_in += int(tokens.get("input", 0))
        total_out += int(tokens.get("output", 0))

    return TokenSnapshot(input_tokens=total_in, output_tokens=total_out)


# ── Guard ─────────────────────────────────────────────────────────────────────

class UsageGuard:
    """Hard stop at ``budget_usd`` by polling the Hindsight stats endpoint.

    Usage::

        guard = UsageGuard(client, bank_id="main-v1", budget_usd=8.0)
        baseline = guard.snapshot()       # record state before the run
        for batch in batches:
            retain(batch)
            guard.check(baseline)         # raises BudgetExceededError if over

    ``StatsUnavailableError`` is raised (not swallowed) so callers can decide
    whether to abort or continue; in ``OLO_GATE_STRICT=1`` callers must abort.
    """

    def __init__(
        self,
        client,  # HindsightClient
        bank_id: str = "main-v1",
        budget_usd: float = 8.0,
    ) -> None:
        self._client = client
        self._bank_id = bank_id
        self._budget_usd = budget_usd

    def _get_stats(self) -> TokenSnapshot:
        """Poll the stats endpoint and parse it.  Raises StatsUnavailableError."""
        from olo.m3.hindsight.client import _request, HindsightAPIError, HindsightUnavailable
        path = self._client._bank_path("/llm-requests/stats", self._bank_id)
        try:
            raw = _request(self._client._cfg, "GET", path)
        except (HindsightAPIError, HindsightUnavailable) as exc:
            raise StatsUnavailableError(
                f"Stats endpoint unreachable: {exc}"
            ) from exc
        return parse_stats(raw)

    def snapshot(self) -> TokenSnapshot:
        """Record the current cumulative token counts as a baseline.

        Also verifies that the stats endpoint is reachable, failing closed if not.
        Call this once before starting a workload.
        """
        self._require_tracing()  # fail closed if stats unavailable
        return self._get_stats()

    def check(self, baseline: TokenSnapshot) -> TokenSnapshot:
        """Check whether the spend since *baseline* has reached the budget.

        Returns the current snapshot so callers can log intermediate spend.

        Raises:
            StatsUnavailableError:  stats endpoint is unavailable.
            BudgetExceededError:    cumulative spend ≥ budget.
        """
        current = self._get_stats()
        delta = current.delta(baseline)
        spent = delta.cost_usd()
        if spent >= self._budget_usd:
            raise BudgetExceededError(
                spent=spent,
                budget=self._budget_usd,
                in_tok=delta.input_tokens,
                out_tok=delta.output_tokens,
            )
        return current

    def remaining_usd(self, baseline: TokenSnapshot) -> float:
        """Return USD remaining in the budget (raises on unavailable stats)."""
        current = self._get_stats()
        spent = current.delta(baseline).cost_usd()
        return max(0.0, self._budget_usd - spent)

    def _require_tracing(self) -> None:
        """Verify server-side LLM tracing is enabled before guarded work.

        Calls the server's GET /version endpoint and inspects
        ``features.llm_trace`` in the response.  This is a server-side check
        rather than a client-side environment-variable check, so it reflects
        the actual running configuration.

        Raises
        ------
        StatsUnavailableError
            If /version is unreachable, returns invalid data, or reports
            ``features.llm_trace == False``.  Callers must treat this as a
            hard failure — disabled tracing means spending is unmonitored.
        """
        from olo.m3.hindsight.client import _request, HindsightAPIError, HindsightUnavailable
        try:
            version_info = _request(self._client._cfg, "GET", "/version")
        except (HindsightAPIError, HindsightUnavailable) as exc:
            raise StatsUnavailableError(
                f"/version endpoint unreachable: {exc}. "
                "Cannot verify LLM tracing is active."
            ) from exc

        features = version_info.get("features") if isinstance(version_info, dict) else None
        if not isinstance(features, dict):
            raise StatsUnavailableError(
                f"/version response missing 'features' dict: {version_info!r}. "
                "Cannot verify LLM tracing."
            )

        llm_trace = features.get("llm_trace")
        if llm_trace is not True:
            raise StatsUnavailableError(
                f"Server reports features.llm_trace={llm_trace!r}. "
                "LLM usage tracing is not active on this server — the budget "
                "guard cannot enforce spending limits without trace data. "
                "Set HINDSIGHT_API_LLM_TRACE_ENABLED=true and restart the server."
            )
        # Server confirms tracing is active.
