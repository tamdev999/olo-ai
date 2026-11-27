"""The seeder's acceptance fixtures, proven offline (no Google call).

The generator writes through ``FakeWriteApi`` into the fake Google server;
Olo's real adapters sync it through the acceptance harness, and the harness
checks it.  A pass here means the fixture produces what the scenario's check
expects; the live run then verifies the same path against Google.
"""
from __future__ import annotations

import email
import json
import re
import sys
from datetime import UTC, datetime
from email.policy import default
from pathlib import Path

import pytest

from tests.olo.fake_google import FakeGoogleServer
from tests.olo.live_seed import acceptance_fixtures as af
from tests.olo.live_seed.fake_write import FakeWriteApi
from tests.olo.test_google_cron_e2e import _save_creds

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "olo" / "m3"))
from google_acceptance import guard, run, scenarios

ACCOUNT, CUSTOMER = "biz@acceptance.test", "customer@acceptance.test"
RUN = "acc20261127t120000beef"
T0 = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def _session(api, manifest=None, evidence=None) -> af.Session:
    manifest = manifest if manifest is not None else {"runs": {}}
    return af.Session(api, RUN, ACCOUNT, CUSTOMER, af.new_run(manifest, RUN, ACCOUNT, CUSTOMER, T0), evidence)


class TestIdentityAndGuards:
    def test_run_ids_are_unique_and_validated(self) -> None:
        a, b = af.new_run_id(T0), af.new_run_id(T0)
        assert a != b and af.check_run_id(a) == a
        with pytest.raises(af.AcceptanceSeedError):
            af.check_run_id("acc; rm -rf")

    def test_ids_are_deterministic_and_valid_for_google(self) -> None:
        assert af.message_id(RUN, "G1.q") == af.message_id(RUN, "G1.q") != af.message_id(RUN, "G1.r")
        eid = af.event_id(RUN, "C1.e")
        assert eid == af.event_id(RUN, "C1.e") and re.fullmatch(r"[a-v0-9]{5,1024}", eid)

    @pytest.mark.parametrize("address", ["someone@gmail.com", "alice@outlook.com", "nobody"])
    def test_the_seeded_customer_is_never_a_real_address(self, address) -> None:
        with pytest.raises(af.AcceptanceSeedError, match="synthetic"):
            af.check_synthetic(address)
        assert af.check_synthetic(f"Customer@{af.SEED_DOMAIN}") == f"customer@{af.SEED_DOMAIN}"

    def test_a_run_cannot_switch_account_or_customer(self) -> None:
        manifest = {"runs": {}}
        af.new_run(manifest, RUN, ACCOUNT, CUSTOMER, T0)
        with pytest.raises(af.AcceptanceSeedError, match="another account"):
            af.new_run(manifest, RUN, "other@acceptance.test", CUSTOMER)
        with pytest.raises(af.AcceptanceSeedError, match="different customer"):
            af.new_run(manifest, RUN, ACCOUNT, "x@acceptance.test")


class TestMime:
    def test_latin1_is_really_latin1(self) -> None:
        raw = af.build_mime(RUN, "G3.l", af.Mail(frm=CUSTOMER, to=ACCOUNT, subject="s", text="Café déjà vu",
                                                charset="iso-8859-1"))
        msg = email.message_from_bytes(raw, policy=default)
        assert msg.get_content_charset() == "iso-8859-1" and "Café déjà vu" in msg.get_content()
        assert "Café".encode() not in raw                      # not UTF-8 on the wire
        assert msg[af.RUN_HEADER] == RUN and msg["Message-ID"] == af.message_id(RUN, "G3.l")

    def test_attachments_and_forwarded_messages_are_real_parts(self) -> None:
        inner = af.build_mime(RUN, "in", af.Mail(frm=CUSTOMER, to=ACCOUNT, subject="inner", text="x"))
        raw = af.build_mime(RUN, "G4", af.Mail(frm=CUSTOMER, to=ACCOUNT, subject="s", text="body",
                                              attachments=(("notes.txt", "OLO-ATTACHMENT-TEXT"),),
                                              forwarded=inner))
        types = [p.get_content_type() for p in email.message_from_bytes(raw, policy=default).walk()]
        assert types[0] == "multipart/mixed" and "message/rfc822" in types


