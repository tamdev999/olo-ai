"""Every calendar on every run (olo.adapters.gcal sync cycle).

Regression for the live Google acceptance of 2026-11-27: the account had
four readable calendars and Olo synced one per run (round-robin), so a
change to the primary calendar surfaced only on every fourth run and quick
successive edits collapsed — C2's reschedule, C3's original title and C6's
creation were never seen.  Each scenario below runs on that account shape
(primary + three secondaries) through the real worker (run_source) and the
deterministic calendar commitment stage.

Incremental sync reports an event's state at fetch time (the fake does the
same, tests/olo/fake_google.py), so a change is seen per run, not per edit:
``test_edits_between_runs_converge_on_the_final_state`` pins that boundary.
"""
from __future__ import annotations

import json

from olo.adapters.gcal import _MAX_CONSECUTIVE_CAL_PAGES
from olo.ledger import STATUS_CANCELLED, STATUS_SCHEDULED
from olo.runner import run_source
from olo.store import connect
from tests.olo.test_calendar_commitments import calendar, event

SECONDARIES = ("team@group.calendar.test", "family@group.calendar.test", "ops@group.calendar.test")


def _live_shape(cal) -> None:
    for c in SECONDARIES:
        cal.fake.add_secondary_calendar_with_events(c, f"Calendar {c}", event_ids=[f"x-{c[:4]}"])
    cal.run()                                                       # bootstrap every calendar


def _moved(eid: str, day: str, hour: int, *, updated: str) -> dict:
    return event(eid, day, updated=updated,
                 start={"dateTime": f"{day}T{hour:02d}:00:00-05:00", "timeZone": "America/New_York"},
                 end={"dateTime": f"{day}T{hour + 1:02d}:00:00-05:00", "timeZone": "America/New_York"})


def _outcomes(cal, eid: str) -> list[str]:
    return [r[0] for r in cal.query(
        "SELECT sr.commitment_outcome FROM signal_resolution sr JOIN signals s"
        " ON s.external_id=sr.signal_id AND s.source=sr.source"
        " WHERE json_extract(s.payload,'$.event_id')=? ORDER BY s.id", (eid,))]


