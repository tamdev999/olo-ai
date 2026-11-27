"""The Google live acceptance harness, proven offline (no Google call).

scripts/olo/m3/google_acceptance is exercised against the loopback fake
Google server: the preflight refuses anything but the dedicated, read-only,
disposable setup; the recorder enforces its request budget and records
response shapes without personal data; and every scenario ends as passed,
failed, not_run or needs_approval — never a silent skip.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tests.olo.fake_google import FakeGoogleServer
from tests.olo.test_google_cron_e2e import _save_creds

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "olo" / "m3"))
from google_acceptance import guard, run, scenarios
from google_acceptance.recorder import BudgetExceeded, RecordingClient, shape, template

CUSTOMER = "customer@acceptance.test"


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    """A disposable acceptance home with the fake account connected."""
    home, evidence = tmp_path / "home", tmp_path / "evidence"
    home.mkdir()
    with FakeGoogleServer(email="biz@acceptance.test") as google:
        _save_creds(google, "accept", home)
        env = {"OLO_ACCEPTANCE_HOME": str(home), "OLO_ACCEPTANCE_ACCOUNT": google.email,
               "OLO_ACCEPTANCE_CONFIRM": google.email, "OLO_ACCEPTANCE_CUSTOMER": CUSTOMER,
               "OLO_ACCEPTANCE_EVIDENCE": str(evidence)}
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("HERMES_HOME", str(home))
        yield google, home, evidence, env


class TestPreflight:
    def test_the_dedicated_setup_passes(self, setup) -> None:
        _, _, _, env = setup
        pf = guard.run_preflight(env)
        assert pf.ok, pf.refusals
        assert pf.facts["profile_matches"] is True
        assert sorted(pf.facts["granted_scopes"]) == sorted(guard.ALLOWED_SCOPES)

    @pytest.mark.parametrize("change, needle", [
        ({"OLO_ACCEPTANCE_CONFIRM": "someone@else.test"}, "must repeat"),
        ({"OLO_ACCEPTANCE_CUSTOMER": None}, "not configured"),
        ({"OLO_ACCEPTANCE_ACCOUNT": "other@acceptance.test",
          "OLO_ACCEPTANCE_CONFIRM": "other@acceptance.test"}, "not OLO_ACCEPTANCE_ACCOUNT"),
    ])
    def test_misconfiguration_is_refused(self, setup, change, needle) -> None:
        _, _, _, env = setup
        env = {k: v for k, v in {**env, **change}.items() if v is not None}
        pf = guard.run_preflight(env)
        assert not pf.ok and any(needle in r for r in pf.refusals), pf.refusals

    def test_the_customer_must_be_a_second_identity(self, setup) -> None:
        _, _, _, env = setup
        pf = guard.settings_from({**env, "OLO_ACCEPTANCE_CUSTOMER": env["OLO_ACCEPTANCE_ACCOUNT"]})
        assert any("second account" in r for r in pf.refusals)

    def test_evidence_inside_the_credential_home_is_refused(self, setup) -> None:
        _, home, _, env = setup
        pf = guard.settings_from({**env, "OLO_ACCEPTANCE_EVIDENCE": str(home / "evidence")})
        assert any("apart from the home" in r for r in pf.refusals)

    def test_write_scopes_and_production_clients_are_refused(self, setup) -> None:
        google, _, _, env = setup
        pf = guard.settings_from(env)
        creds = google.make_credentials("accept", gmail_readonly=True)
        broad = replace(creds, granted_scopes=creds.granted_scopes | {
            "https://www.googleapis.com/auth/gmail.modify"}, oauth_publishing_status="production")
        guard.check_credentials(pf, load=lambda alias: broad, aliases=lambda: ["accept", "main"])
        text = " | ".join(pf.refusals)
        assert "gmail.modify" in text and "Testing mode" in text and "other Google accounts" in text

    def test_a_token_for_another_mailbox_is_refused(self, setup) -> None:
        _, _, _, env = setup
        pf = guard.settings_from(env)

        class Elsewhere:
            def get_json(self, *a):
                return {"emailAddress": "real.person@gmail.example"}
        guard.check_profile(pf, Elsewhere())
        assert any("another mailbox" in r for r in pf.refusals)


ROUTES_DEFAULT = ("Iface\tDestination\tGateway\tFlags\n"
                  "eth0\t00000000\t0100A8C0\t0003\neth0\t0000A8C0\t00000000\t0001\n")
ROUTES_PRIVATE_NS = "Iface\tDestination\tGateway\tFlags\n"


class TestEnvironment:
    """The live run's host checks (WSL default namespace, non-root, this checkout)."""

    def _check(self, **kw):
        from google_acceptance import environment
        repo = Path(__file__).resolve().parents[2]
        mods = {"olo": str(repo / "olo" / "__init__.py"),
                "hermes_cli": str(repo / "hermes_cli" / "__init__.py")}
        args = {"uid": 1001, "route_table": ROUTES_DEFAULT, "module_files": mods,
                "loopback": lambda: True, **kw}
        return environment.check(repo, **args)

    def test_a_suitable_host_passes(self) -> None:
        refusals, facts = self._check()
        assert refusals == [] and facts["default_route"] is True

    @pytest.mark.parametrize("change, needle", [
        ({"uid": 0}, "pg0 refuses root"),
        ({"route_table": ROUTES_PRIVATE_NS}, "private network namespace"),
        ({"module_files": {"olo": "/root/olo-linux/olo/__init__.py",
                           "hermes_cli": "/root/olo-linux/hermes_cli/__init__.py"}}, "not this checkout"),
        ({"loopback": lambda: False}, "still refused"),
    ])
    def test_an_unsuitable_host_is_refused(self, change, needle) -> None:
        refusals, _ = self._check(**change)
        assert any(needle in r for r in refusals), refusals

    def test_a_fresh_loopback_listener_is_reachable_after_the_wait(self) -> None:
        from google_acceptance import environment
        assert environment.loopback_reachable(wait_s=0.5) is True


