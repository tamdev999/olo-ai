"""Mode B acceptance, proven offline: seeder → Gmail → production extractor →
ledger → Hindsight projection → the Hermes customer-context tool.

The model function is the production one (``make_deepseek_model_fn``, built
by ``olo.activation``) pointed at a loopback fake DeepSeek, so the request
shape, the non-thinking mode, the call cap and the spend accounting are the
real ones; only the replies are scripted.  Hindsight is the loopback fake;
projection is structured-only, so no prose reaches it.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from tests.olo.fake_google import FakeGoogleServer
from tests.olo.fake_s5_services import FakeDeepSeek, FakeHindsight
from tests.olo.live_seed import acceptance_fixtures as af
from tests.olo.live_seed.fake_write import FakeWriteApi
from tests.olo.live_seed.test_acceptance_fixtures import ACCOUNT, CUSTOMER, RUN, _session
from tests.olo.test_google_cron_e2e import _save_creds

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "olo" / "m3"))
from google_acceptance import guard, mode_b, run, scenarios

TENANT = "tenant-key-for-tests-only"


def _routes(s: af.Session) -> dict[str, dict]:
    """Scripted replies, latest message first: a reply quotes the one before it."""
    order, d1, d2 = af._b1(s)
    a, b = f"{d1:%A %d %B %Y}", f"{d2:%A %d %B %Y}"
    iso1, iso2 = d1.date().isoformat(), d2.date().isoformat()

    def reply(action, passage, **dates):
        return {"event_type": "delivery", "action": action, "business_key": order,
                "confirmed_date": dates.get("confirmed"), "proposed_date": dates.get("proposed"),
                "prior_confirmed_date": dates.get("prior"), "supporting_passage": passage,
                "confidence": 0.95}
    return {
        "We have cancelled the": reply("cancel", f"We have cancelled the {order} delivery that was"
                                                 f" scheduled for {b}", prior=iso2),
        "please cancel the": reply("cancel", f"please cancel the {order} delivery on {b}", prior=iso2),
        "Confirmed: your": reply("confirm_change", f"Confirmed: your {order} delivery is moved from {a}"
                                                    f" to {b}", confirmed=iso2, prior=iso1),
        "Could we move the": reply("request_change", f"Could we move the {order} delivery from {a} to {b}",
                                   proposed=iso2, prior=iso1),
        "is confirmed for delivery on": reply("commit", f"Your order {order} is confirmed for delivery"
                                                        f" on {a}", confirmed=iso1),
    }


@pytest.fixture()
def mode_b_home(tmp_path, monkeypatch):
    home, evidence = tmp_path / "home", tmp_path / "evidence"
    home.mkdir()
    with FakeGoogleServer(email=ACCOUNT) as google, FakeHindsight(TENANT) as hindsight, \
            FakeDeepSeek({}) as deepseek:
        _save_creds(google, "accept", home)
        (home / "hindsight_port_registry.json").write_text(
            json.dumps({run.BIZ: {"port": hindsight.port, "bank_id": run.BANK}}), encoding="utf-8")
        biz = home / "businesses" / run.BIZ
        biz.mkdir(parents=True)
        (biz / ".env").write_text(f"HINDSIGHT_API_TENANT_API_KEY={TENANT}\n"
                                  "HINDSIGHT_API_LLM_PROVIDER=none\n", encoding="utf-8")
        key = tmp_path / "deepseek.env"
        key.write_text("DEEPSEEK_API_KEY=sk-test-not-a-real-key\n", encoding="utf-8")
        key.chmod(stat.S_IRUSR | stat.S_IWUSR)
        env = {"OLO_ACCEPTANCE_HOME": str(home), "OLO_ACCEPTANCE_ACCOUNT": ACCOUNT,
               "OLO_ACCEPTANCE_CONFIRM": ACCOUNT, "OLO_ACCEPTANCE_CUSTOMER": CUSTOMER,
               "OLO_ACCEPTANCE_EVIDENCE": str(evidence)}
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("HERMES_HOME", str(home))
        pf = guard.run_preflight(env)
        assert pf.ok, pf.refusals
        run.sync(pf, max_requests=200, limit=5)                       # Gmail bootstrap (mode A)
        s = _session(FakeWriteApi(google, ACCOUNT), evidence=evidence)
        deepseek.routes = _routes(s)
        yield pf, s, evidence, deepseek, str(key), env


def _phase(pf, s, deepseek, key, n: int, caps: mode_b.Caps) -> None:
    af.seed_phase(s, "B1", n)
    run.sync(pf, max_requests=300, limit=5, mode="B", key_file=key, run=RUN, caps=caps,
             deepseek_base_url=deepseek.base_url)
    run.snapshot(pf, "B1", run=RUN, mode="B")


def test_b1_request_confirmation_change_and_cancellation(mode_b_home) -> None:
    pf, s, evidence, deepseek, key, _ = mode_b_home
    caps = mode_b.caps(8, 0.05)
    for n in range(1, len(af.PHASES["B1"]) + 1):
        _phase(pf, s, deepseek, key, n, caps)
    run.record_action(pf, "B1", f"seeded, run {RUN}", by="seeder", run=RUN)
    run.check(pf, {"B1"}, "B", run=RUN)
    report = json.loads(run.results_path(evidence, RUN).read_text(encoding="utf-8"))
    [b1] = [o for o in report["outcomes"] if o["id"] == "B1"]
    assert b1["status"] == "passed", b1["details"]["problems"]
    d = b1["details"]
    assert [(v["action"], v["actor"], v["confirmed"]) for v in d["versions"]] == \
        [tuple(x) for x in scenarios.B1_SEQUENCE]
    assert set(d["memory_documents"].values()) == {"confirmed"}
    assert d["tool_current"]["status"] == "cancelled"
    # Every model request was the production request, one per new email, within the cap.
    assert len(deepseek.requests) == 5 == d["model_usage"]["calls_used"]
    assert all(q["model"] == "deepseek-flash" and q["thinking"] == {"type": "disabled"}
               for q in deepseek.requests)
    facts = json.loads((evidence / "facts.json").read_text(encoding="utf-8"))["runs"][RUN]
    snaps = facts["snapshots"]["B1"]
    assert snaps[1]["scheduled_date"] == snaps[0]["scheduled_date"]       # a request is not truth
    assert snaps[1]["pending_proposed_date"] == snaps[2]["scheduled_date"]
    assert snaps[3]["status"] == "scheduled" and snaps[3]["pending_action"] == "cancel"
    tool = facts["B1_tool_output"]
    [entry] = [c for c in tool["current"] if c["status"] == "cancelled"]
    assert entry["superseded_dates"]                                      # the moved-from date
    text = "\n".join(p.read_text(encoding="utf-8") for p in evidence.iterdir() if p.is_file())
    assert ACCOUNT not in text and "sk-test-not-a-real-key" not in text


def test_the_run_cap_stops_model_requests(mode_b_home) -> None:
    pf, s, _, deepseek, key, _ = mode_b_home
    caps = mode_b.caps(3, 0.05)
    for n in (1, 2, 3):
        _phase(pf, s, deepseek, key, n, caps)
    af.seed_phase(s, "B1", 4)
    with pytest.raises(SystemExit, match="cap reached"):
        run.sync(pf, max_requests=300, limit=5, mode="B", key_file=key, run=RUN, caps=caps,
                 deepseek_base_url=deepseek.base_url)
    assert len(deepseek.requests) == 3


def test_a_failing_model_cannot_exceed_the_cap(mode_b_home) -> None:
    pf, s, evidence, deepseek, key, _ = mode_b_home
    caps = mode_b.caps(2, 0.05)
    deepseek.fail_status = 500
    for n in (1, 2):
        af.seed_phase(s, "B1", n)
    run.sync(pf, max_requests=300, limit=5, mode="B", key_file=key, run=RUN, caps=caps,
             deepseek_base_url=deepseek.base_url)
    with pytest.raises(SystemExit, match="cap reached"):
        run.sync(pf, max_requests=300, limit=5, mode="B", key_file=key, run=RUN, caps=caps,
                 deepseek_base_url=deepseek.base_url)
    used = json.loads((evidence / "facts.json").read_text(encoding="utf-8"))["runs"][RUN]["mode_b"]
    assert len(deepseek.requests) == 2 and used["failed_calls"] == 2 and used["cost_usd"] > 0


def test_mode_b_refuses_a_home_with_a_prose_backlog(mode_b_home) -> None:
    pf, s, evidence, deepseek, key, _ = mode_b_home
    other = af.Session(s.api, "acc20261127t130000cafe", ACCOUNT, CUSTOMER,
                       af.new_run({"runs": {}}, "acc20261127t130000cafe", ACCOUNT, CUSTOMER,
                                  af.datetime.fromisoformat(s.run["created_at"])), evidence)
    af.seed_phase(other, "B1", 1)
    run.sync(pf, max_requests=300, limit=5)                       # mode A: the prose waits
    with pytest.raises(SystemExit, match="await extraction"):
        run.sync(pf, max_requests=300, limit=5, mode="B", key_file=key, run=RUN,
                 caps=mode_b.caps(8, 0.05), deepseek_base_url=deepseek.base_url)
    assert deepseek.requests == []


@pytest.mark.platforms("posix")
def test_the_offline_preflight(mode_b_home, capsys) -> None:
    pf, _, evidence, _, key, env = mode_b_home
    caps = mode_b.caps(8, 0.05)
    assert run.preflight_b(env, key_file=key, caps=caps, run=RUN) == 0
    facts = json.loads((evidence / "preflight_b.json").read_text(encoding="utf-8"))["facts"]
    assert facts["model_fn"]["model_id"].startswith("deepseek:deepseek-flash@https://api.deepseek.com")
    assert facts["max_retries"] == 0 and facts["hindsight_llm_provider"] == "none"
    os.chmod(key, 0o640)
    assert run.preflight_b(env, key_file=key, caps=caps, run=RUN) == 3
    os.chmod(key, 0o600)
    biz_env = pf.settings.home / "businesses" / run.BIZ / ".env"
    biz_env.write_text(biz_env.read_text(encoding="utf-8").replace("=none", "=deepseek"), encoding="utf-8")
    assert run.preflight_b(env, key_file=key, caps=caps, run=RUN) == 3
    assert "no Hindsight inference" in capsys.readouterr().err
