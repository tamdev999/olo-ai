"""tests/olo/test_projection_guard.py — S5 part 3: Hindsight work is bounded.

The projection stage must:
  * submit nothing without readable usage statistics (fail closed);
  * stop and cancel its in-flight operations when the usage budget is hit;
  * respect per-run and per-window document caps, including across restarts
    (the window and its baseline live in olo.db);
  * drive operations to completed / failed / cancelled / lost, cancel stale
    ones, never resubmit in-flight documents, and stop retrying a document
    after max_attempts_per_doc;
  * refuse to project a business into another business's bank.

The model and Hindsight are mocks (see test_s5_pipeline); no network.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from olo.projection import ProjectionLimits, ProjectionRefused
from olo.store import connect
from tests.olo.test_s5_pipeline import COMMIT_BODY, Pipeline, content

pytestmark = pytest.mark.not_integration


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def _signals(n: int) -> list:
    return [content(f"m{i}", sent=True, day=1 + i % 20,
                    body=f"Note {i}: thanks for the call today.") for i in range(n)]


def _ops(p: Pipeline, kind: str = "retain") -> dict[str, str]:
    conn = connect(p.db)
    rows = conn.execute("SELECT operation_id, status FROM hindsight_operations WHERE kind=?",
                        (kind,)).fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def _outbox(p: Pipeline) -> dict[str, str]:
    conn = connect(p.db)
    rows = conn.execute("SELECT signal_id, status FROM hindsight_projection").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


@pytest.fixture()
def clock() -> Clock:
    return Clock()


class TestUsageGuard:
    def test_stats_unavailable_submits_nothing(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.guard.unavailable = True
        r = p.run(*_signals(3), clock=clock)
        assert r.status == "succeeded"
        assert r.projection["stats_unavailable"] == 1 and r.projection["submitted"] == 0
        assert p.hindsight.submits == [] and _outbox(p) == {}

    def test_budget_exceeded_stops_and_cancels_in_flight(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.next_status = "processing"
        p.run(*_signals(2), clock=clock)                     # submitted, still running
        assert set(_ops(p).values()) == {"submitted"}
        p.guard.exceeded = True
        r = p.run(content("m9", sent=True, day=9, body="One more note."), clock=clock)
        assert r.projection["budget_exceeded"] == 1
        assert r.projection["submitted"] == 0
        assert set(_ops(p).values()) == {"cancelled"} and p.hindsight.cancelled
        assert set(_outbox(p).values()) == {"failed"}

    def test_spend_survives_restart_and_server_trace_retention(self, tmp_path, clock) -> None:
        """Hindsight 1.2.1 ages trace rows out after a day: spend the server
        no longer reports must still count for the open window (audit D11)."""
        p = Pipeline(tmp_path)
        hour = clock.now.strftime("%Y-%m-%dT%H:00:00Z")
        p.guard.hours = {hour: (0, 5_000_000)}                # $6 at the output price
        r1 = p.run(*_signals(1), clock=clock)
        assert not r1.projection["budget_exceeded"]
        clock.advance(hours=2)
        later = clock.now.strftime("%Y-%m-%dT%H:00:00Z")
        p.guard.hours = {later: (0, 2_500_000)}               # first hour aged out; +$3
        r2 = p.run(content("m9", sent=True, day=9, body="One more note."), clock=clock)  # "restart"
        assert r2.projection["budget_exceeded"] == 1          # $6 + $3 >= $8
        assert r2.projection["window_spend_usd"] == pytest.approx(9.0)
        clock.advance(hours=25)                               # new window: old hours excluded
        p.guard.hours = {}
        r3 = p.run(clock=clock)
        assert not r3.projection.get("budget_exceeded")

    def test_spend_is_never_lowered_by_a_shrinking_bucket(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        hour = clock.now.strftime("%Y-%m-%dT%H:00:00Z")
        p.guard.hours = {hour: (0, 6_000_000)}                # $7.20
        p.run(*_signals(1), clock=clock)
        p.guard.hours = {hour: (0, 1_000_000)}                # server now reports less
        r = p.run(content("m9", sent=True, day=9, body="One more note."), clock=clock)
        assert r.projection["window_spend_usd"] == pytest.approx(7.2)


class TestDocumentCaps:
    def test_per_run_cap_defers_the_rest(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.limits = ProjectionLimits(max_docs_per_run=2)
        r1 = p.run(*_signals(5), clock=clock)
        assert r1.projection["submitted"] == 2 and r1.projection["deferred"] == 3
        r2 = p.run(clock=clock)
        assert r2.projection["submitted"] == 2
        r3 = p.run(clock=clock)
        assert r3.projection["submitted"] == 1 and r3.projection["deferred"] == 0

    def test_prose_cap_is_separate(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.limits = ProjectionLimits(max_prose_docs_per_run=1)
        r = p.run(*_signals(3), clock=clock)
        assert r.projection["submitted"] == 1 and r.projection["deferred"] == 2

    def test_window_cap_is_not_reset_by_restart(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.limits = ProjectionLimits(max_docs_per_window=3)
        assert p.run(*_signals(5), clock=clock).projection["submitted"] == 3
        restarted = p.run(clock=clock)                        # same window
        assert restarted.projection["submitted"] == 0 and restarted.projection["deferred"] == 2
        clock.advance(hours=25)
        assert p.run(clock=clock).projection["submitted"] == 2


class TestOperationLifecycle:
    def test_in_flight_documents_are_not_resubmitted(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.next_status = "processing"
        p.run(*_signals(2), clock=clock)
        before = len(p.hindsight.submits)
        r = p.run(clock=clock)
        assert len(p.hindsight.submits) == before and r.projection["submitted"] == 0
        assert r.projection["unchanged"] == 2

    def test_stale_operation_is_cancelled_then_retried(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.next_status = "processing"
        p.run(*_signals(1), clock=clock)
        clock.advance(minutes=61)
        p.hindsight.next_status = "completed"
        r = p.run(clock=clock)
        assert r.projection["cancelled"] == 1 and p.hindsight.cancelled
        assert r.projection["submitted"] == 1                 # retried after cancel
        assert sorted(_ops(p).values()) == ["cancelled", "completed"]
        assert set(_outbox(p).values()) == {"confirmed"}

    def test_failed_documents_stop_after_max_attempts(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.limits = ProjectionLimits(max_attempts_per_doc=2)
        p.hindsight.next_status = "failed"
        p.run(*_signals(1), clock=clock)                      # attempt 1 fails
        p.run(clock=clock)                                    # attempt 2 fails
        r = p.run(clock=clock)
        assert r.projection["submitted"] == 0 and r.projection["retries_exhausted"] == 1
        assert list(_ops(p).values()).count("failed") == 2

    def test_submit_error_is_recorded_and_retried(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.raise_on_retain = True
        r1 = p.run(*_signals(1), clock=clock)
        assert r1.projection["failed"] == 1 and set(_ops(p).values()) == {"failed"}
        p.hindsight.raise_on_retain = False
        r2 = p.run(clock=clock)
        assert r2.projection["submitted"] == 1 and r2.projection["confirmed"] == 1

    def test_confirmed_documents_are_never_resubmitted(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1), clock=clock)
        n = len(p.hindsight.submits)
        for _ in range(3):
            p.run(clock=clock)
        assert len(p.hindsight.submits) == n


class TestBankIsolation:
    def test_database_bound_to_first_bank(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.run(*_signals(1), clock=clock)
        p.bank = "olo-someone-else"
        with pytest.raises(ProjectionRefused):
            p.run(clock=clock)
        assert "olo-someone-else" not in p.hindsight.retained
