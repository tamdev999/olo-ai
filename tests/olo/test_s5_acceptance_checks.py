"""S5 acceptance judgements (scripts/olo/m3/s5_acceptance/checks.py): each
check must fail on the defect it exists for, and pass a correct state."""
from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts/olo/m3/s5_acceptance/checks.py"
_spec = importlib.util.spec_from_file_location("s5_acceptance_checks", _PATH)
checks = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = checks
_spec.loader.exec_module(checks)

ALICE = "ent:cid_alice"


def _v(n, actor, action, confirmed, *, date=None, proposed=None):
    return {"version": n, "actor_role": actor, "action": action, "confirmed_date": date,
            "prior_confirmed_date": None, "proposed_date": proposed, "is_confirmed": confirmed,
            "source_doc_id": f"gmail:m{n}"}


def _ledger_after_request():
    return {
        "cmt_d": {"event_type": "delivery", "key": "ORD-501", "status": "scheduled",
                  "scheduled_date": "2026-06-10", "pending_action": "request_change",
                  "pending_proposed_date": "2026-06-15",
                  "versions": [_v(1, "business", "commit", 1, date="2026-06-10"),
                               _v(2, "customer", "request_change", 0, proposed="2026-06-15")]},
        "cmt_m": {"event_type": "meeting", "key": None, "status": "scheduled",
                  "scheduled_date": "2026-06-05", "pending_action": None, "pending_proposed_date": None,
                  "versions": [_v(1, "business", "commit", 1, date="2026-06-05")]},
    }


def _ledger_after_confirmation():
    led = _ledger_after_request()
    d = led["cmt_d"]
    d.update(scheduled_date="2026-06-15", pending_action=None, pending_proposed_date=None)
    d["versions"].append(_v(3, "business", "confirm_change", 1, date="2026-06-15"))
    return led


def _memory():
    return {
        "sig:1": {"text": "Your delivery is confirmed for 2026-06-10.", "tags": [ALICE, "ym:2026-06", "olo:prose"]},
        "sig:2": {"text": "Could we move the ORD-501 delivery to 2026-06-15?", "tags": [ALICE, "ym:2026-06", "olo:prose"]},
        "ledger:cmt_d:v1": {"text": "Status as of version 1: scheduled, date 2026-06-10.",
                            "tags": [ALICE, "ym:2026-06", "kind:ledger.state"]},
        "ledger:cmt_m:v1": {"text": "Status as of version 1: scheduled, date 2026-06-05.",
                            "tags": [ALICE, "ym:2026-06", "kind:ledger.state"]},
    }


def _settled_inputs():
    return dict(runs=[{"status": "succeeded", "error": None, "projection": {"unchanged": 4}}],
                outbox={"operations": [{"kind": "retain", "status": "completed", "detail": None},
                                       {"kind": "consolidation", "status": "completed", "detail": None}],
                        "documents": {"c:m1": "confirmed", "c:m2": "confirmed"}},
                prose_pending=0,
                resolution={"c:m1": {"identity_status": "resolved", "commitment_status": "resolved"}},
                stopped_by_spend=False, max_runs=36)


# ── Processing must actually settle ──────────────────────────────────────────

def test_settled_processing_passes() -> None:
    assert checks.settle_findings(**_settled_inputs()).ok


@pytest.mark.parametrize("change, expect", [
    (lambda a: a["runs"][-1]["projection"].update(consolidation_in_flight=1), "still busy"),
    (lambda a: a["runs"][-1]["projection"].update(submitted=2), "still busy"),
    (lambda a: a["outbox"]["operations"].append({"kind": "retain", "status": "submitted", "detail": ""}),
     "retain operation submitted"),
    (lambda a: a["outbox"]["documents"].update({"c:m3": "pending"}), "projection status pending"),
    (lambda a: a.update(prose_pending=3), "still pending consolidation"),
    (lambda a: a.update(prose_pending=None), "pending count unavailable"),
    (lambda a: a.update(stopped_by_spend=True), "spend stop"),
    (lambda a: a.update(runs=[]), "no worker run"),
    (lambda a: a["runs"][-1]["projection"].update(budget_exceeded=1), "budget_exceeded"),
    (lambda a: a["resolution"]["c:m1"].update(commitment_status="failed", commitment_attempts=5),
     "held back from projection"),
])
def test_unsettled_processing_is_incomplete_never_a_pass(change, expect) -> None:
    args = copy.deepcopy(_settled_inputs())
    change(args)
    f = checks.settle_findings(**args)
    assert not f.ok and not f.ledger and not f.hindsight
    assert any(expect in x for x in f.incomplete), f.incomplete