class TestSeeding:
    def test_phases_apply_once_and_in_order(self) -> None:
        with FakeGoogleServer(email=ACCOUNT) as google:
            s = _session(FakeWriteApi(google, ACCOUNT))
            with pytest.raises(af.AcceptanceSeedError, match="phase"):
                af.seed_phase(s, "G5", 2)
            assert af.seed_phase(s, "G5", 1) == "done"
            assert af.seed_phase(s, "G5", 1) == "already done"
            assert af.seed_phase(s, "G5", 2) == "done"
            assert [m.label_ids for m in google.gmail.messages.values()] == [["INBOX"]]

    def test_reseeding_creates_nothing_twice(self) -> None:
        with FakeGoogleServer(email=ACCOUNT) as google:
            s = _session(FakeWriteApi(google, ACCOUNT))
            af.seed_phase(s, "G9", 1)
            af.seed_phase(s, "G9", 1, force=True)
            assert len(google.gmail.messages) == 12
            af.seed_phase(s, "C8", 1)
            af.seed_phase(s, "C8", 1, force=True)
            assert len(google.calendar.events) == 12

    def test_the_write_budget_stops_seeding(self) -> None:
        with FakeGoogleServer(email=ACCOUNT) as google:
            api = af.Budgeted(FakeWriteApi(google, ACCOUNT), max_writes=7)
            s = _session(api)
            with pytest.raises(af.BudgetExceeded):
                af.seed_phase(s, "G9", 1)
            received = [m for m in google.gmail.messages.values() if "DRAFT" not in m.label_ids]
            assert api.writes == 7 and len(received) == 2                  # nothing past the budget
            assert 1 not in s.run["scenarios"]["G9"]["phases_done"]

    def test_scenarios_without_a_fixture_are_refused(self) -> None:
        with FakeGoogleServer(email=ACCOUNT) as google:
            s = _session(FakeWriteApi(google, ACCOUNT))
            for sid in ("C4", "L1", "G2", "E1"):
                with pytest.raises(af.AcceptanceSeedError, match="no seeder fixture"):
                    af.seed_phase(s, sid, 1)

    def test_cleanup_touches_only_this_runs_marked_items(self, tmp_path) -> None:
        with FakeGoogleServer(email=ACCOUNT) as google:
            api = FakeWriteApi(google, ACCOUNT)
            s = _session(api, evidence=tmp_path)
            for sid in ("G9", "C1", "C5"):
                af.seed_phase(s, sid, 1)
            af.seed_phase(s, "C5", 2)
            foreign = af.build_mime("acc20261101t000000aaaa", "x",
                                    af.Mail(frm=CUSTOMER, to=ACCOUNT, subject="not ours", text="x"))
            other = api.import_message(foreign, ["INBOX"], None)["id"]
            s.run["scenarios"]["G9"]["items"]["foreign"] = {"type": "message", "id": other}
            assert {r["result"] for r in af.cleanup(api, RUN, s.run, dry_run=True)} >= {"would be trashed"}
            assert all("TRASH" not in m.label_ids for m in google.gmail.messages.values())
            report = af.cleanup(api, RUN, s.run)
            results = {r["key"]: r["result"] for r in report}
            assert results["foreign"] == "skipped: run marker differs"
            assert "TRASH" not in google.gmail.messages[other].label_ids
            assert sum("TRASH" in m.label_ids for m in google.gmail.messages.values()) == 12
            assert results["moved"] == "removed with its series"
            assert all(e.status == "cancelled" for e in google.calendar.events.values()
                       if not e.id.count("_"))
            assert (tmp_path / "fixtures.jsonl").exists()

    def test_every_fixture_matches_a_harness_scenario_that_can_use_it(self) -> None:
        by_id = {sc.id: sc for sc in scenarios.SCENARIOS}
        for sid in af.PHASES:
            assert sid in by_id and by_id[sid].evidence != "second_identity", sid
        needs_data = [sc.id for sc in scenarios.SCENARIOS
                      if sc.identities != "local" and sc.evidence != "second_identity" and sc.id != "E6"]
        assert set(needs_data) <= set(af.PHASES), set(needs_data) - set(af.PHASES)


