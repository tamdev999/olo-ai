"""The Hermes-facing ``olo_customer_context`` tool (post-audit batch 4).

The Olo plugin is installed in a temporary HERMES_HOME and loaded by
Hermes's own plugin discovery; every call goes through
``model_tools.handle_function_call``, as the agent's would.  The business
behind it is built by the real worker (identity, extraction with a mock
model, ledger, projection to a loopback Hindsight that enforces the tenant
key), so the tool reads what production would hold.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

import hermes_yaml as yaml
from olo.commitments import CommitmentStageConfig
from olo.projection import ProjectionConfig, ProjectionLimits
from olo.runner import run_source
from olo.store import connect
from tests.olo.fake_s5_services import FakeHindsight
from tests.olo.test_s5_pipeline import (
    COMMIT_BODY,
    CONFIRM_BODY,
    REQUEST_BODY,
    ROUTES,
    Adapter,
    Guard,
    Model,
    answer,
    content,
)

ORD2 = "Your order ORD-2 is confirmed for delivery on 2024-04-15."
ORD2_MOVE = "Could we move order ORD-2 from 2024-04-15 to 2024-04-20?"
BOB_ORDER = "Your order ORD-9 is confirmed for delivery on 2024-04-10."
VAGUE = "Please move my delivery to 2024-04-22."
TOOL_ROUTES = {
    **ROUTES,
    ORD2: answer("commit", ORD2.rstrip("."), key="ORD-2", confirmed="2024-04-15"),
    ORD2_MOVE: answer("request_change", ORD2_MOVE.rstrip("?"), key="ORD-2",
                      proposed="2024-04-20", prior="2024-04-15"),
    BOB_ORDER: answer("commit", BOB_ORDER.rstrip("."), key="ORD-9", confirmed="2024-04-10"),
    VAGUE: answer("request_change", VAGUE.rstrip("."), proposed="2024-04-22"),
}
ROOT = Path(__file__).resolve().parents[2]
KEY = "tenant-key-for-biz"


@pytest.fixture()
def business(tmp_path, monkeypatch):
    """A HERMES_HOME with the Olo plugin enabled and one business's data."""
    from hermes_cli import plugins
    from olo.activation import bind_business
    from olo.m3.hindsight.client import HindsightClient, HindsightConfig

    home = tmp_path / "home"
    shutil.copytree(ROOT / "olo" / "hermes_plugin", home / "plugins" / "olo",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["olo"]}}),
                                      encoding="utf-8")
    (home / "businesses" / "biz").mkdir(parents=True)
    (home / "businesses" / "biz" / ".env").write_text(f"HINDSIGHT_API_TENANT_API_KEY={KEY}\n",
                                                       encoding="utf-8")
    (home / "olo").mkdir()
    bundled = tmp_path / "no-bundled-plugins"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    with FakeHindsight(KEY) as hs:
        (home / "hindsight_port_registry.json").write_text(
            json.dumps({"biz": {"port": hs.port, "bank_id": "olo-biz"}}), encoding="utf-8")
        cfg = HindsightConfig.for_business("biz", registry_path=str(home / "hindsight_port_registry.json"))
        adapter = Adapter()
        adapter.queue.extend([
            content("m1", sent=True, body=COMMIT_BODY, day=1),
            content("m2", sent=False, body=REQUEST_BODY, day=2),
            content("m3", sent=True, body=CONFIRM_BODY, day=3),
            content("m4", sent=True, body=ORD2, day=4, thread="t2"),
            content("m5", sent=False, body=ORD2_MOVE, day=5, thread="t2"),
            content("m6", sent=True, body=BOB_ORDER, day=6, thread="t9", to="Bob <bob@y.com>"),
            content("m7", sent=False, body=VAGUE, day=7, thread="t7"),
        ])
        db = home / "olo" / "olo.db"
        r = run_source(adapter, db_path=db,
                       commitment=CommitmentStageConfig(model_fn=Model(TOOL_ROUTES)),
                       projection=ProjectionConfig(client=HindsightClient(cfg), bank_id="olo-biz",
                                                   usage_guard=Guard(), limits=ProjectionLimits()))
        assert r.status == "succeeded", r.error
        conn = connect(db)
        bind_business(conn, "biz", "2026-11-26T00:00:00Z")
        conn.commit()
        conn.close()
        plugins._reset_plugin_managers_for_tests()
        plugins.discover_plugins()
        yield home, hs
        plugins._reset_plugin_managers_for_tests()