def test_recovered_operation_is_noted_not_failed() -> None:
    args = _settled_inputs()
    args["outbox"]["operations"].insert(0, {"kind": "retain", "status": "failed", "detail": "HTTP 500"})
    f = checks.settle_findings(**args)
    assert f.ok and f.notes


# ── Ledger ────────────────────────────────────────────────────────────────────

def test_correct_ledgers_pass() -> None:
    assert checks.ledger_after_request(_ledger_after_request()).ok
    assert checks.ledger_after_confirmation(_ledger_after_confirmation()).ok


def test_customer_proposal_confirmed_without_authority_fails() -> None:
    led = _ledger_after_request()
    d = led["cmt_d"]
    d.update(scheduled_date="2026-06-15", pending_action=None, pending_proposed_date=None)
    d["versions"][1]["is_confirmed"] = 1
    f = checks.ledger_after_request(led)
    assert f.ledger and not f.hindsight


def test_lost_original_version_fails_after_confirmation() -> None:
    led = _ledger_after_confirmation()
    led["cmt_d"]["versions"].pop(0)
    assert checks.ledger_after_confirmation(led).ledger


# ── Hindsight: documents, scope, proposals ───────────────────────────────────

def test_ledger_documents_follow_confirmed_versions() -> None:
    led, mem = _ledger_after_request(), _memory()
    assert checks.ledger_documents(mem, led).ok
    mem["ledger:cmt_d:v2"] = {"text": "Status as of version 2: scheduled, date 2026-06-15.", "tags": [ALICE]}
    f = checks.ledger_documents(mem, led)
    assert any("unconfirmed version" in x for x in f.hindsight)
    assert any("states the unconfirmed proposal 2026-06-15" in x for x in f.hindsight)
    del mem["ledger:cmt_m:v1"]
    assert any("missing ledger document ledger:cmt_m:v1" in x for x in checks.ledger_documents(mem, led).hindsight)


@pytest.mark.parametrize("text", [
    "The ORD-501 delivery is confirmed for June 15, 2026.",
    "Alice's delivery is scheduled for 2026-06-15.",
    "Delivery of ORD-501 will arrive on 15 June.",
    "ORD-501 was moved to 15th June.",
])
def test_proposal_stated_as_fact_fails(text) -> None:
    f = checks.no_proposal_as_fact([("observation", text)], ["2026-06-15"])
    assert f.hindsight and not f.ledger and not f.incomplete


@pytest.mark.parametrize("text", [
    "Alice asked to move the ORD-501 delivery from 2026-06-10 to 2026-06-15.",
    "Alice requested rescheduling the delivery to June 15; not yet confirmed.",
    "Could we move the ORD-501 delivery to 2026-06-15?",
    "The ORD-501 delivery is confirmed for 2026-06-10.",
])
def test_proposal_stated_as_request_passes(text) -> None:
    assert checks.no_proposal_as_fact([("observation", text)], ["2026-06-15"]).ok


def test_unconfirmed_proposals_come_from_the_ledger() -> None:
    assert checks.unconfirmed_proposals(_ledger_after_request()) == ["2026-06-15"]
    assert checks.unconfirmed_proposals(_ledger_after_confirmation()) == []


def test_customer_scope() -> None:
    obs = [{"tags": [ALICE, "ym:2026-06"], "text": "Alice's delivery ORD-501 is due 2026-06-10."}]
    assert checks.customer_scope(_memory(), obs, ALICE).ok
    assert any("no observation" in x for x in checks.customer_scope(_memory(), [], ALICE).incomplete)
    leak = obs + [{"tags": ["ent:cid_bob"], "text": "Bob's order."}]
    assert checks.customer_scope(_memory(), leak, ALICE).hindsight
    mem = _memory()
    mem["sig:2"]["tags"].append("ent:cid_bob")
    assert checks.customer_scope(mem, obs, ALICE).hindsight
    ledger_obs = obs + [{"tags": [ALICE, "kind:ledger.state"], "text": "x"}]
    assert checks.customer_scope(_memory(), ledger_obs, ALICE).hindsight


def test_recall_must_state_the_date() -> None:
    assert checks.recall_states([{"text": "Delivery on 2026-06-15."}], "2026-06-15", "r").ok
    assert checks.recall_states([{"text": "Delivery on 2026-06-10."}], "2026-06-15", "r").hindsight
    assert checks.recall_states([], "2026-06-15", "r").incomplete


