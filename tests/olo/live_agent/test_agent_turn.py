"""The live Hermes agent-turn harness, offline (tests.olo.live_agent.turn).

The real ``AIAgent`` (deepseek provider, default tool surface, plugin
discovery, tool-search bridge) runs one turn against a scripted loopback
model behind the real ``MeteredUpstream``; only the model's replies are
fixed.  The judgement reads the ledger, never the tool's output.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
from tests.olo.live_agent import turn
from tests.olo.live_agent.metered import MeteredUpstream

KEY = "sk-offline-test-key"
GOOD = """Here is what I found in the ledger.
ORD-1 is scheduled for delivery on 2024-04-15 (it was originally 2024-04-10, but that changed).
ORD-2 is scheduled for 2024-04-15; you have requested a move to 2024-04-20, which is not confirmed yet.
ORD-3 was cancelled.
ORD-4 is scheduled for 2024-04-25.
```json
{"orders": [
 {"order": "ORD-1", "status": "scheduled", "confirmed_date": "2024-04-15", "pending_request": null},
 {"order": "ORD-2", "status": "scheduled", "confirmed_date": "2024-04-15", "pending_request": "2024-04-20"},
 {"order": "ORD-3", "status": "cancelled", "confirmed_date": null, "pending_request": null},
 {"order": "ORD-4", "status": "scheduled", "confirmed_date": "2024-04-25", "pending_request": null}]}
```"""
LOOKUP = ToolCall("tool_call", {"calls": [{"name": "olo_customer_context",
                                           "arguments": {"customer": turn.ALICE,
                                                         "question": "orders and delivery dates"}}]})


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from hermes_cli import plugins
    h = tmp_path / "home"
    h.mkdir()
    bundled = tmp_path / "no-bundled-plugins"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    plugins._reset_plugin_managers_for_tests()
    yield h
    plugins._reset_plugin_managers_for_tests()


def _run(home: Path, script, **caps) -> tuple[dict, FakeLLMServer]:
    with FakeLLMServer(script, api_key=KEY) as srv:
        evidence = turn.run_turn(home, srv.base_url, KEY, **caps)
        return evidence, srv


def test_the_ledger_truth_has_every_kind_of_commitment(home) -> None:
    with turn.business(home):
        truth = turn.ledger_truth(home / "olo" / "olo.db")
    assert truth == {
        "ORD-1": {"status": "scheduled", "confirmed_date": "2024-04-15", "pending_request": None,
                  "superseded": ["2024-04-10"]},
        "ORD-2": {"status": "scheduled", "confirmed_date": "2024-04-15", "pending_request": "2024-04-20",
                  "superseded": []},
        "ORD-3": {"status": "cancelled", "confirmed_date": None, "pending_request": None,
                  "superseded": ["2024-04-18"]},
        "ORD-4": {"status": "scheduled", "confirmed_date": "2024-04-25", "pending_request": None,
                  "superseded": []}}


def test_a_correct_answer_after_the_tool_passes(home) -> None:
    evidence, srv = _run(home, [LOOKUP, Text(GOOD)])
    v = evidence["verdict"]
    assert v["passed"], v["problems"]
    assert v["tool_results"] >= 1
    assert any("olo_customer_context" in json.dumps(c) for c in evidence["tool_invocations"])
    # Hindsight is background: stale observations only as "no longer true", the
    # other customer's excluded, and memory never authoritative
    seen = v["memory_seen"][0]
    assert seen["stale_marked_no_longer_true"] == 2 and seen["excluded_other_scope"] == 1
    assert seen["authoritative"] is False and "superseded_ledger" in seen["no_longer_true"]
    acct = evidence["accounting"]
    assert acct["forwarded_calls"] == len(srv.main_requests()) == 2 and acct["refused_calls"] == 0
    assert acct["prompt_tokens"] > 0 and 0 < acct["cost_usd"] <= turn.MAX_USD
    # the key reached the provider only through the meter, never the Hermes home or evidence
    assert all(KEY not in p.read_text(errors="ignore") for p in home.rglob("*") if p.is_file()
               and p.stat().st_size < 5_000_000)
    assert KEY not in json.dumps(evidence)


def test_a_pending_request_presented_as_confirmed_fails(home) -> None:
    bad = GOOD.replace("ORD-2 is scheduled for 2024-04-15; you have requested a move to 2024-04-20,"
                       " which is not confirmed yet.", "ORD-2 is now delivered on 2024-04-20.")
    bad = bad.replace('"confirmed_date": "2024-04-15", "pending_request": "2024-04-20"',
                      '"confirmed_date": "2024-04-20", "pending_request": null')
    v = _run(home, [LOOKUP, Text(bad)])[0]["verdict"]
    assert not v["passed"]
    assert any("pending request 2024-04-20" in p for p in v["problems"])
    assert any("ORD-2: summary confirmed_date" in p for p in v["problems"])


def test_a_superseded_date_presented_as_current_fails(home) -> None:
    bad = GOOD.replace("ORD-1 is scheduled for delivery on 2024-04-15 (it was originally 2024-04-10,"
                       " but that changed).", "ORD-1 arrives on 2024-04-10.")
    v = _run(home, [LOOKUP, Text(bad)])[0]["verdict"]
    assert not v["passed"] and any("superseded date 2024-04-10" in p for p in v["problems"])


def test_an_answer_without_the_tool_fails(home) -> None:
    v = _run(home, [Text(GOOD)])[0]["verdict"]
    assert not v["passed"] and "never received an olo_customer_context result" in v["problems"][0]


def test_the_call_cap_is_hard(home) -> None:
    evidence, srv = _run(home, [LOOKUP, Text(GOOD)], max_calls=1)
    acct = evidence["accounting"]
    assert acct["forwarded_calls"] == 1 and len(srv.main_requests()) == 1    # nothing past the cap
    assert acct["refused_calls"] >= 1 and not evidence["verdict"]["passed"]


def test_the_spend_cap_refuses_before_the_provider_sees_a_request() -> None:
    with FakeLLMServer([Text("x")], api_key=KEY) as srv, MeteredUpstream(
            srv.base_url, api_key=KEY, model=turn.MODEL, max_calls=5, max_usd=0.000001,
            max_output_tokens=800, price_in_per_m=0.3, price_out_per_m=1.2) as meter:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(meter.base_url + "/chat/completions", method="POST",
                                     data=json.dumps({"model": turn.MODEL, "messages": []}).encode(),
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code == 400 and b"spend cap" in exc.value.read()
        assert meter.refused == 1 and meter.calls == 0 and srv.main_requests() == []


def test_the_real_provider_needs_the_live_flag(tmp_path) -> None:
    with pytest.raises(SystemExit, match="--live"):
        turn.main(["--home", str(tmp_path / "h"), "--out", str(tmp_path / "e.json")])
    with pytest.raises(SystemExit, match="key-file"):
        turn.main(["--home", str(tmp_path / "h"), "--out", str(tmp_path / "e.json"), "--live"])
    with pytest.raises(SystemExit, match="lowered"):
        turn.run_turn(tmp_path / "h", "http://127.0.0.1:9/v1", KEY, max_calls=turn.MAX_CALLS + 1)