def call(customer: str, question: str = "") -> dict:
    import model_tools
    return json.loads(model_tools.handle_function_call(
        "olo_customer_context", {"customer": customer, "question": question},
        task_id="t", session_id="s", tool_call_id="c"))


def contact(home: Path, email: str) -> str:
    conn = connect(home / "olo" / "olo.db")
    try:
        return conn.execute("SELECT contact_id FROM contact_identifiers WHERE identifier_value=?",
                            (email,)).fetchone()[0]
    finally:
        conn.close()


def by_key(brief: dict) -> dict[str, dict]:
    return {c["key"]: c for c in brief["current"]}


def test_the_tool_is_registered_through_the_plugin(business) -> None:
    import model_tools
    names = {d["function"]["name"] for d in model_tools.get_tool_definitions(
        enabled_toolsets=["olo"], skip_tool_search_assembly=True)}
    assert "olo_customer_context" in names


def test_current_state_comes_from_the_ledger_with_requests_unconfirmed(business) -> None:
    brief = call("alice@x.com")
    assert brief["identity"]["status"] == "verified" and brief["actions"] == "disabled"
    assert brief["business"] == "biz"
    orders = by_key(brief)
    assert set(orders) == {"ORD-1", "ORD-2"}           # Bob's ORD-9 is not Alice's
    assert (orders["ORD-1"]["date"], orders["ORD-1"]["superseded_dates"]) == ("2024-04-15",
                                                                             ["2024-04-10"])
    assert orders["ORD-1"].get("pending_request") is None
    assert orders["ORD-2"]["date"] == "2024-04-15"
    assert orders["ORD-2"]["pending_request"] == {"action": "request_change",
                                                  "date": "2024-04-20", "confirmed": False}
    # Two commitments on one date: the agent is told to name them.
    assert any("2 delivery commitments are on 2024-04-15" in w for w in brief["warnings"])
    # The vague request matched no commitment and waits for an operator.
    [waiting] = brief["uncertain_ledger"]
    assert waiting["event"] == "customer request_change" and waiting["dates"] == {
        "proposed_date": "2024-04-22"}


def test_memories_are_labelled_and_stale_ones_never_current(business) -> None:
    home, hs = business
    alice, bob = contact(home, "alice@x.com"), contact(home, "bob@y.com")
    hs.recall_extra = [
        {"type": "observation", "text": "Order ORD-1 is scheduled for delivery on 2024-04-10.",
         "tags": [f"ent:{alice}"]},
        {"type": "world", "text": "Bob's order ORD-9 ships on 2024-04-10.", "tags": [f"ent:{bob}"]},
    ]
    brief = call("alice@x.com", "when is my delivery?")
    memory = brief["memory"]
    assert memory["status"] == "ok" and memory["authoritative"] is False
    stale = [m["text"] for m in memory["no_longer_true"]]
    assert "Order ORD-1 is scheduled for delivery on 2024-04-10." in stale
    everything = json.dumps(memory)
    assert "ORD-9" not in everything and memory["excluded_other_scope"] >= 1
    assert by_key(brief)["ORD-1"]["date"] == "2024-04-15"   # the ledger, not the memory


def test_an_unreachable_hindsight_never_hides_the_ledger(business) -> None:
    _, hs = business
    hs.down = True
    brief = call("alice@x.com")
    assert brief["memory"]["status"].startswith("unavailable")
    assert set(by_key(brief)) == {"ORD-1", "ORD-2"}


def test_disputed_and_unknown_identities_return_no_customer_data(business) -> None:
    home, _ = business
    alice = contact(home, "alice@x.com")
    conn = connect(home / "olo" / "olo.db")
    conn.execute("INSERT INTO identifier_conflicts (identifier_type, identifier_value,"
                 " existing_contact_id, new_contact_id, source, detected_at)"
                 " VALUES ('email', 'alice@x.com', ?, 'cid_someone_else', 'crm', 'x')", (alice,))
    conn.commit()
    conn.close()
    disputed = call("alice@x.com")
    assert disputed["identity"]["status"] == "disputed" and "current" not in disputed
    unknown = call("nobody@nowhere.example")
    assert unknown["identity"]["status"] == "unknown" and "current" not in unknown
    assert call("Alice")["identity"]["status"] == "invalid"     # never by name


