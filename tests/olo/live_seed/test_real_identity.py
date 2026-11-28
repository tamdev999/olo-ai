"""Genuine second-identity acceptance (google_acceptance.real_identity).

A scenario recorded ``--by real-identity`` passes only on data that shows a
genuine second party: seeded data recorded as real fails, whatever the
operator claimed.  The genuine data here is built the way the real clients
build it (Gmail web Message-IDs, a customer-organized Calendar invitation);
the seeded data is the seeder's own.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest

from olo.adapters.gcal import _event_to_signal
from olo.runner import run_source
from olo.store import connect
from tests.olo.fake_google import FakeGoogleServer
from tests.olo.live_seed import acceptance_fixtures as af
from tests.olo.live_seed.fake_write import FakeWriteApi
from tests.olo.test_calendar_commitments import event
from tests.olo.test_calendar_email_linking import Queue
from tests.olo.test_google_cron_e2e import _save_creds

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "olo" / "m3"))
from google_acceptance import guard, real_identity, run, scenarios

ACCOUNT = "biz@acceptance.test"
REAL = "second.identity@customer-mail.org"         # a genuine (non-reserved) second party
RUN = "acc20261128t120000beef"
T0 = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


def _mime(frm: str, to: str, subject: str, text: str, msgid: str, in_reply_to: str = "") -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Message-ID"] = frm, to, subject, msgid
    msg["Date"] = format_datetime(T0)
    if in_reply_to:
        msg["In-Reply-To"] = msg["References"] = in_reply_to
    msg.set_content(text)
    return bytes(msg)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    def make(customer: str):
        h, evidence = tmp_path / "home", tmp_path / "evidence"
        h.mkdir()
        google = FakeGoogleServer(email=ACCOUNT).__enter__()
        _save_creds(google, "accept", h)
        env = {"OLO_ACCEPTANCE_HOME": str(h), "OLO_ACCEPTANCE_ACCOUNT": ACCOUNT,
               "OLO_ACCEPTANCE_CONFIRM": ACCOUNT, "OLO_ACCEPTANCE_CUSTOMER": customer,
               "OLO_ACCEPTANCE_EVIDENCE": str(evidence), "OLO_ACCEPTANCE_RUN": RUN}
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("HERMES_HOME", str(h))
        pf = guard.run_preflight(env)
        assert pf.ok, pf.refusals
        run.sync(pf, max_requests=200, limit=5, run=RUN)          # bootstrap
        made.append(google)
        return pf, FakeWriteApi(google, ACCOUNT), evidence
    made: list = []
    yield make
    for g in made:
        g.__exit__(None, None, None)


def _outcome(pf, evidence, sid: str) -> dict:
    run.check(pf, {sid}, "A", run=RUN)
    report = json.loads(run.results_path(evidence, RUN).read_text(encoding="utf-8"))
    return {o["id"]: o for o in report["outcomes"]}[sid]


def test_a_genuine_gmail_conversation_passes_g1_for_real(home) -> None:
    pf, api, evidence = home(REAL)
    subject = f"[OLO-G1] order question - acc {RUN}"
    first = "<CAbiz0001@mail.gmail.com>"
    api.insert_message(_mime(ACCOUNT, REAL, subject, "Your order 4471 ships on Friday.", first), ["SENT"], None)
    run.sync(pf, max_requests=200, limit=5, run=RUN)
    reply = "<CAcust0001@mail.gmail.com>"
    api.import_message(_mime(f"Second Identity <{REAL}>", ACCOUNT, "Re: " + subject,
                             "Could you confirm it ships on Friday?\n\n"
                             f"On Sat, 28 Nov 2026 at 12:00, <{ACCOUNT}> wrote:\n"
                             "> Your order 4471 ships on Friday.\n", reply, first), ["INBOX"], None)
    run.sync(pf, max_requests=200, limit=5, run=RUN)
    api.insert_message(_mime(ACCOUNT, REAL, "Re: " + subject,
                             "Yes, it ships on Friday.\n\n"
                             f"On Sat, 28 Nov 2026 at 12:05, Second Identity <{REAL}> wrote:\n"
                             "> Could you confirm it ships on Friday?\n",
                             "<CAbiz0002@mail.gmail.com>", reply), ["SENT"], None)
    run.sync(pf, max_requests=200, limit=5, run=RUN)
    run.record_action(pf, "G1", "second Google account, Gmail web", by="real-identity", run=RUN)
    out = _outcome(pf, evidence, "G1")
    assert out["status"] == "passed", out
    assert out["details"]["provenance"]["genuine"] and out["details"]["customer_replies"] == 1
    assert out["details"]["quoted_in_authored"] == []


def test_seeded_data_recorded_as_real_fails_provenance(home) -> None:
    pf, api, evidence = home(REAL)
    # The seeder refuses a non-synthetic customer, so it seeds as itself; the
    # operator then (wrongly) records the action as a real identity's.
    s = af.Session(api, RUN, ACCOUNT, "customer@olo-seed.example.com",
                   af.new_run({"runs": {}}, RUN, ACCOUNT, "customer@olo-seed.example.com", T0), evidence)
    af.seed_phase(s, "G1", 1)
    run.sync(pf, max_requests=200, limit=5, run=RUN)
    run.record_action(pf, "G1", "claimed real", by="real-identity", run=RUN)
    out = _outcome(pf, evidence, "G1")
    assert out["status"] == "failed"
    prov = out["details"]["provenance"]
    assert not prov["genuine"] and prov["received"] and all(r["seeded"] for r in prov["received"])


def test_g2_needs_a_real_outlook_identity(home, monkeypatch) -> None:
    pf, api, evidence = home(REAL)
    subject = f"[OLO-G2] quote - acc {RUN}"
    api.import_message(_mime(f"Second Identity <{REAL}>", ACCOUNT, subject, "Thanks!",
                             "<CAcust0002@mail.gmail.com>"), ["INBOX"], None)
    run.sync(pf, max_requests=200, limit=5, run=RUN)
    run.record_action(pf, "G2", "a Gmail account is not Outlook", by="real-identity", run=RUN)
    out = _outcome(pf, evidence, "G2")
    assert out["status"] == "failed" and not out["details"]["provenance"]["outlook_domain"]


def test_the_outlook_identity_must_not_be_the_business() -> None:
    env = {"OLO_ACCEPTANCE_HOME": "/nonexistent", "OLO_ACCEPTANCE_ACCOUNT": ACCOUNT,
           "OLO_ACCEPTANCE_CONFIRM": ACCOUNT, "OLO_ACCEPTANCE_CUSTOMER": REAL,
           "OLO_ACCEPTANCE_EVIDENCE": "/tmp/e", "OLO_ACCEPTANCE_OUTLOOK": ACCOUNT}
    assert any("second account" in r for r in guard.settings_from(env).refusals)


def _calendar_ctx(tmp_path: Path, resource: dict, customer: str) -> scenarios.Ctx:
    db = tmp_path / "olo.db"
    q = Queue("gcal_biz")
    q.queue.append(_event_to_signal(resource, "gcal_biz", datetime(2026, 11, 28, tzinfo=UTC), "primary"))
    assert run_source(q, db_path=db).status == "succeeded"
    return scenarios.Ctx(connect(db), "owner@biz.example", customer, {}, run_id=RUN)


def test_an_invitation_counts_only_when_the_customer_organized_it(tmp_path) -> None:
    title = f"[OLO-C4] site visit - acc {RUN}"
    theirs = event("cust0001", "2026-12-03", updated="2026-11-28T12:00:00.000Z", summary=title,
                   organiser_self=False, guests=((REAL, "accepted"),), my_response="needsAction")
    ok, details = real_identity.c4(_calendar_ctx(tmp_path / "a", theirs, REAL))
    assert ok, details
    ours = event(af.event_id(RUN, "C4.e"), "2026-12-03", updated="2026-11-28T12:00:00.000Z",
                 summary=title, guests=((REAL, "needsAction"),))
    ok, details = real_identity.c4(_calendar_ctx(tmp_path / "b", ours, REAL))
    assert not ok and details["events"][0]["seeded"] and details["events"][0]["organizer_self"]


def test_a_synthetic_customer_is_never_a_real_identity(tmp_path) -> None:
    title = f"[OLO-C4] site visit - acc {RUN}"
    fake = "customer@olo-seed.example.com"
    theirs = event("cust0002", "2026-12-03", updated="2026-11-28T12:00:00.000Z", summary=title,
                   organiser_self=False, guests=((fake, "accepted"),))
    ok, details = real_identity.c4(_calendar_ctx(tmp_path, theirs, fake))
    assert not ok and details["customer_synthetic"]


def test_every_outcome_class_stays_distinct(tmp_path) -> None:
    """not_run, needs_identity, needs_approval, passed_simulated, passed and
    failed never collapse into one another (verdict and exit code follow)."""
    from olo.store import connect, migrate
    conn = connect(tmp_path / "olo.db")
    migrate(conn)
    ctx = scenarios.Ctx(conn, ACCOUNT, REAL, {}, run_id=RUN)
    out = {o.id: o.status for o in scenarios.evaluate(ctx)}
    assert {out[s] for s in ("C4", "L1", "L3", "G2")} == {"needs_identity"}
    assert out["B1"] == out["L2"] == "needs_approval"                 # mode B, not approved
    assert out["G5"] == out["E2"] == "not_run"
    assert scenarios.verdict(scenarios.evaluate(ctx)) == ("incomplete", 2)
    ctx.actions = {"G2": {"at": "2026-11-28T12:00:00Z", "by": "real-identity"}}
    [g2] = scenarios.evaluate(ctx, {"G2"})
    assert g2.status == "failed" and g2.details["provenance"]["genuine"] is False   # claimed, not shown
    ctx.actions = {"G2": {"at": "2026-11-28T12:00:00Z", "by": "seeder"}}
    assert scenarios.evaluate(ctx, {"G2"})[0].status == "needs_identity"
    conn.close()


def test_the_operator_plan_names_every_subject_with_the_run_tag() -> None:
    text = real_identity.plan(RUN)
    for sid in ("G1", "G2", "G4", "C4", "L1"):
        assert f"[OLO-{sid}]" in text
    assert text.count(f"acc {RUN}") == 5 and "--by real-identity" in text   # G1 G4 G2 C4 L1
    assert run.main(["--run-id", RUN, "real-plan"]) == 0
