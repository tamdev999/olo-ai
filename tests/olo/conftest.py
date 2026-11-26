"""Olo test fixtures.

Provides: temp_home, fake_clock, in-test adapter fakes.
No autouse fixtures that leak outside tests/olo/.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import datetime, timezone, timedelta, UTC
from pathlib import Path
from typing import Optional

import pytest

from olo.adapters import AdapterConfigError, FetchBatch, MonitorAdapter, SourceError
from olo.signals import NormalizedSignal


def pytest_configure(config: pytest.Config) -> None:
    # `live` is registered in pyproject.toml (a Hermes core file); only the
    # Olo-specific marker is registered here.
    config.addinivalue_line(
        "markers",
        "not_integration: Olo unit test that needs no external service",
    )


_REPO_ROOT = str(Path(__file__).resolve().parents[2])


@pytest.fixture(autouse=True)
def _subprocesses_import_this_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every child Python (cron shims, CLI runs, helper scripts) imports this
    checkout. A script run by path gets only its own directory on sys.path,
    so imports fall through to the venv's editable install, which can point
    at another copy (in WSL, /root/olo_linux_venv maps to a stale
    /root/olo-linux) and silently test that code instead."""
    current = os.environ.get("PYTHONPATH", "")
    if _REPO_ROOT not in current.split(os.pathsep):
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in (_REPO_ROOT, current) if p))


# ── FakeClock ─────────────────────────────────────────────────────────────────

class FakeClock:
    """Settable/advanceable UTC clock for lease-expiry tests."""

    def __init__(self, start: Optional[datetime] = None) -> None:
        self._now = start or datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)

    def set(self, dt: datetime) -> None:
        self._now = dt


@pytest.fixture()
def fake_clock() -> FakeClock:
    return FakeClock()


# ── temp_home ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HERMES_HOME to a fresh temp directory."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


# ── In-test adapter fakes ─────────────────────────────────────────────────────

class StallingAdapter(MonitorAdapter):
    """Always returns has_more=True with the SAME cursor — triggers CursorStalled."""

    source = "synthetic"

    def fetch(self, cursor: str | None, *, limit: int) -> FetchBatch:
        from olo.signals import NormalizedSignal
        from datetime import datetime, timezone
        # Return one signal but keep cursor the same
        s = NormalizedSignal(
            source="synthetic",
            external_id="syn-1-00001",
            kind="payment.failed",
            occurred_at=datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC),
            subject=None,
            payload={"seq": 1},
        )
        return FetchBatch(signals=(s,), next_cursor=cursor, has_more=True)


class EmptyButHasMoreAdapter(MonitorAdapter):
    """Returns empty signals with has_more=True — triggers CursorStalled."""

    source = "synthetic"

    def fetch(self, cursor: str | None, *, limit: int) -> FetchBatch:
        return FetchBatch(signals=(), next_cursor=cursor, has_more=True)


class OversizedBatchAdapter(MonitorAdapter):
    """Returns more signals than limit — triggers AdapterContractError."""

    source = "synthetic"

    def __init__(self, limit_to_exceed: int = 5) -> None:
        self._limit = limit_to_exceed

    def fetch(self, cursor: str | None, *, limit: int) -> FetchBatch:
        from datetime import datetime, timezone
        signals = tuple(
            NormalizedSignal(
                source="synthetic",
                external_id=f"syn-1-{n:05d}",
                kind="payment.failed",
                occurred_at=datetime(2026, 1, 1, 0, n, 0, tzinfo=UTC),
                subject=None,
                payload={"seq": n},
            )
            for n in range(1, self._limit + 2)  # always 1 more than limit
        )
        return FetchBatch(signals=signals, next_cursor=str(self._limit + 1), has_more=False)


class WrongSourceAdapter(MonitorAdapter):
    """Returns signals with a different source field — triggers AdapterContractError."""

    source = "synthetic"

    def fetch(self, cursor: str | None, *, limit: int) -> FetchBatch:
        from datetime import datetime, timezone
        s = NormalizedSignal(
            source="other_source",
            external_id="syn-1-00001",
            kind="payment.failed",
            occurred_at=datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC),
            subject=None,
            payload={"seq": 1},
        )
        return FetchBatch(signals=(s,), next_cursor="1", has_more=False)


class BlockingAdapter(MonitorAdapter):
    """Blocks inside fetch() until released by an Event."""

    source = "synthetic"

    def __init__(self, total: int = 25) -> None:
        self._total = total
        self._block_event = threading.Event()
        self._released_event = threading.Event()
        self._unblocked = False

    def release(self) -> None:
        """Allow fetch to proceed."""
        self._block_event.set()

    def wait_blocked(self, timeout: float = 10.0) -> None:
        """Wait until fetch is blocking."""
        self._released_event.wait(timeout)

    def fetch(self, cursor: str | None, *, limit: int) -> FetchBatch:
        from datetime import datetime, timezone
        self._released_event.set()  # signal that we've entered fetch
        self._block_event.wait()   # block until released
        from olo.adapters.synthetic import SyntheticAdapter
        real = SyntheticAdapter({"total": str(self._total)})
        return real.fetch(cursor, limit=limit)
