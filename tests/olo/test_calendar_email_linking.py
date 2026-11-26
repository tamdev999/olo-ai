"""Email and Calendar describing the same meeting (post-audit batch 4).

A Gmail invitation carries the calendar event's iCalendar UID; that UID —
never a similar date — is what links an email to a calendar event.  Each
test runs the real worker (identity, extraction with a mock model, the
deterministic calendar stage, the ledger) over Gmail API-shaped messages
and Calendar API-shaped events, in both arrival orders.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from olo.adapters import FetchBatch
from olo.adapters.gcal import _event_to_signal
from olo.adapters.gmail_full import build_email_content_signal
from olo.commitments import CommitmentStageConfig
from olo.ledger import STATUS_CANCELLED, STATUS_SCHEDULED, get_all_versions, get_current_state
from olo.operator import authorize, enroll, resolve_commitment
from olo.runner import run_source
from olo.store import connect
from tests.olo import gmail_fixtures as gf
from tests.olo.test_calendar_commitments import ALICE, OWNER, cancelled, event
from tests.olo.test_s5_pipeline import Model, answer

UID = "evt-site-visit@google.com"
INVITE = "Invitation: Site visit on 2027-01-15 at 15:00 at your office."
PLAIN = "Confirming our site visit on 2027-01-15 at your office."
ROUTES = {
    INVITE: answer("commit", "Site visit on 2027-01-15", event_type="meeting",
                   confirmed="2027-01-15"),
    PLAIN: answer("commit", "site visit on 2027-01-15", event_type="meeting",
                  confirmed="2027-01-15"),
}
NOW = "2026-11-26T12:00:00.000000Z"


class Queue:
    def __init__(self, source: str) -> None:
        self.source, self.queue, self.n = source, [], 0

    def fetch(self, cursor, *, limit):
        batch, self.queue = self.queue, []
        self.n += 1
        return FetchBatch(signals=batch, has_more=False, next_cursor=f"c{self.n}")


class Business:
    def __init__(self, tmp_path: Path) -> None:
        self.db = tmp_path / "olo.db"
        self.model = Model(dict(ROUTES))
        self.gmail, self.gcal = Queue("gmail_biz"), Queue("gcal_biz")

    def email(self, msg_id: str, text: str, *, uid: str | None = None, day: int = 19,
              to: str = f"Alice <{ALICE}>") -> None:
        body = gf.invitation(text, uid) if uid else gf.text_part(text)
        payload = gf.with_headers(body, gf.message_headers(
            frm=f"Owner <{OWNER}>", to=to, subject="Site visit", msg_id=msg_id))
        ms = str(int(datetime(2026, 11, day, 9, tzinfo=UTC).timestamp() * 1000))
        raw = gf.raw_message(msg_id, payload, labels=["SENT"], thread=f"t-{msg_id}", internal_ms=ms)
        self.gmail.queue.append(build_email_content_signal("gmail_biz", raw, "1"))
        assert run_source(self.gmail, db_path=self.db,
                          commitment=CommitmentStageConfig(model_fn=self.model)).status == "succeeded"

    def calendar(self, *resources: dict) -> None:
        fetched = datetime(2026, 11, 26, tzinfo=UTC)
        self.gcal.queue.extend(_event_to_signal(r, "gcal_biz", fetched, "primary") for r in resources)
        assert run_source(self.gcal, db_path=self.db,
                          commitment=CommitmentStageConfig(model_fn=self.model)).status == "succeeded"

    def rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        conn = connect(self.db)
        try:
            return [tuple(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def only(self) -> str:
        [(cid,)] = self.rows("SELECT id FROM commitments")
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
            return [(v["source_kind"].split(".")[0], v["action"], v["confirmed_date"],
                     v["confirmed_start"]) for v in get_all_versions(conn, cid)]
        finally:
            conn.close()

    def outcome(self, kind_prefix: str) -> list[str]:
        return [r[0] for r in self.rows(
            "SELECT sr.commitment_outcome FROM signal_resolution sr JOIN signals s"
            " ON s.external_id=sr.signal_id AND s.source=sr.source WHERE s.kind LIKE ?"
            " ORDER BY s.occurred_at", (f"{kind_prefix}%",))]


def test_a_same_day_time_change_is_a_change_of_the_commitment(tmp_path) -> None:
    from olo.projection import desired_documents
    b = Business(tmp_path)
    b.calendar(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z"))
    b.calendar(event("e1", "2027-01-15", updated="2026-11-21T10:00:00.000Z",
                     start={"dateTime": "2027-01-15T17:00:00-05:00"},
                     end={"dateTime": "2027-01-15T18:00:00-05:00"}))
    cid = b.only()
    assert b.versions(cid) == [
        ("calendar", "commit", "2027-01-15", "2027-01-15T15:00:00-05:00"),
        ("calendar", "confirm_change", "2027-01-15", "2027-01-15T17:00:00-05:00")]
    assert b.state(cid)["scheduled_start"] == "2027-01-15T17:00:00-05:00"
    conn = connect(b.db)
    docs = {d["document_id"]: d for d in desired_documents(conn, "gcal_biz")}
    conn.close()
    current = docs[f"ledger:{cid}:v2"]
    assert "at 2027-01-15T17:00:00-05:00" in current["content"]
    assert "CURRENT" in current["content"] and "SUPERSEDED" in docs[f"ledger:{cid}:v1"]["content"]


def test_invitation_first_then_calendar_is_one_commitment(tmp_path) -> None:
    b = Business(tmp_path)
    b.email("m1", INVITE, uid=UID)
    cid = b.only()
    b.calendar(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z", iCalUID=UID))
    b.calendar(cancelled("e1", updated="2026-11-22T10:00:00.000Z"))   # the stub has no UID
    assert b.only() == cid
    assert b.versions(cid) == [
        ("email", "commit", "2027-01-15", None),
        ("calendar", "confirm_change", "2027-01-15", "2027-01-15T15:00:00-05:00"),
        ("calendar", "cancel", None, None)]
    assert b.state(cid)["status"] == STATUS_CANCELLED


def test_calendar_first_then_invitation_links_by_uid(tmp_path) -> None:
    b = Business(tmp_path)
    b.calendar(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z", iCalUID=UID))
    cid = b.only()
    b.email("m1", INVITE, uid=UID, day=21)
    assert b.only() == cid
    assert b.outcome("email") == ["applied"]
    [(reason,)] = b.rows("SELECT commitment_reason FROM signal_resolution"
                         " WHERE commitment_outcome='applied' AND signal_id LIKE 'c:%'")
    assert "linked by calendar invitation UID" in reason
    assert b.state(cid)["status"] == STATUS_SCHEDULED


def test_no_uid_on_the_same_day_is_unresolved_not_a_duplicate(tmp_path) -> None:
    b = Business(tmp_path)
    b.email("m1", PLAIN)
    email_cid = b.only()
    b.calendar(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z", iCalUID="other@google"))
    assert b.only() == email_cid                       # no second commitment
    assert b.outcome("calendar") == ["ledger_unresolved"]
    [(reason,)] = b.rows("SELECT reason FROM unresolved_matches WHERE resolved_at IS NULL")
    assert "no invitation UID" in reason
    # The next revision waits behind the undecided first one.
    b.calendar(event("e1", "2027-01-15", updated="2026-11-21T10:00:00.000Z", iCalUID="other@google",
                     start={"dateTime": "2027-01-15T17:00:00-05:00"}))
    assert b.outcome("calendar") == ["ledger_unresolved", "ledger_unresolved"]
    # An operator decides it is the same meeting; what waited follows.
    conn = connect(b.db)
    enroll(conn, "ops", "correct horse battery", now=NOW)
    g = authorize(conn, "ops", "correct horse battery", now=NOW)
    [(doc,)] = conn.execute("SELECT source_doc_id FROM unresolved_matches WHERE resolved_at IS NULL"
                            " AND blocked_by='[]'").fetchall()
    res = resolve_commitment(conn, g, doc, "link", target=email_cid, now=NOW,
                             reason="the calendar entry for the visit confirmed by email")
    conn.close()
    assert res.outcome == "applied" and len(res.replayed) == 1
    assert b.state(email_cid)["scheduled_start"] == "2027-01-15T17:00:00-05:00"
    b.calendar(cancelled("e1", updated="2026-11-23T10:00:00.000Z"))
    assert b.only() == email_cid and b.state(email_cid)["status"] == STATUS_CANCELLED


def test_a_late_invitation_never_revives_a_cancelled_meeting(tmp_path) -> None:
    b = Business(tmp_path)
    b.calendar(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z", iCalUID=UID))
    b.calendar(cancelled("e1", updated="2026-11-22T10:00:00.000Z"))
    b.email("m1", INVITE, uid=UID, day=19)            # older, delivered last
    cid = b.only()
    assert [v[1] for v in b.versions(cid)] == ["commit", "cancel", "commit"]
    assert b.state(cid)["status"] == STATUS_CANCELLED


def test_a_uid_naming_another_customers_meeting_is_not_linked(tmp_path) -> None:
    b = Business(tmp_path)
    b.calendar(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z", iCalUID=UID,
                     guests=(("bob@other.example", "accepted"),)))
    bobs = b.only()
    b.email("m1", INVITE, uid=UID)                     # to Alice
    assert b.rows("SELECT COUNT(*) FROM commitments") == [(1,)]
    assert b.outcome("email") == ["ledger_unresolved"]
    assert [v[1] for v in b.versions(bobs)] == ["commit"]


KEYED = "Meeting MTG-5 is confirmed for 2027-01-15 at your office."


def test_a_keyed_email_meeting_on_a_calendar_day_is_never_a_second_commitment(tmp_path) -> None:
    """A business key cannot tell an email meeting apart from a calendar
    event on the same day (the event has no key): unresolved, not doubled."""
    b = Business(tmp_path)
    b.model.routes[KEYED] = answer("commit", "Meeting MTG-5 is confirmed for 2027-01-15",
                                   event_type="meeting", key="MTG-5", confirmed="2027-01-15")
    b.calendar(event("e1", "2027-01-15", updated="2026-11-20T10:00:00.000Z"))
    b.email("m1", KEYED)
    assert b.rows("SELECT COUNT(*) FROM commitments") == [(1,)]
    assert b.outcome("email") == ["ledger_unresolved"]
    # A keyed meeting on another day is its own commitment.
    other = "Meeting MTG-6 is confirmed for 2027-01-16 at your office."
    b.model.routes[other] = answer("commit", "Meeting MTG-6 is confirmed for 2027-01-16",
                                   event_type="meeting", key="MTG-6", confirmed="2027-01-16")
    b.email("m2", other, day=20)
    assert b.rows("SELECT COUNT(*) FROM commitments") == [(2,)]