class TestRecorder:
    def test_the_budget_stops_requests(self) -> None:
        class Echo:
            def get_json(self, *a):
                return {}
        rc = RecordingClient(Echo(), max_requests=2)
        rc.get_json("gmail", "/users/me/history", [])
        rc.get_json("gmail", "/users/me/history", [])
        with pytest.raises(BudgetExceeded):
            rc.get_json("gmail", "/users/me/history", [])

    def test_shapes_never_hold_personal_data(self) -> None:
        msg = {"id": "18c0ffee", "threadId": "t", "labelIds": ["INBOX"], "payload": {
            "mimeType": "multipart/alternative", "headers": [
                {"name": "From", "value": "Alice <alice@customer.example>"},
                {"name": "Subject", "value": "secret order 42"}],
            "parts": [{"mimeType": "text/plain", "body": {"data": "aGVsbG8=", "size": 5}}]}}
        text = json.dumps(shape(msg))
        assert "alice" not in text and "secret" not in text and "aGVsbG8" not in text
        assert "enum:multipart/alternative" in text and '"list": 2' in text
        assert template("/users/me/messages/18c0ffee") == "/users/me/messages/{id}"


class TestScenarios:
    def test_a_real_sync_then_a_spam_rescue_check(self, setup) -> None:
        google, _, evidence, env = setup
        pf = guard.run_preflight(env)
        run.sync(pf, max_requests=100, limit=5)                         # bootstrap
        google.gmail.add_message("r1", labels=["SPAM"], subject="[OLO-G5] please help",
                                 from_=f"Customer <{CUSTOMER}>", to=google.email)
        google.gmail.move_labels("r1", add=["INBOX"], remove=["SPAM"])
        out = run.sync(pf, max_requests=100, limit=5)
        assert out["requests"] <= 100
        run.record_action(pf, "G5", "moved to spam, then not spam")
        code = run.check(pf, {"G5"}, "A")
        result = json.loads((evidence / "results.json").read_text(encoding="utf-8"))
        assert code == 0 and result["outcomes"][0]["status"] == "passed", result
        shapes = json.loads((evidence / "api_shapes.json").read_text(encoding="utf-8"))
        assert "gmail GET /users/me/history" in shapes
        assert "test_example" not in json.dumps(shapes) and "@" not in json.dumps(shapes)
        assert "access_fake" not in (evidence / "requests.jsonl").read_text(encoding="utf-8")

    def test_every_scenario_is_reported_and_none_is_skipped(self, setup) -> None:
        _, _, evidence, env = setup
        pf = guard.run_preflight(env)
        run.sync(pf, max_requests=100, limit=5)
        run.record_action(pf, "C1", "created, but nothing arrived")
        code = run.check(pf, None, "A")
        outcomes = {o["id"]: o["status"] for o in
                    json.loads((evidence / "results.json").read_text(encoding="utf-8"))["outcomes"]}
        assert set(outcomes) == {sc.id for sc in scenarios.SCENARIOS}
        assert outcomes["C1"] == "failed"                 # recorded, not hidden
        assert outcomes["L2"] == "needs_approval"         # mode B only
        assert outcomes["G1"] == "not_run"                # its action is not recorded
        assert outcomes["C4"] == "needs_identity"         # no second identity: never passed
        assert code == 1                                  # any failure rejects the run
        assert "| C1 | api | failed |" in (evidence / "results.md").read_text(encoding="utf-8")

    def test_an_incomplete_run_is_not_accepted(self) -> None:
        o = [scenarios.Outcome("G1", "passed", {}), scenarios.Outcome("G2", "not_run", {})]
        assert scenarios.verdict(o) == ("incomplete", 2)
        assert scenarios.verdict(o[:1]) == ("accepted", 0)

    @pytest.mark.parametrize("status", ["passed_simulated", "needs_identity"])
    def test_seeded_or_untested_cross_account_scenarios_are_never_full_acceptance(self, status) -> None:
        o = [scenarios.Outcome("G5", "passed", {}), scenarios.Outcome("X", status, {})]
        assert scenarios.verdict(o) == ("accepted_within_scope", 4)
        assert scenarios.verdict(o + [scenarios.Outcome("G6", "failed", {})])[1] == 1

    def test_unknown_actors_are_refused(self, setup) -> None:
        _, _, _, env = setup
        pf = guard.run_preflight(env)
        with pytest.raises(SystemExit):
            run.record_action(pf, "G1", "x", by="someone")

    def test_commands_refuse_before_any_work_when_unconfigured(self, monkeypatch, tmp_path) -> None:
        for key in ("OLO_ACCEPTANCE_HOME", "OLO_ACCEPTANCE_ACCOUNT", "OLO_ACCEPTANCE_CONFIRM",
                    "OLO_ACCEPTANCE_CUSTOMER", "OLO_ACCEPTANCE_EVIDENCE"):
            monkeypatch.delenv(key, raising=False)
        with pytest.raises(SystemExit) as exc:
            run.main(["sync"])
        assert exc.value.code == 3
