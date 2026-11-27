"""Calendar events → meeting commitments (olo.calendar_commitments).

The decision table is tested as a pure function; every other test drives the
real path — GcalAdapter against the loopback fake Google server (Calendar
API-shaped event resources), run_source ingest into SQLite, identity, the
deterministic calendar commitment stage, the ledger and the projection to a
fake Hindsight.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from olo.adapters.gcal import GcalAdapter
from olo.calendar_commitments import Current, Revision, decide, event_key
from olo.commitments import CommitmentStageConfig
from olo.ledger import STATUS_CANCELLED, STATUS_SCHEDULED, get_all_versions, get_current_state
from olo.projection import ProjectionConfig, ProjectionLimits
from olo.runner import run_source
from olo.store import connect
from tests.olo.fake_google import FakeGoogleServer
from tests.olo.test_s5_pipeline import Guard, Hindsight, Model

OWNER = "owner@biz.example"
ALICE = "alice@customer.example"
BOB = "bob@other.example"
CAROL = "carol@third.example"


def event(eid: str, day: str, *, updated: str, status: str = "confirmed",
          organiser_self: bool = True, guests=((ALICE, "accepted"),),
          my_response: str = "accepted", summary: str = "Site visit", **extra) -> dict:
    """A Calendar API v3 event resource as events.list returns it."""
    attendees = [{"email": OWNER, "self": True, "organizer": organiser_self,
                  "responseStatus": my_response}]
    attendees += [{"email": e, "responseStatus": r, **({"organizer": True}
                   if not organiser_self and i == 0 else {})} for i, (e, r) in enumerate(guests)]
    organiser = ({"email": OWNER, "self": True} if organiser_self
                 else {"email": guests[0][0], "displayName": "Customer"})
    res = {
        "kind": "calendar#event", "id": eid, "status": status, "summary": summary,
        "created": "2026-11-01T09:00:00.000Z", "updated": updated,
        "start": {"dateTime": f"{day}T15:00:00-05:00", "timeZone": "America/New_York"},
        "end": {"dateTime": f"{day}T16:00:00-05:00", "timeZone": "America/New_York"},
        "organizer": organiser, "attendees": attendees, "iCalUID": f"{eid}@google.com",
        "sequence": 0, "reminders": {"useDefault": True},
    }
    res.update(extra)
    return res


def cancelled(eid: str, *, updated: str, **extra) -> dict:
    """The minimal resource incremental sync returns for a deleted event."""
    return {"kind": "calendar#event", "id": eid, "status": "cancelled", "updated": updated, **extra}


class Calendar:
    """One business: the fake Google calendar and its pipeline."""

    def __init__(self, fake: FakeGoogleServer, tmp_path: Path) -> None:
        from olo.google.http import GoogleClient
        client = GoogleClient(fake.make_credentials("biz", gmail_readonly=True))
        client._access_token = fake.tokens.access_token
        self.fake = fake
        self.adapter = GcalAdapter({"account": "biz", "lookback_days": "30"}, client=client)
        self.db = tmp_path / "biz.db"
        self.model = Model({})
        self.hindsight = Hindsight()

    def put(self, *resources: dict) -> None:
        for r in resources:
            self.fake.calendar.put_event(r)

    def run(self):
        result = run_source(
            self.adapter, db_path=self.db,
            commitment=CommitmentStageConfig(model_fn=self.model),
            projection=ProjectionConfig(client=self.hindsight, bank_id="olo-biz",
                                        usage_guard=Guard(), limits=ProjectionLimits()),
        )
        assert result.status == "succeeded", result.error
        assert self.model.calls == []            # calendar commitments need no model
        return result

    def query(self, sql: str, params: tuple = ()) -> list:
        conn = connect(self.db)
        try:
            return [tuple(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def commitment(self, eid: str) -> str:
        [(cid,)] = self.query("SELECT id FROM commitments WHERE business_key=?",
                              (event_key("primary", eid),))
        return cid

    def state(self, cid: str) -> dict:
        conn = connect(self.db)
        try:
            return get_current_state(conn, cid)
        finally:
            conn.close()

    def versions(self, cid: str) -> list[tuple]:
        conn = connect(self.db)
        try:
            return [(v["action"], v["confirmed_date"], v["prior_confirmed_date"])
                    for v in get_all_versions(conn, cid)]
        finally:
            conn.close()

    def outcomes(self) -> list[tuple[str, str]]:
        return self.query(
            "SELECT s.kind, sr.commitment_outcome FROM signal_resolution sr JOIN signals s"
            " ON s.external_id=sr.signal_id AND s.source=sr.source"
            " WHERE s.kind LIKE 'calendar.event.%' ORDER BY s.occurred_at, s.id")


@contextmanager
def calendar(tmp_path: Path):
    with FakeGoogleServer() as fake:
        yield Calendar(fake, tmp_path)


# ── Decision table ────────────────────────────────────────────────────────────

def _rev(**kw) -> Revision:
    base = dict(key="gcal:primary:e1", status="confirmed", updated="2026-11-20T10:00:00.000000Z",
                date="2027-01-15", original_date=None, bound=True, declined=False, series=False)
    return Revision(**{**base, **kw})


def _cur(**kw) -> Current:
    from datetime import UTC, datetime
    base = dict(commitment_id="c1", status=STATUS_SCHEDULED, date="2027-01-15",
                latest=datetime(2026, 11, 19, tzinfo=UTC))
    return Current(**{**base, **kw})


@pytest.mark.parametrize("rev, cur, outcome, action", [
    (_rev(), None, "applied", "commit"),
    (_rev(), _cur(), "no_change", None),
    (_rev(date="2027-01-20"), _cur(), "applied", "confirm_change"),
    (_rev(status="cancelled", date=None), _cur(), "applied", "cancel"),
    (_rev(status="cancelled", date=None), None, "no_commitment", None),
    (_rev(status="cancelled", date=None), _cur(status=STATUS_CANCELLED, date=None), "no_change", None),
    (_rev(), _cur(status=STATUS_CANCELLED, date=None), "applied", "commit"),
    (_rev(bound=False), None, "no_commitment", None),
    (_rev(status="tentative"), None, "no_commitment", None),
    (_rev(bound=False, declined=True), _cur(), "applied", "cancel"),
    (_rev(series=True), None, "recurring_series", None),
    (_rev(updated="2026-11-18T10:00:00.000000Z", date="2027-02-01"), _cur(), "stale_revision", None),
    (_rev(updated=None), None, "no_input", None),
    (_rev(date=None), None, "no_input", None),
])
def test_decision_table(rev, cur, outcome, action) -> None:
    step = decide(rev, cur)
    assert (step.outcome, step.action) == (outcome, action), step.reason


def test_a_reschedule_supersedes_the_scheduled_date_and_a_cancel_names_it() -> None:
    move = decide(_rev(date="2027-01-20"), _cur())
    assert (move.confirmed_date, move.prior_confirmed_date) == ("2027-01-20", "2027-01-15")
    cancel = decide(_rev(status="cancelled", date=None), _cur())
    assert (cancel.confirmed_date, cancel.prior_confirmed_date) == (None, "2027-01-15")


# ── Adapter → SQLite → ledger → Hindsight ─────────────────────────────────────

class TestCalendarPipeline:
    def test_a_scheduled_meeting_reaches_the_ledger_and_hindsight(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            cal.put(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z",
                          description="Bring the floor plans."))
            cal.run()
            cid = cal.commitment("e1")
            st = cal.state(cid)
            assert (st["status"], st["scheduled_date"], st["event_type"]) == (
                STATUS_SCHEDULED, "2027-01-15", "meeting")
            assert st["source_doc_id"] == "gcal:primary:e1@2026-11-20T10:00:00.000000Z"
            assert cal.outcomes() == [("calendar.event.changed", "applied")]
            # The full event document is the immutable evidence (description included).
            [(blob,)] = cal.query("SELECT COUNT(*) FROM content_blobs")
            assert blob == 1
            docs = {d["document_id"]: d for d in cal.hindsight.retained["olo-biz"]}
            [sig_doc] = [d for d in docs.values() if d["document_id"].startswith("sig:")]
            assert sig_doc["metadata"]["commitment_id"] == cid
            assert f"ledger:{cid}:v1" in docs
            assert "date 2027-01-15" in docs[f"ledger:{cid}:v1"]["content"]

    def test_reschedule_attendee_edit_and_cancellation(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            cal.put(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z",
                          guests=((ALICE, "needsAction"),)))
            cal.run()
            cal.put(event("e1", "2027-01-20", updated="2026-11-21T10:00:00.000Z",
                          guests=((ALICE, "needsAction"),)))
            cal.run()
            # Alice accepts and the title changes: an attendee edit, not a commitment.
            cal.put(event("e1", "2027-01-20", updated="2026-11-22T10:00:00.000Z",
                          summary="Site visit (confirmed)"))
            cal.run()
            cal.put(cancelled("e1", updated="2026-11-23T10:00:00.000Z"))
            cal.run()
            cid = cal.commitment("e1")
            assert cal.versions(cid) == [
                ("commit", "2027-01-15", None),
                ("confirm_change", "2027-01-20", "2027-01-15"),
                ("cancel", None, "2027-01-20"),
            ]
            assert cal.state(cid)["status"] == STATUS_CANCELLED
            assert [o for _, o in cal.outcomes()] == ["applied", "applied", "no_change", "applied"]
            # Every revision stays as evidence; the cancellation did not erase any.
            [(n,)] = cal.query("SELECT COUNT(*) FROM signals WHERE kind LIKE 'calendar.event.%'")
            assert n == 4

    def test_a_second_customer_on_the_invite_never_moves_the_commitment(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            cal.put(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z"))
            cal.run()
            cal.put(event("e1", "2027-01-18", updated="2026-11-21T10:00:00.000Z",
                          guests=((ALICE, "accepted"), (BOB, "needsAction"))))
            cal.run()
            cid = cal.commitment("e1")
            assert cal.versions(cid) == [("commit", "2027-01-15", None)]
            [(status,)] = cal.query(
                "SELECT identity_status FROM signal_resolution sr JOIN signals s"
                " ON s.external_id=sr.signal_id WHERE s.occurred_at LIKE '2026-11-21%'")
            assert status == "ambiguous"          # waits for an operator, never a guess

    def test_unrelated_events_on_the_same_day_stay_distinct(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            cal.put(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z", summary="Visit"),
                    event("e2", "2027-01-15", updated="2026-11-20T11:00:00.000Z", summary="Review"))
            cal.run()
            a, b = cal.commitment("e1"), cal.commitment("e2")
            assert a != b
            cal.put(cancelled("e2", updated="2026-11-22T10:00:00.000Z"))
            cal.run()
            assert cal.state(a)["status"] == STATUS_SCHEDULED
            assert cal.state(b)["status"] == STATUS_CANCELLED

    def test_recurring_series_and_its_exceptions(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            cal.put(event("s1", "2027-01-08", updated="2026-11-20T10:00:00.000Z",
                          recurrence=["RRULE:FREQ=WEEKLY;COUNT=6"]))
            cal.run()
            assert cal.query("SELECT COUNT(*) FROM commitments") == [(0,)]
            # One occurrence moves a day; another is cancelled.
            moved = event("s1_20270115T200000Z", "2027-01-16", updated="2026-11-21T10:00:00.000Z",
                          recurringEventId="s1",
                          originalStartTime={"dateTime": "2027-01-15T15:00:00-05:00"})
            gone = cancelled("s1_20270122T200000Z", updated="2026-11-21T11:00:00.000Z",
                             recurringEventId="s1",
                             originalStartTime={"dateTime": "2027-01-22T15:00:00-05:00"})
            cal.put(moved, gone)
            cal.run()
            cid = cal.commitment("s1_20270115T200000Z")
            assert cal.versions(cid) == [("commit", "2027-01-16", "2027-01-15")]
            assert cal.query("SELECT COUNT(*) FROM commitments") == [(1,)]
            assert [o for _, o in cal.outcomes()] == [
                "recurring_series", "applied", "no_commitment"]

    def test_older_revision_delivered_late_never_rolls_back(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            cal.put(event("e1", "2027-01-20", updated="2026-11-21T10:00:00.000Z"))
            cal.run()
            cal.put(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z"))
            cal.run()
            cid = cal.commitment("e1")
            assert cal.versions(cid) == [("commit", "2027-01-20", None)]
            assert [o for _, o in cal.outcomes()] == ["stale_revision", "applied"]

    def test_expired_sync_token_backfills_and_duplicates_are_harmless(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            cal.put(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z"))
            cal.run()
            cal.put(event("e1", "2027-01-19", updated="2026-11-21T10:00:00.000Z"))
            cal.fake.calendar.expire_next_sync = True
            cal.run()                           # 410 → full resync delivers the change
            cid = cal.commitment("e1")
            assert cal.versions(cid)[-1] == ("confirm_change", "2027-01-19", "2027-01-15")
            cal.run()                           # the queued revision arrives again
            cal.run()
            assert len(cal.versions(cid)) == 2
            [(n,)] = cal.query("SELECT COUNT(*) FROM signals WHERE kind='calendar.event.changed'")
            assert n == 2

    def test_a_changed_customer_is_unresolved_not_a_second_commitment(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            cal.put(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z"))
            cal.run()
            cal.put(event("e1", "2027-01-15", updated="2026-11-21T10:00:00.000Z",
                          guests=((CAROL, "accepted"),)))
            cal.run()
            assert cal.query("SELECT COUNT(*) FROM commitments") == [(1,)]
            [(reason,)] = cal.query("SELECT reason FROM unresolved_matches WHERE resolved_at IS NULL")
            assert "customer changed" in reason
            # The next revision for the new customer waits behind that decision.
            cal.put(event("e1", "2027-01-17", updated="2026-11-22T10:00:00.000Z",
                          guests=((CAROL, "accepted"),)))
            cal.run()
            assert cal.query("SELECT COUNT(*) FROM commitments") == [(1,)]
            assert [o for _, o in cal.outcomes()] == [
                "applied", "ledger_unresolved", "ledger_unresolved"]

    def test_an_invitation_binds_only_once_accepted(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            cal.put(event("i1", "2027-01-15", updated="2026-11-20T10:00:00.000Z",
                          organiser_self=False, my_response="needsAction"))
            cal.run()
            assert cal.query("SELECT COUNT(*) FROM commitments") == [(0,)]
            cal.put(event("i1", "2027-01-15", updated="2026-11-21T10:00:00.000Z",
                          organiser_self=False, my_response="accepted"))
            cal.run()
            assert cal.versions(cal.commitment("i1")) == [("commit", "2027-01-15", None)]
            cal.put(event("i1", "2027-01-15", updated="2026-11-22T10:00:00.000Z",
                          organiser_self=False, my_response="declined"))
            cal.run()
            assert cal.state(cal.commitment("i1"))["status"] == STATUS_CANCELLED

    def test_calendar_signals_from_before_batch_3_are_processed_once(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            cal.put(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z"))
            run_source(cal.adapter, db_path=cal.db)          # ingest only
            conn = connect(cal.db)
            conn.execute("UPDATE signal_resolution SET commitment_status='skipped'")
            conn.commit()
            conn.close()
            cal.run()
            assert cal.versions(cal.commitment("e1")) == [("commit", "2027-01-15", None)]


def test_calendar_commitments_are_applied_after_the_model_budget_runs_out(tmp_path) -> None:
    """An exhausted extraction budget stops prose only: calendar events need
    no model and are still applied in the same run."""
    from datetime import UTC, datetime

    from olo.extractor import Budget
    from tests.olo.test_calendar_email_linking import Business, PLAIN
    b = Business(tmp_path)
    b.calendar(event("e1", "2027-01-20", updated="2026-11-20T10:00:00.000Z"))
    before = b.rows("SELECT COUNT(*) FROM commitment_versions")[0][0]
    # Prose first in the queue, then a calendar revision, with no model budget.
    from olo.adapters.gcal import _event_to_signal
    from olo.adapters.gmail_full import build_email_content_signal
    from tests.olo import gmail_fixtures as gf
    payload = gf.with_headers(gf.text_part(PLAIN), gf.message_headers(
        frm=f"Owner <{OWNER}>", to=f"Alice <{ALICE}>", subject="Visit", msg_id="m1"))
    email = build_email_content_signal("gcal_biz", gf.raw_message(
        "m1", payload, labels=["SENT"], internal_ms="1700000000000"), "1")
    revision = _event_to_signal(event("e1", "2027-01-22", updated="2026-11-21T10:00:00.000Z"),
                                "gcal_biz", datetime.now(UTC), "primary")
    b.gcal.queue.extend([email, revision])
    r = run_source(b.gcal, db_path=b.db, commitment=CommitmentStageConfig(
        model_fn=b.model, budget=Budget(max_calls=0)))
    assert r.commitment.get("budget_exhausted") == 1 and r.commitment.get("applied") == 1
    assert b.rows("SELECT COUNT(*) FROM commitment_versions")[0][0] == before + 1
    assert b.model.calls == []



# ── Recurring occurrences share the series' iCalUID (live Google, 2026-11-27) ─

SERIES_UID = "s1@google.com"


def _occurrence(day: str, original: str, *, updated: str, hour: int = 15, **extra) -> dict:
    """A moved or cancelled occurrence as live Google returns it: its own id,
    the series id and original slot, and the *series'* iCalUID."""
    eid = f"s1_{original.replace('-', '')}T200000Z"
    return event(eid, day, updated=updated, recurringEventId="s1", iCalUID=SERIES_UID,
                 originalStartTime={"dateTime": f"{original}T15:00:00-05:00"},
                 start={"dateTime": f"{day}T{hour:02d}:00:00-05:00"},
                 end={"dateTime": f"{day}T{hour + 1:02d}:00:00-05:00"}, **extra)


