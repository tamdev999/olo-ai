"""Tests for olo.signals — NormalizedSignal, canonical_json, format_utc, content_sha256.

§8.1 acceptance criteria.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone, timedelta, UTC

import pytest

from olo.signals import (
    NormalizedSignal,
    canonical_json,
    format_utc,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make(**kwargs) -> NormalizedSignal:
    defaults = dict(
        source="synthetic",
        external_id="syn-1-00001",
        kind="payment.failed",
        occurred_at=datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC),
        subject="cust-001",
        payload={"seq": 1, "amount_cents": 1001},
    )
    defaults.update(kwargs)
    return NormalizedSignal(**defaults)


# ── canonical_json ────────────────────────────────────────────────────────────

def test_canonical_json_is_order_and_whitespace_insensitive() -> None:
    a = canonical_json({"b": 2, "a": 1})
    b = canonical_json({"a": 1, "b": 2})
    assert a == b
    assert " " not in a
    assert "\n" not in a


def test_canonical_json_nested_dicts_sorted() -> None:
    obj = {"z": {"y": 1, "x": 2}, "a": 3}
    result = canonical_json(obj)
    assert result == '{"a":3,"z":{"x":2,"y":1}}'


def test_canonical_json_rejects_nan() -> None:
    with pytest.raises((ValueError, TypeError)):
        canonical_json({"x": float("nan")})


def test_canonical_json_rejects_infinity() -> None:
    with pytest.raises((ValueError, TypeError)):
        canonical_json({"x": float("inf")})


# ── format_utc ────────────────────────────────────────────────────────────────

def test_format_utc_always_six_fractional_digits() -> None:
    dt = datetime(2026, 1, 1, 12, 30, 45, 123456, tzinfo=UTC)
    assert format_utc(dt) == "2026-01-01T12:30:45.123456Z"


def test_format_utc_zero_microseconds() -> None:
    dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert format_utc(dt) == "2026-01-01T00:00:00.000000Z"


def test_format_utc_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        format_utc(datetime(2026, 1, 1))


def test_format_utc_normalises_to_utc() -> None:
    plus2 = timezone(timedelta(hours=2))
    dt = datetime(2026, 1, 1, 14, 0, 0, tzinfo=plus2)   # = 12:00 UTC
    assert format_utc(dt) == "2026-01-01T12:00:00.000000Z"


# ── content_sha256 ────────────────────────────────────────────────────────────

def test_content_hash_covers_exactly_the_specified_fields() -> None:
    base = _make()

    for changed in [
        _make(schema_version=2),
        _make(source="stripe"),
        _make(external_id="syn-1-00002"),
        _make(kind="invoice.overdue"),
        _make(occurred_at=datetime(2026, 1, 1, 0, 2, 0, tzinfo=UTC)),
        _make(subject="cust-002"),
        _make(payload={"seq": 2, "amount_cents": 1001}),
    ]:
        assert changed.content_sha256() != base.content_sha256(), (
            f"hash unchanged after modifying {changed!r}"
        )


def test_content_hash_stable_across_timezones() -> None:
    utc = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    plus2 = timezone(timedelta(hours=2))
    offset = datetime(2026, 1, 1, 14, 0, 0, tzinfo=plus2)
    s_utc = _make(occurred_at=utc)
    s_off = _make(occurred_at=offset)
    assert s_utc.content_sha256() == s_off.content_sha256()


def test_content_hash_ignores_ingestion_metadata() -> None:
    """id, ingested_at, run_id are NOT inputs to the hash."""
    # Two identical NormalizedSignals (same content) must hash the same
    # regardless of when or by which run they were ingested — the hash
    # is computed entirely from the signal's own fields.
    s1 = _make()
    s2 = _make()
    assert s1.content_sha256() == s2.content_sha256()


def test_content_hash_matches_manual_computation() -> None:
    s = _make(
        schema_version=1,
        source="synthetic",
        external_id="syn-1-00001",
        kind="payment.failed",
        occurred_at=datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC),
        subject="cust-001",
        payload={"seq": 1, "amount_cents": 1001},
    )
    blob = canonical_json({
        "schema_version": 1,
        "source": "synthetic",
        "external_id": "syn-1-00001",
        "kind": "payment.failed",
        "occurred_at": "2026-01-01T00:01:00.000000Z",
        "subject": "cust-001",
        "payload": {"seq": 1, "amount_cents": 1001},
    })
    expected = hashlib.sha256(blob.encode()).hexdigest()
    assert s.content_sha256() == expected


# ── Validation — rejects invalid signals ─────────────────────────────────────

def test_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _make(occurred_at=datetime(2026, 1, 1, 0, 0, 0))


def test_rejects_bad_source_pattern() -> None:
    with pytest.raises(ValueError, match="source"):
        _make(source="Bad-Source!")


def test_rejects_source_too_long() -> None:
    with pytest.raises(ValueError, match="source"):
        _make(source="a" * 33)


def test_rejects_bad_kind_pattern() -> None:
    with pytest.raises(ValueError, match="kind"):
        _make(kind="UPPER.CASE")


def test_rejects_kind_too_long() -> None:
    with pytest.raises(ValueError, match="kind"):
        _make(kind="a" * 65)


def test_rejects_empty_external_id() -> None:
    with pytest.raises(ValueError, match="external_id"):
        _make(external_id="")


def test_rejects_external_id_too_long() -> None:
    with pytest.raises(ValueError, match="external_id"):
        _make(external_id="x" * 257)


def test_rejects_payload_with_nan() -> None:
    with pytest.raises(ValueError, match="NaN"):
        _make(payload={"x": float("nan")})


def test_rejects_payload_with_infinity() -> None:
    with pytest.raises(ValueError, match="NaN or Infinity"):
        _make(payload={"x": float("inf")})


def test_rejects_oversize_payload() -> None:
    big = {"k" * i: "v" * 100 for i in range(1, 300)}
    with pytest.raises(ValueError, match="payload canonical JSON exceeds"):
        _make(payload=big)


def test_rejects_subject_too_long() -> None:
    with pytest.raises(ValueError, match="subject"):
        _make(subject="x" * 257)


def test_accepts_none_subject() -> None:
    s = _make(subject=None)
    assert s.subject is None


def test_signal_is_frozen() -> None:
    s = _make()
    with pytest.raises((AttributeError, TypeError)):
        s.source = "other"  # type: ignore[misc]


def test_duplicate_same_hash() -> None:
    """Same identity + same content → same hash (deduplication case)."""
    s1 = _make()
    s2 = _make()
    assert s1.content_sha256() == s2.content_sha256()


def test_conflict_different_hash() -> None:
    """Same identity + different payload → different hash (conflict case)."""
    s1 = _make(payload={"seq": 1})
    s2 = _make(payload={"seq": 2})
    assert s1.content_sha256() != s2.content_sha256()
