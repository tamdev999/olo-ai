"""Tests for olo.adapters.synthetic — SyntheticAdapter.

§8.2 acceptance criteria.
"""
from __future__ import annotations

import pytest

from olo.adapters import AdapterConfigError, FetchBatch
from olo.adapters.synthetic import SyntheticAdapter


def _make(**kwargs) -> SyntheticAdapter:
    defaults: dict[str, str] = {}
    for k, v in kwargs.items():
        defaults[k] = str(v)
    return SyntheticAdapter(defaults)


# ── Determinism ───────────────────────────────────────────────────────────────

def test_deterministic() -> None:
    a = _make(seed=1, total=10)
    b = _make(seed=1, total=10)
    batch_a = a.fetch(None, limit=10)
    batch_b = b.fetch(None, limit=10)
    assert batch_a.signals == batch_b.signals
    assert batch_a.next_cursor == batch_b.next_cursor


# ── Full pagination ───────────────────────────────────────────────────────────

def test_pagination_covers_all_events_once() -> None:
    adapter = _make(total=25)
    cursor: str | None = None
    all_ids: list[str] = []
    cursors: list[str | None] = []
    has_more_flags: list[bool] = []

    while True:
        batch = adapter.fetch(cursor, limit=10)
        all_ids.extend(s.external_id for s in batch.signals)
        cursors.append(batch.next_cursor)
        has_more_flags.append(batch.has_more)
        cursor = batch.next_cursor
        if not batch.has_more:
            break

    expected_ids = [f"syn-1-{n:05d}" for n in range(1, 26)]
    assert all_ids == expected_ids
    # cursors at end of each full batch
    assert "10" in cursors
    assert "20" in cursors
    assert "25" in cursors
    assert has_more_flags[-1] is False


# ── Overlap ───────────────────────────────────────────────────────────────────

def test_overlap_redelivers_and_still_advances() -> None:
    adapter = _make(total=25, overlap=3)
    cursor: str | None = None
    prev_cursor: str | None = None

    for _ in range(10):  # safety limit
        batch = adapter.fetch(cursor, limit=10)
        # cursor must strictly advance while has_more
        if prev_cursor is not None and batch.has_more:
            assert batch.next_cursor is not None
            assert int(batch.next_cursor) > int(prev_cursor), (
                f"cursor did not advance: {prev_cursor!r} → {batch.next_cursor!r}"
            )
        prev_cursor = cursor
        cursor = batch.next_cursor
        if not batch.has_more:
            break


def test_overlap_not_smaller_than_limit_rejected() -> None:
    adapter_eq = _make(overlap=10, total=25)
    with pytest.raises(AdapterConfigError, match="overlap must be smaller than limit"):
        adapter_eq.fetch(None, limit=10)

    adapter_gt = _make(overlap=11, total=25)
    with pytest.raises(AdapterConfigError, match="overlap must be smaller than limit"):
        adapter_gt.fetch(None, limit=10)


# ── Construction validation ───────────────────────────────────────────────────

def test_negative_or_non_integer_options_rejected() -> None:
    with pytest.raises(AdapterConfigError):
        SyntheticAdapter({"seed": "-1"})
    with pytest.raises(AdapterConfigError):
        SyntheticAdapter({"total": "abc"})
    with pytest.raises(AdapterConfigError):
        SyntheticAdapter({"overlap": "-5"})
    with pytest.raises(AdapterConfigError):
        SyntheticAdapter({"fail_at_batch": "0"})


# ── Invalid cursor ────────────────────────────────────────────────────────────

def test_invalid_cursor_raises_source_error() -> None:
    from olo.adapters import SourceError

    adapter = _make(total=25)
    with pytest.raises(SourceError):
        adapter.fetch("abc", limit=10)
    with pytest.raises(SourceError):
        adapter.fetch("-1", limit=10)
    with pytest.raises(SourceError):
        adapter.fetch("26", limit=10)


# ── Empty final batch ─────────────────────────────────────────────────────────

def test_empty_final_batch_at_end() -> None:
    adapter = _make(total=25)
    batch = adapter.fetch("25", limit=10)
    assert batch.signals == ()
    assert batch.next_cursor == "25"
    assert batch.has_more is False


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_total_zero() -> None:
    adapter = _make(total=0)
    batch = adapter.fetch(None, limit=10)
    assert batch.signals == ()
    assert batch.has_more is False
    assert batch.next_cursor is None


def test_fail_at_batch() -> None:
    from olo.adapters import SourceError

    adapter = _make(total=25, fail_at_batch=2)
    # First call succeeds
    adapter.fetch(None, limit=10)
    # Second call raises
    with pytest.raises(SourceError, match="programmed failure"):
        adapter.fetch("10", limit=10)


def test_limit_zero_rejected() -> None:
    adapter = _make(total=25)
    with pytest.raises(AdapterConfigError, match="limit must be ≥ 1"):
        adapter.fetch(None, limit=0)


def test_exact_multiple_no_spurious_has_more() -> None:
    """total=20, limit=10: last batch cursor=20, has_more=False, then empty."""
    adapter = _make(total=20)
    b1 = adapter.fetch(None, limit=10)
    assert b1.next_cursor == "10"
    assert b1.has_more is True
    b2 = adapter.fetch("10", limit=10)
    assert b2.next_cursor == "20"
    assert b2.has_more is False
    # one more call returns empty final batch
    b3 = adapter.fetch("20", limit=10)
    assert b3.signals == ()
    assert b3.has_more is False