def _series(cal) -> None:
    cal.put(event("s1", "2026-12-04", updated="2026-11-20T10:00:00.000Z", iCalUID=SERIES_UID,
                  recurrence=["RRULE:FREQ=WEEKLY;COUNT=6"]))
    cal.run()


class TestRecurringOccurrenceIdentity:
    def test_cancelling_one_occurrence_never_cancels_another(self, tmp_path) -> None:
        """Live C5: the 18 Dec occurrence's cancellation was recorded on the
        11 Dec occurrence's commitment through the shared iCalUID."""
        with calendar(tmp_path) as cal:
            _series(cal)
            cal.put(_occurrence("2026-12-12", "2026-12-11", updated="2026-11-21T10:00:00.000Z"))
            cal.run()
            moved = cal.commitment("s1_20261211T200000Z")
            # Live shape: the cancelled occurrence is a full resource (title, start, UID).
            cal.put(_occurrence("2026-12-18", "2026-12-18", updated="2026-11-21T11:00:00.000Z",
                                status="cancelled"))
            cal.run()
            assert cal.state(moved)["status"] == STATUS_SCHEDULED
            assert cal.versions(moved) == [("commit", "2026-12-12", "2026-12-11")]
            assert [o for _, o in cal.outcomes()] == ["recurring_series", "applied", "no_commitment"]
            assert cal.query("SELECT COUNT(*) FROM commitment_aliases WHERE alias LIKE 'ical:%'") == [(0,)]

    def test_a_minimal_cancellation_stub_finds_only_its_own_occurrence(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            _series(cal)
            cal.put(_occurrence("2026-12-12", "2026-12-11", updated="2026-11-21T10:00:00.000Z"),
                    _occurrence("2026-12-19", "2026-12-18", updated="2026-11-21T10:01:00.000Z"))
            cal.run()
            a, b = cal.commitment("s1_20261211T200000Z"), cal.commitment("s1_20261218T200000Z")
            # A stub: id, status and updated only — no title, series or UID.
            cal.put(cancelled("s1_20261218T200000Z", updated="2026-11-22T10:00:00.000Z"))
            cal.run()
            assert cal.state(b)["status"] == STATUS_CANCELLED
            assert cal.state(a)["status"] == STATUS_SCHEDULED

    def test_two_occurrences_on_one_day_stay_two_commitments(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            _series(cal)
            morning = _occurrence("2026-12-11", "2026-12-11", updated="2026-11-21T10:00:00.000Z",
                                  hour=9)
            evening = dict(_occurrence("2026-12-11", "2026-12-11", updated="2026-11-21T10:01:00.000Z",
                                       hour=17), id="s1_20261211T220000Z",
                           originalStartTime={"dateTime": "2026-12-11T17:00:00-05:00"})
            cal.put(morning, evening)
            cal.run()
            m, e = cal.commitment("s1_20261211T200000Z"), cal.commitment("s1_20261211T220000Z")
            assert m != e
            cal.put(dict(evening, status="cancelled", updated="2026-11-22T10:00:00.000Z"))
            cal.run()
            assert cal.state(e)["status"] == STATUS_CANCELLED
            assert cal.state(m)["status"] == STATUS_SCHEDULED

    def test_rescheduling_an_occurrence_twice_updates_only_it(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            _series(cal)
            cal.put(_occurrence("2026-12-12", "2026-12-11", updated="2026-11-21T10:00:00.000Z"),
                    _occurrence("2026-12-18", "2026-12-18", updated="2026-11-21T10:01:00.000Z",
                                hour=16))
            cal.run()
            other = cal.commitment("s1_20261218T200000Z")
            cal.put(_occurrence("2026-12-14", "2026-12-11", updated="2026-11-22T10:00:00.000Z"))
            cal.run()
            moved = cal.commitment("s1_20261211T200000Z")
            assert cal.versions(moved) == [("commit", "2026-12-12", "2026-12-11"),
                                           ("confirm_change", "2026-12-14", "2026-12-12")]
            assert cal.versions(other) == [("commit", "2026-12-18", None)]
            # The series association is kept apart from the occurrence's identity.
            series = cal.query("SELECT alias, commitment_id, basis FROM commitment_aliases"
                               " WHERE alias LIKE 'series:%' ORDER BY alias")
            assert series == [
                ("series:gcal:primary:s1@2026-12-11T15:00:00-05:00", moved, "series_occurrence"),
                ("series:gcal:primary:s1@2026-12-18T15:00:00-05:00", other, "series_occurrence")]

    def test_a_single_event_still_links_by_its_ical_uid(self, tmp_path) -> None:
        with calendar(tmp_path) as cal:
            cal.put(event("e1", "2026-12-10", updated="2026-11-20T10:00:00.000Z"))
            cal.run()
            assert cal.query("SELECT alias, commitment_id FROM commitment_aliases WHERE alias LIKE 'ical:%'"
                             ) == [("ical:e1@google.com", cal.commitment("e1"))]