class TestGoogleWriteApiRequests:
    def _api(self, answers=None):
        calls = []

        def request(method, url, body, content_type):
            calls.append((method, url, body, content_type))
            ans = (answers or {}).get((method, url.split("?")[0]))
            if isinstance(ans, Exception):
                raise ans
            return ans if ans is not None else {"id": "x"}
        return af.GoogleWriteApi("token", request=request), calls

    def test_event_writes_never_send_invitations(self) -> None:
        api, calls = self._api()
        api.insert_event({"id": "oloacc1"})
        api.patch_event("oloacc1", {"summary": "s"})
        api.delete_event("oloacc1")
        assert all("sendUpdates=none" in url for _, url, _, _ in calls)

    def test_spam_imports_may_be_marked_spam_and_others_never(self) -> None:
        api, calls = self._api()
        api.import_message(b"raw", ["SPAM"], None)
        api.import_message(b"raw", ["INBOX"], "t1")
        assert "neverMarkSpam=false" in calls[0][1] and "neverMarkSpam=true" in calls[1][1]
        assert b'"threadId": "t1"' in calls[1][2] and calls[1][3].startswith("multipart/related")

    def test_conflicts_and_missing_items_are_idempotent(self) -> None:
        events = af.GoogleWriteApi.events
        api, calls = self._api({("POST", events): af.ApiError(409, "exists"),
                                ("GET", f"{events}/oloacc1"): {"id": "oloacc1", "status": "confirmed"},
                                ("DELETE", f"{events}/gone"): af.ApiError(410, "gone")})
        assert api.insert_event({"id": "oloacc1"})["id"] == "oloacc1"
        api.delete_event("gone")
        assert [c[0] for c in calls] == ["POST", "GET", "DELETE"]

    def test_lookup_is_by_message_id_including_spam_and_trash(self) -> None:
        api, calls = self._api({("GET", f"{af.GoogleWriteApi.gmail}/messages"): {"messages": []}})
        assert api.find_message("<a.b@olo-seed.example.com>") is None
        assert "rfc822msgid%3Aa.b%40olo-seed.example.com" in calls[0][1]
        assert "includeSpamTrash=true" in calls[0][1]


# ── End to end, offline: seeder → Olo sync → harness check ──────────────────

@pytest.fixture()
def acceptance(tmp_path, monkeypatch):
    home, evidence = tmp_path / "home", tmp_path / "evidence"
    home.mkdir()
    with FakeGoogleServer(email=ACCOUNT) as google:
        _save_creds(google, "accept", home)
        env = {"OLO_ACCEPTANCE_HOME": str(home), "OLO_ACCEPTANCE_ACCOUNT": ACCOUNT,
               "OLO_ACCEPTANCE_CONFIRM": ACCOUNT, "OLO_ACCEPTANCE_CUSTOMER": CUSTOMER,
               "OLO_ACCEPTANCE_EVIDENCE": str(evidence)}
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("HERMES_HOME", str(home))
        pf = guard.run_preflight(env)
        assert pf.ok, pf.refusals
        run.sync(pf, max_requests=200, limit=5)                       # bootstrap
        s = _session(FakeWriteApi(google, ACCOUNT), evidence=evidence)
        yield pf, s, evidence


def _seed_all_phases(pf, s, sid: str) -> None:
    for phase in range(1, len(af.PHASES[sid]) + 1):
        af.seed_phase(s, sid, phase)
        run.sync(pf, max_requests=300, limit=5)
    run.record_action(pf, sid, f"seeded, run {s.run_id}", by="seeder")


def _results(evidence: Path) -> dict:
    return {o["id"]: o for o in json.loads((evidence / "results.json").read_text(encoding="utf-8"))["outcomes"]}


@pytest.mark.parametrize("sid", ["G5", "G6", "G7", "G8", "G9", "C1", "C2", "C3", "C5", "C6", "C8"])
def test_seeded_gmail_scenarios_pass_through_olo(acceptance, sid) -> None:
    pf, s, evidence = acceptance
    _seed_all_phases(pf, s, sid)
    run.check(pf, {sid}, "A")
    outcome = _results(evidence)[sid]
    assert outcome["status"] == "passed", outcome
    assert outcome["details"]["evidence"] == "api" and outcome["details"]["by"] == "seeder"


@pytest.mark.parametrize("sid", ["G1", "G3", "G4"])
def test_simulated_scenarios_never_pass_as_real(acceptance, sid) -> None:
    pf, s, evidence = acceptance
    _seed_all_phases(pf, s, sid)
    code = run.check(pf, {sid}, "A")
    outcome = _results(evidence)[sid]
    assert outcome["status"] == "passed_simulated", outcome
    assert code == 4                                     # accepted within scope, not accepted


def test_second_identity_scenarios_are_never_passed_on_seeded_data(acceptance) -> None:
    pf, _, evidence = acceptance
    for sid in ("C4", "L1", "L3", "G2"):
        run.record_action(pf, sid, "seeder cannot produce this", by="seeder")
    run.check(pf, {"C4", "L1", "L3", "G2"}, "A")
    assert {o["status"] for o in _results(evidence).values()} == {"needs_identity"}
    run.record_action(pf, "C4", "a genuine second account invited the business", by="real-identity")
    run.check(pf, {"C4"}, "A")
    assert _results(evidence)["C4"]["status"] == "failed"   # evaluated now: no such data exists