def test_probe_replacement_detects_stale_memory() -> None:
    good = {"text": "Bob ORD-777 delivery 2026-07-09.", "history": [{"facts_removed": 1}],
            "observations": [{"text": "Bob's ORD-777 is due 2026-07-09."}],
            "recall": [{"text": "ORD-777 on 2026-07-09"}]}
    assert checks.probe_replacement(good, new_iso="2026-07-09", old_iso="2026-07-01").ok
    stale = copy.deepcopy(good)
    stale["observations"].append({"text": "Bob's ORD-777 will be delivered on July 1."})
    assert checks.probe_replacement(stale, new_iso="2026-07-09", old_iso="2026-07-01").hindsight


def test_operations_and_cancellation_require_terminal_success() -> None:
    assert checks.probe_operation({"retain": "completed", "consolidation": "completed",
                                   "prose_pending_after": 0}).ok
    assert checks.probe_operation({"retain": "completed", "consolidation": "pending"}).incomplete
    absent = {"status_after": {"status": "cancelled"}, "document": {"exists": False},
              "reconciled": {"persisted": {}, "absent": ["sig:probe-cancel-1"], "unknown": []}}
    assert checks.cancellation(absent).ok
    persisted = {"status_after": {"status": "cancelled"}, "document": {"exists": True, "units": 1},
                 "reconciled": {"persisted": {"sig:probe-cancel-1": 1}, "absent": [], "unknown": []}}
    f = checks.cancellation(persisted)
    assert f.ok and any("partial effect" in n for n in f.notes)
    assert checks.cancellation(dict(persisted, reconciled=absent["reconciled"])).hindsight
    assert checks.cancellation({"status_after": {"status": "cancelled"}}).hindsight   # unreconciled
    assert checks.cancellation(dict(absent, status_after={"status": "completed"})).incomplete


def test_run_and_replay_counts() -> None:
    run = {"status": "succeeded", "error": None, "inserted": 0, "duplicates": 4, "identity_resolved": 0}
    assert checks.run_ok(run, inserted=0, duplicates=4).ok
    assert checks.run_ok(dict(run, duplicates=3), inserted=0, duplicates=4).ledger
    assert checks.run_ok(dict(run, status="failed"), inserted=0).incomplete
    before = {"versions": 4, "operations": 5, "documents": 8}
    assert checks.replay_unchanged(before, dict(before)).ok
    assert checks.replay_unchanged(before, dict(before, versions=5)).ledger
    assert checks.replay_unchanged(before, dict(before, documents=9)).hindsight


def test_spend_and_reasoning() -> None:
    assert checks.spend({"total_usd_upper": 0.01, "hindsight_thoughts_tokens": 0}, 0.20).ok
    assert checks.spend({"total_usd_upper": 0.20, "hindsight_thoughts_tokens": 0}, 0.20).incomplete
    assert checks.spend({"total_usd_upper": 0.01, "hindsight_thoughts_tokens": 12}, 0.20).hindsight


# ── S5 live findings: current state, evidence scope, strategies, usage ───────

def test_ledger_markers_follow_the_ledgers_current_version() -> None:
    led = _ledger_after_confirmation()
    led["cmt_d"]["current_version"] = 3
    mem = {"ledger:cmt_d:v1": {"text": "SUPERSEDED: version 1 is history.", "tags": [ALICE, "ledger:superseded"]},
           "ledger:cmt_d:v3": {"text": "CURRENT: version 3 is the latest.", "tags": [ALICE, "ledger:current"]}}
    assert checks.ledger_markers(mem, led).ok
    mem["ledger:cmt_d:v1"] = {"text": "Commitment ledger state ...", "tags": [ALICE]}
    assert checks.ledger_markers(mem, led).hindsight


def _view(current_date, pending, superseded, memories, excluded=()):
    return {"current": [{"commitment_id": "cmt_d", "event_type": "delivery", "current_date": current_date,
                         "current_version": 3 if current_date == "2026-06-15" else 1,
                         "pending_date": pending, "superseded_dates": superseded}],
            "memories": list(memories), "excluded": list(excluded)}


