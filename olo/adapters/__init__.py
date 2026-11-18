"""Olo AI — MonitorAdapter ABC, FetchBatch, error types, adapter registry.

§5.3: adapter interface contract.
No side effects at import time.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from olo.signals import NormalizedSignal


# ── Error types ───────────────────────────────────────────────────────────────

class SourceError(Exception):
    """Raised by an adapter when the upstream source is temporarily unavailable
    or returns an unexpected error.  The runner records a failed run; the next
    scheduled tick will retry from the last committed checkpoint.
    """


class AdapterConfigError(ValueError):
    """Raised when adapter options are invalid or the adapter cannot be configured.
    Distinct from SourceError: configuration problems should not be retried
    without a human fix.
    """


# ── FetchBatch ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FetchBatch:
    """The result of one ``MonitorAdapter.fetch()`` call.

    Contract enforced by the runner (§5.7 step 4):
    - If ``has_more`` is True, ``next_cursor`` MUST differ from the cursor
      passed to ``fetch`` (the runner raises ``CursorStalled`` otherwise).
    - An empty ``signals`` tuple is valid only when ``has_more`` is False.
    - ``next_cursor`` is None only when ``total == 0`` and nothing was ever
      fetched; the runner skips the checkpoint write in that case (§5.7 step 3).

    pending_audit_events: adapter-generated audit events to be committed
    atomically with signals and the checkpoint inside ingest_batch.  Each
    entry is a dict with keys: actor, action, detail (all strings).  Optional
    subject_type and subject_id keys are forwarded to _audit.
    The runner passes these straight through to ingest_batch; adapters must
    NOT write them to the DB directly (that would break atomicity).
    """

    signals: tuple[NormalizedSignal, ...]
    next_cursor: str | None
    has_more: bool
    pending_audit_events: tuple[dict, ...] = ()


# ── MonitorAdapter ABC ────────────────────────────────────────────────────────

class MonitorAdapter(ABC):
    """Abstract base for all business signal adapters.

    Adapters are read-only with respect to Olo state: they must not access
    the Olo DB, write checkpoints, or call back into the runner.
    """

    #: Registry identifier — must match ``^[a-z0-9_]{1,32}$``.
    source: str  # ClassVar declared by concrete subclasses

    @abstractmethod
    def fetch(self, cursor: str | None, *, limit: int) -> FetchBatch:
        """Return up to *limit* (≥ 1) signals after *cursor* (None = from start).

        Contract:
        - Read-only: no DB access, no checkpoint writes.
        - May re-deliver already-seen signals (at-least-once sources are normal).
        - If ``has_more`` is True, ``next_cursor`` MUST differ from *cursor*.
        - An empty batch is valid only with ``has_more=False`` (end of data).

        Raises:
            SourceError: source temporarily unavailable.
            AdapterConfigError: adapter misconfigured.
        """


# ── Registry ──────────────────────────────────────────────────────────────────

def _make_registry() -> dict[str, Callable[[Mapping[str, str]], MonitorAdapter]]:
    """Build the ADAPTERS table lazily to avoid circular imports."""
    from olo.adapters.synthetic import SyntheticAdapter

    def _synthetic_factory(options: Mapping[str, str]) -> MonitorAdapter:
        return SyntheticAdapter(options)

    from olo.adapters.gmail import GmailAdapter

    def _gmail_factory(options: Mapping[str, str]) -> MonitorAdapter:
        return GmailAdapter(options)

    from olo.adapters.gcal import GcalAdapter

    def _gcal_factory(options: Mapping[str, str]) -> MonitorAdapter:
        return GcalAdapter(options)

    return {
        "synthetic": _synthetic_factory,
        "gmail": _gmail_factory,
        "gcal": _gcal_factory,
    }


# Populated at first call; module-level dict is intentionally mutable so tests
# can inject fakes without import-time side effects.
ADAPTERS: dict[str, Callable[[Mapping[str, str]], MonitorAdapter]] = {}


def _ensure_registry() -> None:
    if not ADAPTERS:
        ADAPTERS.update(_make_registry())


def get_adapter(name: str, options: Mapping[str, str]) -> MonitorAdapter:
    """Return a configured adapter for *name*.

    Raises:
        AdapterConfigError: unknown adapter name or invalid options.
    """
    _ensure_registry()
    factory = ADAPTERS.get(name)
    if factory is None:
        known = ", ".join(sorted(ADAPTERS))
        raise AdapterConfigError(
            f"Unknown adapter {name!r}. Known adapters: {known}"
        )
    return factory(options)
