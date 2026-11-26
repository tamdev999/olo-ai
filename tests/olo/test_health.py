"""Pipeline health (olo.health): persisted per source and stage, visible in
status, and never reported as success when the pipeline is degraded.

The model and Hindsight are mocks (tests/olo/test_s5_pipeline); no network.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from olo.health import assess, backlog, record_run
from olo.store import connect, migrate
from tests.olo.test_s5_pipeline import COMMIT_BODY, Pipeline, content

pytestmark = pytest.mark.not_integration


def _rows(p: Pipeline) -> dict[str, tuple]:
    conn = connect(p.db)
    rows = conn.execute("SELECT stage, status, needs_operator, consecutive_failures, detail"
                        " FROM pipeline_health").fetchall()
    conn.close()
    return {r[0]: tuple(r)[1:] for r in rows}


class TestRecordedHealth:
    def test_ingest_only_installation_records_disabled_stages_and_no_operator_items(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        r = p.run(content("m1", sent=True, body=COMMIT_BODY, day=1), commitment=False, projection=False)
        rows = _rows(p)
        assert rows["commitment"][0] == rows["projection"][0] == "disabled"
        assert "operator" not in rows
        assert r.degraded == () and r.attention_total == 0

    def test_healthy_pipeline_is_ok_everywhere(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        r = p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
        assert {k: v[0] for k, v in _rows(p).items()} == {
            "ingest": "ok", "identity": "ok", "commitment": "ok", "projection": "ok",
            "operator": "ok"}
        assert r.degraded == ()

    def test_unprepared_bank_is_degraded_not_success(self, tmp_path) -> None:
        from olo.m3.hindsight.client import HindsightUnavailable
        p = Pipeline(tmp_path)
        p.hindsight.config_error = HindsightUnavailable("connection refused")
        r = p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
        assert any("bank_unprepared" in d for d in r.degraded)
        assert _rows(p)["projection"][0] == "degraded"

    def test_stats_unavailable_and_budget_are_degraded(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.guard.unavailable = True
        r = p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
        assert any("stats_unavailable" in d for d in r.degraded)

    def test_extraction_errors_degrade_and_failures_accumulate(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.model.fail.add(COMMIT_BODY)
        r1 = p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
        r2 = p.run()
        assert any("model_error" in d for d in r1.degraded + r2.degraded)
        assert _rows(p)["commitment"][2] == 2                  # consecutive failures

    def test_exhausted_extraction_is_a_new_operator_item_once(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.model.fail.add(COMMIT_BODY)
        results = [p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))]
        results += [p.run() for _ in range(5)]
        exhausted = [r for r in results if r.attention_new]
        assert len(exhausted) == 1 and exhausted[0].attention_total == 1
        assert results[-1].attention_total == 1 and results[-1].attention_new == 0
        assert json.loads(_rows(p)["operator"][3])["extraction_exhausted"] == 1

    def test_health_survives_a_restart(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.guard.unavailable = True
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
        conn = connect(p.db)                                  # a new process
        conditions = assess(conn, datetime.now(UTC))
        conn.close()
        assert any(c.severity == "degraded" and c.stage.endswith("/projection") for c in conditions)


class TestFailedFetch:
    def test_ingested_signals_reach_the_ledger_when_the_next_fetch_fails(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.model.fail.add(COMMIT_BODY)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))   # ingested; extraction failed
        assert p.commitments() == []
        p.model.fail.clear()

        def broken(cursor, *, limit):
            raise ConnectionError("google unavailable")

        p.adapter.fetch = broken
        with pytest.raises(ConnectionError):
            p.run()
        assert len(p.commitments()) == 1                       # processed despite the failure
        rows = _rows(p)
        assert rows["ingest"][0] == "failed" and rows["commitment"][0] == "ok"


class TestConditions:
    def _db(self, tmp_path: Path):
        conn = connect(tmp_path / "olo.db")
        migrate(conn)
        return conn

    def test_oauth_failure_needs_an_operator_and_the_source_is_stalled(self, tmp_path) -> None:
        conn = self._db(tmp_path)
        now = datetime(2026, 11, 26, 12, tzinfo=UTC)
        record_run(conn, "gmail_acme", "2026-11-26T09:00:00Z", run_status="succeeded", error=None,
                   fetched=1, inserted=1, identity=(1, 0, 0), commitment_enabled=False,
                   commitment={}, projection_enabled=False, projection={})
        record_run(conn, "gmail_acme", "2026-11-26T11:55:00Z", run_status="failed",
                   error="GoogleAuthRevoked: google auth revoked or expired", fetched=0,
                   inserted=0, identity=(0, 0, 0), commitment_enabled=False, commitment={},
                   projection_enabled=False, projection={})
        conditions = {(c.severity, c.stage): c.message for c in assess(conn, now)}
        assert conditions[("attention", "gmail_acme/ingest")].startswith("oauth")
        assert ("stalled", "gmail_acme/ingest") in conditions
        assert ("degraded", "gmail_acme/ingest") in conditions

    def test_recent_success_is_not_stalled(self, tmp_path) -> None:
        conn = self._db(tmp_path)
        record_run(conn, "gcal_acme", "2026-11-26T11:50:00Z", run_status="succeeded", error=None,
                   fetched=0, inserted=0, identity=(0, 0, 0), commitment_enabled=False,
                   commitment={}, projection_enabled=False, projection={})
        assert assess(conn, datetime(2026, 11, 26, 12, tzinfo=UTC)) == []
        assert assess(conn, datetime(2026, 11, 26, 12, tzinfo=UTC) + timedelta(hours=3))[0].severity == "stalled"

    def test_backlog_counts_come_from_the_owning_tables(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
        conn = connect(p.db)
        b = backlog(conn)
        conn.close()
        assert b["commitment"] == {"resolved": 1}
        assert b["projection_outbox"] == {"confirmed": 2} and b["ledger_unprojected"] == 0
