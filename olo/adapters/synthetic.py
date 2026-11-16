"""Olo AI — SyntheticAdapter: deterministic, no network, no clock dependence.

§5.4: synthetic adapter spec.
No side effects at import time.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone, timedelta, UTC

from olo.adapters import AdapterConfigError, FetchBatch, MonitorAdapter, SourceError
from olo.signals import NormalizedSignal

_KINDS = ("payment.failed", "invoice.overdue", "meeting.scheduled")
_EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _parse_int(options: Mapping[str, str], key: str, default: int) -> int:
    """Parse *key* from *options* as a non-negative integer."""
    raw = options.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (ValueError, TypeError) as exc:
        raise AdapterConfigError(
            f"synthetic adapter option {key!r} must be an integer, got {raw!r}"
        ) from exc
    if value < 0:
        raise AdapterConfigError(
            f"synthetic adapter option {key!r} must be ≥ 0, got {value}"
        )
    return value


class SyntheticAdapter(MonitorAdapter):
    """Deterministic synthetic event generator for testing and demos.

    Options (all strings, parsed at construction):
        seed         int ≥ 0, default 1
        total        int ≥ 0, default 25
        overlap      int ≥ 0, default 0 — how many events before the cursor to re-deliver
        fail_at_batch int ≥ 1, optional — raises SourceError on that call number
    """

    source = "synthetic"

    def __init__(self, options: Mapping[str, str]) -> None:
        self._seed = _parse_int(options, "seed", 1)
        self._total = _parse_int(options, "total", 25)
        self._overlap = _parse_int(options, "overlap", 0)

        raw_fail = options.get("fail_at_batch")
        if raw_fail is not None:
            try:
                fail_val = int(raw_fail)
            except (ValueError, TypeError) as exc:
                raise AdapterConfigError(
                    f"synthetic adapter option 'fail_at_batch' must be an integer, "
                    f"got {raw_fail!r}"
                ) from exc
            if fail_val < 1:
                raise AdapterConfigError(
                    f"synthetic adapter option 'fail_at_batch' must be ≥ 1, got {fail_val}"
                )
            self._fail_at_batch: int | None = fail_val
        else:
            self._fail_at_batch = None

        self._call_count = 0

    def _make_event(self, n: int) -> NormalizedSignal:
        return NormalizedSignal(
            source="synthetic",
            external_id=f"syn-{self._seed}-{n:05d}",
            kind=_KINDS[(n - 1) % len(_KINDS)],
            occurred_at=_EPOCH + timedelta(minutes=n),
            subject=f"cust-{n % 7:03d}",
            payload={"seq": n, "amount_cents": 1000 + n, "seed": self._seed},
        )

    def fetch(self, cursor: str | None, *, limit: int) -> FetchBatch:
        """Return up to *limit* signals after *cursor*.

        Raises:
            AdapterConfigError: limit < 1 or overlap >= limit.
            SourceError: cursor invalid, or fail_at_batch triggered.
        """
        self._call_count += 1

        if limit < 1:
            raise AdapterConfigError(
                f"fetch limit must be ≥ 1, got {limit}"
            )
        if self._overlap >= limit:
            raise AdapterConfigError(
                "overlap must be smaller than limit, or the cursor cannot advance"
            )

        # Trigger programmed failure
        if self._fail_at_batch is not None and self._call_count == self._fail_at_batch:
            raise SourceError(
                f"synthetic adapter: programmed failure at batch {self._fail_at_batch}"
            )

        # Empty source
        if self._total == 0:
            return FetchBatch(signals=(), next_cursor=None, has_more=False)

        # Parse cursor
        if cursor is None:
            c = 0
        else:
            try:
                c = int(cursor)
            except (ValueError, TypeError) as exc:
                raise SourceError(
                    f"synthetic adapter: invalid cursor {cursor!r}, expected integer string"
                ) from exc
            if c < 0 or c > self._total:
                raise SourceError(
                    f"synthetic adapter: cursor {cursor!r} out of range "
                    f"[0, {self._total}]"
                )

        # Already at the end
        if c >= self._total:
            return FetchBatch(signals=(), next_cursor=cursor, has_more=False)

        # Compute window
        start = max(1, c + 1 - self._overlap)
        last = min(start + limit - 1, self._total)
        signals = tuple(self._make_event(n) for n in range(start, last + 1))
        next_cursor = str(last)
        has_more = last < self._total

        return FetchBatch(signals=signals, next_cursor=next_cursor, has_more=has_more)