class TestEveryRunSeesEveryCalendar:
    def test_c2_reschedule_then_same_day_move(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            _live_shape(cal)
            cal.put(event("c2", "2026-12-01", updated="2026-11-27T10:00:00.000Z"))
            cal.run()
            cal.put(event("c2", "2026-12-02", updated="2026-11-27T10:05:00.000Z"))
            cal.run()
            cal.put(_moved("c2", "2026-12-02", 17, updated="2026-11-27T10:10:00.000Z"))
            cal.run()
            assert cal.versions(cal.commitment("c2")) == [
                ("commit", "2026-12-01", None),
                ("confirm_change", "2026-12-02", "2026-12-01"),
                ("confirm_change", "2026-12-02", "2026-12-02"),
            ]

    def test_c3_title_only_edit(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            _live_shape(cal)
            cal.put(event("c3", "2026-12-03", updated="2026-11-27T10:00:00.000Z", summary="Planning"))
            cal.run()
            cal.put(event("c3", "2026-12-03", updated="2026-11-27T10:05:00.000Z",
                          summary="Planning (agenda updated)"))
            cal.run()
            assert _outcomes(cal, "c3") == ["applied", "no_change"]
            assert cal.versions(cal.commitment("c3")) == [("commit", "2026-12-03", None)]

    def test_c6_created_then_deleted(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            _live_shape(cal)
            cal.put(event("c6", "2026-12-04", updated="2026-11-27T10:00:00.000Z"))
            cal.run()
            # Live Google returned the deleted event as the full resource.
            cal.put(event("c6", "2026-12-04", updated="2026-11-27T10:05:00.000Z", status="cancelled"))
            cal.run()
            cid = cal.commitment("c6")
            assert cal.versions(cid) == [("commit", "2026-12-04", None), ("cancel", None, "2026-12-04")]
            assert cal.state(cid)["status"] == STATUS_CANCELLED

    def test_a_change_on_any_calendar_arrives_on_the_next_run(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            _live_shape(cal)
            for n, c in enumerate(SECONDARIES):
                cal.fake.calendar._extra_calendar_states[c].add_event(f"new-{n}", summary="New")
            cal.put(event("p1", "2026-12-05", updated="2026-11-27T10:00:00.000Z"))
            before = cal.query("SELECT COUNT(*) FROM signals")[0][0]
            cal.run()
            got = cal.query("SELECT DISTINCT json_extract(payload,'$.calendar_id') FROM signals"
                            " WHERE id > ? AND kind LIKE 'calendar.event.%'", (before,))
            assert {g[0] for g in got} == {"primary", *SECONDARIES}

    def test_edits_between_runs_converge_on_the_final_state(self, tmp_path) -> None:
        """Two edits with no run between them: Google (and the fake) report the
        event once, in its final state.  The ledger converges on it; the
        intermediate date was never observable and is not reconstructed."""
        with calendar(tmp_path) as cal:
            _live_shape(cal)
            cal.put(event("e", "2026-12-01", updated="2026-11-27T10:00:00.000Z"))
            cal.run()
            cal.put(event("e", "2026-12-02", updated="2026-11-27T10:01:00.000Z"))
            cal.put(event("e", "2026-12-03", updated="2026-11-27T10:02:00.000Z"))
            cal.run()
            cid = cal.commitment("e")
            assert cal.versions(cid) == [("commit", "2026-12-01", None),
                                         ("confirm_change", "2026-12-03", "2026-12-01")]
            assert cal.state(cid)["status"] == STATUS_SCHEDULED


def _health(db) -> dict:
    conn = connect(db)
    try:
        row = conn.execute("SELECT status, detail FROM pipeline_health WHERE stage='ingest'").fetchone()
        return {"status": row[0], **json.loads(row[1])}
    finally:
        conn.close()


class TestBudgetAndFairness:
    def test_a_budget_short_of_every_calendar_is_incomplete_and_resumes(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            _live_shape(cal)
            for n, c in enumerate(SECONDARIES):
                cal.fake.calendar._extra_calendar_states[c].add_event(f"b-{n}", summary="B")
            cal.put(event("p", "2026-12-06", updated="2026-11-27T10:00:00.000Z"))
            r = run_source(cal.adapter, db_path=cal.db, limit=5, max_batches=2)
            assert r.status == "partial"
            h = _health(cal.db)
            assert h["status"] == "degraded" and h["coverage"]["complete"] is False
            assert h["coverage"]["pending"] >= 1 and h["coverage"]["calendars"] == 4
            assert any("incomplete" in d for d in r.degraded)
            r = run_source(cal.adapter, db_path=cal.db, limit=5, max_batches=50)
            assert r.status == "succeeded" and _health(cal.db)["status"] == "ok"
            got = cal.query("SELECT DISTINCT json_extract(payload,'$.calendar_id') FROM signals"
                            " WHERE json_extract(payload,'$.event_id') IN ('p','b-0','b-1','b-2')")
            assert {g[0] for g in got} == {"primary", *SECONDARIES}    # nothing starved

    def test_a_large_backlog_yields_to_the_other_calendars(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            _live_shape(cal)
            # The backlog is on the calendar the cycle visits first; the small
            # change is on the one it visits last.
            first, last = min(SECONDARIES), max(SECONDARIES)
            big = cal.fake.calendar._extra_calendar_states[first]
            big.page_size = 1
            for n in range(_MAX_CONSECUTIVE_CAL_PAGES + 6):
                big.add_event(f"big-{n}", summary="Backlog")
            cal.fake.calendar._extra_calendar_states[last].add_event("small", summary="S")
            cursor = cal.query("SELECT cursor FROM sync_checkpoints")[0][0]
            order = []
            for _ in range(60):
                b = cal.adapter.fetch(cursor, limit=1)
                order += [s.payload["calendar_id"] for s in b.signals]
                cursor = b.next_cursor
                if not b.has_more:
                    break
            assert not b.has_more and json.loads(cursor)["cycle"] == []
            last_big = max(i for i, c in enumerate(order) if c == first)
            assert order.index(last) < last_big                     # served before the backlog ends
            assert order.count(first) == _MAX_CONSECUTIVE_CAL_PAGES + 6

    def test_a_round_robin_cursor_from_before_the_cycle_still_syncs_everything(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            _live_shape(cal)
            conn = connect(cal.db)
            [(source, raw)] = conn.execute("SELECT source, cursor FROM sync_checkpoints").fetchall()
            old = json.loads(raw)
            old.pop("cycle", None)
            old["next_cal"] = 2                                     # the pre-cycle index
            conn.execute("UPDATE sync_checkpoints SET cursor=? WHERE source=?", (json.dumps(old), source))
            conn.commit()
            conn.close()
            for n, c in enumerate(SECONDARIES):
                cal.fake.calendar._extra_calendar_states[c].add_event(f"m-{n}", summary="M")
            cal.put(event("pm", "2026-12-07", updated="2026-11-27T10:00:00.000Z"))
            cal.run()
            got = cal.query("SELECT DISTINCT json_extract(payload,'$.calendar_id') FROM signals"
                            " WHERE json_extract(payload,'$.event_id') IN ('pm','m-0','m-1','m-2')")
            assert {g[0] for g in got} == {"primary", *SECONDARIES}
            [(raw,)] = cal.query("SELECT cursor FROM sync_checkpoints")
            assert "next_cal" not in json.loads(raw)