def test_current_view_requires_labels_on_superseded_and_requested_dates() -> None:
    stale = "The ORD-501 delivery is confirmed for 2026-06-10. (rescheduled to 2026-06-15.)"
    good = _view("2026-06-15", None, ["2026-06-10"], [{"text": stale, "label": "historical"}])
    assert checks.current_view(good, {"delivery": ("2026-06-15", None)}).ok
    bad = _view("2026-06-15", None, ["2026-06-10"], [{"text": stale, "label": "memory"}])
    assert checks.current_view(bad, {"delivery": ("2026-06-15", None)}).hindsight
    asked = _view("2026-06-10", "2026-06-15", [], [{"text": "Delivery confirmed for June 15.",
                                                    "label": "memory"}])
    assert checks.current_view(asked, {"delivery": ("2026-06-10", "2026-06-15")}).hindsight
    wrong_ledger = _view("2026-06-15", None, ["2026-06-10"], [{"text": "Status as of version 1",
                                                               "label": "current_ledger",
                                                               "document_id": "ledger:cmt_d:v1"}])
    assert checks.current_view(wrong_ledger, {"delivery": ("2026-06-15", None)}).hindsight
    current_doc = _view("2026-06-15", None, ["2026-06-10"], [{
        "text": "CURRENT: version 3 ... supersedes version 1 (date 2026-06-10).",
        "label": "current_ledger", "document_id": "ledger:cmt_d:v3"}])
    assert checks.current_view(current_doc, {"delivery": ("2026-06-15", None)}).ok
    leaked = _view("2026-06-15", None, [], [], excluded=[{"text": "Bob's order"}])
    assert checks.current_view(leaked, {"delivery": ("2026-06-15", None)}).hindsight
    assert checks.current_view(good, {"delivery": ("2026-06-10", None)}).ledger
    assert checks.current_view(None, {"delivery": ("2026-06-15", None)}).hindsight


def test_authored_prose_and_strategy_and_log_checks() -> None:
    mem = {"sig:c:m2": {"text": "Could we move it?", "tags": [ALICE],
                        "metadata": {"text_scope": "authored", "quoted_history": "omitted"}}}
    assert checks.authored_prose(mem).ok
    mem["sig:c:m2"]["text"] += "\n\nOn Mon, 1 Jun 2026, Owner wrote:\n> Your delivery is confirmed."
    assert checks.authored_prose(mem).hindsight
    want = {"prose": {"retain_extraction_mode": "concise", "enable_observations": True}}
    assert checks.bank_strategies(dict(want), want).ok
    assert checks.bank_strategies({}, want).hindsight
    assert checks.server_log("INFO ok").ok
    assert checks.server_log("x - WARNING - y - Unknown retain strategy 'prose', using").hindsight
    assert checks.server_log(None).incomplete


def test_usage_is_reconciled_by_call_type() -> None:
    parts = {"llm": {"count": 3, "input": 900, "output": 90, "thoughts": 0},
             "embedding": {"count": 5, "input": 100, "output": 0}, "rerank": {}, "decision": {}}
    good = {"total": {"count": 8, "input": 1000, "output": 90, "errors": 0}, "by_call_type": parts,
            "http_log_lines": 3}
    assert checks.usage(good).ok
    assert checks.usage(dict(good, total={"count": 9, "input": 1000, "output": 90})).incomplete
    assert checks.usage(None).incomplete


# ── The first live run (5bf8753b), re-judged ──────────────────────────────────

_LIVE = Path(__file__).resolve().parents[2] / "docs/olo/m3/s5_live_5bf8753b"


@pytest.fixture(scope="module")
def live() -> dict:
    import json
    return json.loads((_LIVE / "evidence.json").read_text(encoding="utf-8"))


def test_the_first_live_run_passed_its_own_checks(live) -> None:
    assert live["verdict"]["pass"] is True                     # the shortcoming being fixed


def test_new_checks_identify_the_first_live_runs_shortcomings(live) -> None:
    after = live["steps"]["4_state"]["result"]
    final = live["steps"]["10_final"]["result"]
    markers = checks.ledger_markers(after["memory"], after["ledger"])
    assert any("is superseded (current is v3) but not marked superseded" in x for x in markers.hindsight)
    assert any("current version but is not marked current" in x for x in markers.hindsight)
    assert checks.current_view(after.get("view"), {"delivery": ("2026-06-15", None)}).hindsight
    assert any("sig:c:m2 retains quoted history" in x for x in checks.authored_prose(after["memory"]).hindsight)
    log = checks.server_log((_LIVE / "server.redacted.log").read_text(encoding="utf-8"))
    assert any("5 retains fell back" in x and "'prose'" in x for x in log.hindsight)
    assert checks.cancellation(live["steps"]["9_cancellation"]["result"]).hindsight
    assert checks.usage(final.get("usage")).incomplete
    stale = checks.stale_raw_observations(after["observations"], after["ledger"])
    assert any("superseded 2026-06-10" in n and "confirmed for 2026-06-10" in n for n in stale.notes)