def test_the_tool_never_writes(business) -> None:
    home, hs = business
    db = home / "olo" / "olo.db"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    requests_before = len(hs.requests)
    call("alice@x.com", "anything")
    call("bob@y.com")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert all(m in ("GET", "POST") for m, _ in hs.requests[requests_before:])
    assert all(p.endswith("/memories/recall") for m, p in hs.requests[requests_before:]
               if m == "POST")


def test_each_customer_sees_only_their_own_commitments(business) -> None:
    brief = call("bob@y.com")
    assert set(by_key(brief)) == {"ORD-9"} and brief["uncertain_ledger"] == []


def test_a_memory_pointing_at_another_customers_ledger_never_attaches(business) -> None:
    """A recalled memory carrying Alice's tag but Bob's ledger document id
    (a misprojection) is never tied to a commitment, and never current."""
    home, hs = business
    alice = contact(home, "alice@x.com")
    conn = connect(home / "olo" / "olo.db")
    bobs = conn.execute("SELECT id FROM commitments WHERE business_key='ORD-9'").fetchone()[0]
    conn.close()
    hs.recall_extra = [{"type": "world", "document_id": f"ledger:{bobs}:v1",
                        "text": "CURRENT: order ORD-1 delivery on 2024-04-01.",
                        "tags": [f"ent:{alice}"]}]
    brief = call("alice@x.com")
    planted = [m for g in ("supporting", "no_longer_true", "unconfirmed", "uncertain")
               for m in brief["memory"][g] if "2024-04-01" in m["text"]]
    assert planted and all(m["commitment_id"] is None for m in planted)
    assert all(m["label"] == "uncertain" for m in planted)
    assert by_key(brief)["ORD-1"]["date"] == "2024-04-15"


def _find_brief(obj):
    """The brief inside a tool result, however the bridge wraps it."""
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError:
            return None
    if isinstance(obj, dict):
        if obj.get("authority") and "actions" in obj:
            return obj
        obj = list(obj.values())
    if isinstance(obj, list):
        for item in obj:
            found = _find_brief(item)
            if found:
                return found
    return None


def test_a_real_agent_turn_reads_the_ledger_through_the_tool(business) -> None:
    """A real Hermes ``AIAgent`` turn with the default tool surface: the Olo
    tool is a deferred plugin tool behind Hermes's tool-search bridge, the
    (scripted, loopback) model calls it through ``tool_call``, and the agent
    loop returns the brief to the model.  No inference: the model's replies
    are fixed; everything between them — tool listing, bridge dispatch,
    result injection — is Hermes."""
    from run_agent import AIAgent
    from tests.e2e.core.history._helpers import NO_BACKGROUND_REVIEW, OFFLINE_CONFIG
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home

    home, hs = business
    alice = contact(home, "alice@x.com")
    hs.recall_extra = [{"type": "observation", "tags": [f"ent:{alice}"],
                        "text": "Order ORD-1 is scheduled for delivery on 2024-04-10."}]
    call_args = {"customer": "alice@x.com", "question": "when is order ORD-1 delivered?"}
    script = [ToolCall("tool_call", {"calls": [{"name": "olo_customer_context",
                                                "arguments": call_args}]}),
              Text("ORD-1 is scheduled for 2024-04-15.")]
    with FakeLLMServer(script) as srv:
        write_hermes_home(home, srv.base_url, extra_config=(
            "plugins:\n  enabled:\n  - olo\n" + OFFLINE_CONFIG + NO_BACKGROUND_REVIEW))
        agent = AIAgent(provider="custom", base_url=srv.base_url, api_key="sk-fake",
                        model="fake-model", session_id="olo-context-turn", quiet_mode=True,
                        platform="cli", skip_memory=True)
        try:
            result = agent.run_conversation("When is my order ORD-1 delivered?",
                                            conversation_history=[], task_id="t")
        finally:
            agent.close()
        first, second = srv.main_requests()[:2]
    assert "olo_customer_context" in json.dumps(first.get("tools") or [])   # discoverable
    [tool_msg] = [m for m in second["messages"] if m.get("role") == "tool"]
    brief = _find_brief(tool_msg["content"])
    assert brief is not None, tool_msg["content"][:500]
    assert brief["actions"] == "disabled"
    assert by_key(brief)["ORD-1"]["date"] == "2024-04-15"             # SQLite, not memory
    stale = [m["text"] for m in brief["memory"]["no_longer_true"]]
    assert "Order ORD-1 is scheduled for delivery on 2024-04-10." in stale
    assert result.get("final_response") == "ORD-1 is scheduled for 2024-04-15."
