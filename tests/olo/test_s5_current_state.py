"""S5 live findings — current operational truth, evidence scope, strategies
and cancellation (offline; model and Hindsight are mocks).

The first live S5 acceptance showed, after the business confirmed a move
from 2026-06-10 to 2026-06-15:
  * an observation still leading with "confirmed for 2026-06-10";
  * ledger-state documents v1 and v3 side by side with nothing marking v3 as
    current;
  * a fact extracted from the quoted part of the customer's reply;
  * "Unknown retain strategy 'prose'" fallbacks;
  * a cancelled retain whose document was persisted anyway.
These tests pin the deterministic corrections: the ledger decides current
state, ledger documents are marked current / superseded from it, recall for
a customer is labelled against it regardless of how a memory is worded,
prose is retained as authored text only, strategies are registered or
refused, and cancellations are reconciled against the documents the server
actually holds.
"""
from __future__ import annotations

import sqlite3

import pytest

from olo.current_state import (
    CONSISTENT,
    CURRENT_LEDGER,
    HISTORICAL,
    SUPERSEDED_LEDGER,
    UNCERTAIN,
    UNCONFIRMED_REQUEST,
    current_truth,
    customer_context,
    customer_state,
    dates_mentioned,
    label_memories,
)
from olo.ledger import LedgerEvent, migrate, replay_events
from olo.projection import LEDGER_CURRENT_TAG, LEDGER_SUPERSEDED_TAG, _ledger_documents
from olo.signal_content import load_content
from olo.store import connect, get_blob_raw
from tests.olo.test_projection_guard import Clock, _ops
from tests.olo.test_s5_pipeline import (
    COMMIT_BODY,
    CONFIRM_BODY,
    REQUEST_BODY,
    Pipeline,
    content,
)

pytestmark = pytest.mark.not_integration

ALICE, BOB = "cid_alice", "cid_bob"
TS = "2026-06-30T00:00:00+00:00"


def _ev(**kw) -> LedgerEvent:
    d = dict(event_type="delivery", contact_id=ALICE, business_key="ORD-501",
             source_doc_id="gmail:m1", source_kind="email.content",
             occurred_at="2026-06-01T09:00:00+00:00", actor_role="business", action="commit",
             proposed_date=None, confirmed_date="2026-06-10", prior_confirmed_date=None,
             thread_id="t-ord501", notes="")
    d.update(kw)
    return LedgerEvent(**d)


COMMIT = _ev()
REQUEST = _ev(source_doc_id="gmail:m2", occurred_at="2026-06-02T09:00:00+00:00",
              actor_role="customer", action="request_change", confirmed_date=None,
              proposed_date="2026-06-15", prior_confirmed_date="2026-06-10")
CONFIRM = _ev(source_doc_id="gmail:m4", occurred_at="2026-06-03T09:00:00+00:00",
              action="confirm_change", confirmed_date="2026-06-15",
              prior_confirmed_date="2026-06-10")


def _ledger(*events) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    replay_events(conn, list(events), recorded_at=TS)
    return conn


def _docs(conn) -> dict[str, dict]:
    return {d["document_id"]: d for d in _ledger_documents(conn)}


def _version_doc(docs: dict, v: int) -> dict:
    return next(d for k, d in docs.items() if k.endswith(f":v{v}"))


# ── Ledger-state documents: exactly one current, from the ledger ──────────────

class TestLedgerDocuments:
    def test_before_confirmation_the_original_is_current_and_the_request_has_no_state(self) -> None:
        docs = _docs(_ledger(COMMIT, REQUEST))
        assert [k.rsplit(":", 1)[1] for k in docs] == ["v1"]
        v1 = _version_doc(docs, 1)
        assert v1["content"].startswith("CURRENT: version 1")
        assert LEDGER_CURRENT_TAG in v1["tags"] and v1["metadata"]["ledger_role"] == "current"
        assert "2026-06-15" not in v1["content"]

    def test_after_confirmation_the_original_is_marked_superseded(self) -> None:
        docs = _docs(_ledger(COMMIT, REQUEST, CONFIRM))
        v1, v3 = _version_doc(docs, 1), _version_doc(docs, 3)
        assert v1["content"].startswith("SUPERSEDED: version 1 is history")
        assert "Current confirmed state (version 3): scheduled, date 2026-06-15" in v1["content"]
        assert LEDGER_SUPERSEDED_TAG in v1["tags"] and LEDGER_CURRENT_TAG not in v1["tags"]
        assert v3["content"].startswith("CURRENT: version 3")
        assert [d for d in docs.values() if LEDGER_CURRENT_TAG in d["tags"]] == [v3]

    def test_confirmation_changes_the_superseded_document_so_it_is_reprojected(self) -> None:
        conn = _ledger(COMMIT, REQUEST)
        before = _version_doc(_docs(conn), 1)
        replay_events(conn, [CONFIRM], recorded_at=TS)
        after = _version_doc(_docs(conn), 1)
        assert before["document_id"] == after["document_id"]
        assert before["metadata"]["content_sha256"] != after["metadata"]["content_sha256"]

    def test_out_of_order_arrival_current_is_the_latest_instant_not_the_highest_version(self) -> None:
        later = _ev(source_doc_id="gmail:m9", occurred_at="2026-06-05T09:00:00+00:00",
                    confirmed_date="2026-06-20")
        earlier = _ev(occurred_at="2026-06-01T09:00:00+00:00", confirmed_date="2026-06-10")
        docs = _docs(_ledger(later, earlier))              # v1 = later event, v2 = earlier
        assert LEDGER_CURRENT_TAG in _version_doc(docs, 1)["tags"]
        assert LEDGER_SUPERSEDED_TAG in _version_doc(docs, 2)["tags"]
        [state] = customer_state(_ledger(later, earlier), ALICE)
        assert (state.current_version, state.current_date) == (1, "2026-06-20")
        assert state.superseded_dates == ("2026-06-10",)

    def test_business_cancellation_is_current_and_the_cancelled_date_is_history(self) -> None:
        cancel = _ev(source_doc_id="gmail:m5", occurred_at="2026-06-04T09:00:00+00:00",
                     action="cancel", confirmed_date=None, prior_confirmed_date="2026-06-10")
        conn = _ledger(COMMIT, cancel)
        docs = _docs(conn)
        current = [d for d in docs.values() if LEDGER_CURRENT_TAG in d["tags"]]
        assert len(current) == 1 and current[0]["metadata"]["status"] == "cancelled"
        [state] = customer_state(conn, ALICE)
        assert state.status == "cancelled" and state.current_date is None
        assert state.superseded_dates == ("2026-06-10",)

    def test_replay_produces_identical_documents(self) -> None:
        conn = _ledger(COMMIT, REQUEST, CONFIRM)
        first = _docs(conn)
        replay_events(conn, [COMMIT, REQUEST, CONFIRM], recorded_at=TS)
        assert _docs(conn) == first


# ── Customer-facing retrieval: labelled against the ledger ────────────────────

def _obs(text: str, ent: str = ALICE) -> dict:
    return {"type": "observation", "text": text, "document_id": None, "tags": [f"ent:{ent}", "ym:2026-06"]}


class TestCustomerRetrieval:
    def test_state_before_confirmation_keeps_the_request_pending(self) -> None:
        [s] = customer_state(_ledger(COMMIT, REQUEST), ALICE)
        assert (s.status, s.current_date, s.current_version) == ("scheduled", "2026-06-10", 1)
        assert (s.pending_action, s.pending_date) == ("request_change", "2026-06-15")
        assert s.superseded_dates == ()

    def test_state_after_confirmation(self) -> None:
        [s] = customer_state(_ledger(COMMIT, REQUEST, CONFIRM), ALICE)
        assert (s.current_date, s.current_version, s.pending_date) == ("2026-06-15", 3, None)
        assert s.superseded_dates == ("2026-06-10",)

    @pytest.mark.parametrize("text", [
        "Alice requested to move the ORD-501 delivery from 2026-06-10 to 2026-06-15.",
        "The ORD-501 delivery is confirmed for June 15.",        # wrong wording, same label
    ])
    def test_before_confirmation_the_proposed_date_is_always_an_unconfirmed_request(self, text) -> None:
        conn = _ledger(COMMIT, REQUEST)
        [m], _ = label_memories([_obs(text)], customer_state(conn, ALICE), f"ent:{ALICE}")
        assert m.label == UNCONFIRMED_REQUEST and not m.authoritative
        assert any("not confirmed by the business" in n for n in m.notes)

    def test_after_confirmation_the_live_stale_observation_is_historical(self) -> None:
        conn = _ledger(COMMIT, REQUEST, CONFIRM)
        stale = ("Order ORD-501's delivery is confirmed for 2026-06-10. (2026-06-03 09:00: Alice "
                 "Customer's ORD-501 delivery was rescheduled from 2026-06-10 to 2026-06-15.)")
        [m], _ = label_memories([_obs(stale)], customer_state(conn, ALICE), f"ent:{ALICE}")
        assert m.label == HISTORICAL and m.link == "business_key"
        assert any("no longer in force" in n and "2026-06-15" in n for n in m.notes)

    def test_ledger_documents_are_labelled_by_version_from_the_ledger(self) -> None:
        conn = _ledger(COMMIT, REQUEST, CONFIRM)
        cid = conn.execute("SELECT id FROM commitments").fetchone()[0]
        results = [{"type": "world", "text": "Status as of version 1: scheduled, date 2026-06-10.",
                    "document_id": f"ledger:{cid}:v1", "tags": [f"ent:{ALICE}"]},
                   {"type": "world", "text": "Status as of version 3: scheduled, date 2026-06-15.",
                    "document_id": f"ledger:{cid}:v3", "tags": [f"ent:{ALICE}"]}]
        labelled, _ = label_memories(results, customer_state(conn, ALICE), f"ent:{ALICE}")
        assert [m.label for m in labelled] == [SUPERSEDED_LEDGER, CURRENT_LEDGER]

    def test_another_customers_memory_is_excluded(self) -> None:
        conn = _ledger(COMMIT)
        labelled, excluded = label_memories(
            [_obs("Bob's ORD-501 is due 2026-06-10.", ent=BOB), _obs("Thanks for the call.")],
            customer_state(conn, ALICE), f"ent:{ALICE}")
        assert [m.text for m in labelled] == ["Thanks for the call."]
        assert labelled[0].label == UNCERTAIN and len(excluded) == 1

    def test_month_day_dates_resolve_in_the_ledgers_years(self) -> None:
        assert dates_mentioned("on 15th of June and Jun 10, or 2026-06-05", {2026}) == {
            "2026-06-15", "2026-06-10", "2026-06-05"}


# ── Commitment-specific provenance (audit D4) ─────────────────────────────────

def _orders():
    """The audit's counterexample: three orders of one customer.

    ORD-501 moved 06-10 → 06-15 (confirmed); ORD-777 is current on 06-10;
    ORD-888 is current on 06-20 with a pending customer request for 06-15.
    """
    return _ledger(
        COMMIT, CONFIRM,
        _ev(business_key="ORD-777", source_doc_id="gmail:b1", thread_id="t2",
            occurred_at="2026-06-04T09:00:00+00:00", confirmed_date="2026-06-10"),
        _ev(business_key="ORD-888", source_doc_id="gmail:c1", thread_id="t3",
            occurred_at="2026-06-04T10:00:00+00:00", confirmed_date="2026-06-20"),
        _ev(business_key="ORD-888", source_doc_id="gmail:c2", thread_id="t3", actor_role="customer",
            occurred_at="2026-06-05T10:00:00+00:00", action="request_change", confirmed_date=None,
            proposed_date="2026-06-15", prior_confirmed_date="2026-06-20"))


def _cid(conn, key):
    return conn.execute("SELECT id FROM commitments WHERE business_key=?", (key,)).fetchone()[0]


class TestCommitmentProvenance:
    def test_audit_counterexample_order_a_date_is_not_historical_because_of_order_b(self) -> None:
        conn = _orders()
        labelled, _ = label_memories(
            [_obs("ORD-777 delivery is confirmed for 2026-06-10."),
             _obs("ORD-501 delivery is confirmed for 2026-06-15."),
             _obs("ORD-888 delivery is confirmed for 2026-06-15.")],
            customer_state(conn, ALICE), f"ent:{ALICE}")
        assert [(m.label, m.commitment_id) for m in labelled] == [
            (CONSISTENT, _cid(conn, "ORD-777")),         # was 'historical' (ORD-501 once 06-10)
            (CONSISTENT, _cid(conn, "ORD-501")),         # was 'unconfirmed_request' (ORD-888)
            (UNCONFIRMED_REQUEST, _cid(conn, "ORD-888"))]

    def test_signal_provenance_links_without_any_key_in_the_text(self) -> None:
        conn = _orders()
        links = {"sig:c:a1": (_cid(conn, "ORD-501"), 1), "sig:c:a2": (_cid(conn, "ORD-501"), 2),
                 "sig:c:b1": (_cid(conn, "ORD-777"), 1), "sig:c:c2": (_cid(conn, "ORD-888"), 2)}
        facts = [
            {"type": "world", "text": "Delivery confirmed for 2026-06-10.", "document_id": "sig:c:a1"},
            {"type": "world", "text": "Delivery moved from 2026-06-10 to 2026-06-15.",
             "document_id": "sig:c:a2"},
            {"type": "world", "text": "Delivery confirmed for 2026-06-10.", "document_id": "sig:c:b1"},
            {"type": "world", "text": "Customer asked to move delivery to 2026-06-15.",
             "document_id": "sig:c:c2"}]
        labelled, _ = label_memories(facts, customer_state(conn, ALICE), f"ent:{ALICE}", links=links)
        assert [(m.label, m.link) for m in labelled] == [
            (HISTORICAL, "signal_provenance"),           # superseded version of ORD-501
            (CONSISTENT, "signal_provenance"),           # the change to ORD-501's current version
            (CONSISTENT, "signal_provenance"),           # same text, but ORD-777: current
            (UNCONFIRMED_REQUEST, "signal_provenance")]  # ORD-888's pending request

    def test_observation_sources_link_or_stay_uncertain(self) -> None:
        conn = _orders()
        links = {"sig:c:a2": (_cid(conn, "ORD-501"), 2), "sig:c:b1": (_cid(conn, "ORD-777"), 1)}
        obs = [dict(_obs("The delivery is confirmed for 2026-06-10."), source_fact_ids=["f1"]),
               dict(_obs("Deliveries on 2026-06-10 and 2026-06-15."), source_fact_ids=["f1", "f2"]),
               dict(_obs("Something about 2026-06-10."), source_fact_ids=["f3"]),
               _obs("A delivery is confirmed for 2026-06-10.")]
        labelled, _ = label_memories(obs, customer_state(conn, ALICE), f"ent:{ALICE}", links=links,
                                     source_docs={"f1": "sig:c:b1", "f2": "sig:c:a2", "f3": "sig:c:zz"})
        assert labelled[0].label == CONSISTENT and labelled[0].commitment_id == _cid(conn, "ORD-777")
        assert labelled[1].label == UNCERTAIN and "links to 2 commitments" in labelled[1].notes[-1]
        assert labelled[2].label == UNCERTAIN                    # source not linked
        assert labelled[3].label == UNCERTAIN and labelled[3].commitment_id is None   # unlinked
        assert not any(m.authoritative for m in labelled)

    def test_same_date_orders_of_two_customers_do_not_mix(self) -> None:
        conn = _ledger(COMMIT, CONFIRM,
                       _ev(contact_id=BOB, business_key="ORD-501", source_doc_id="gmail:z1",
                           thread_id="tz", confirmed_date="2026-06-10"))
        [a], _ = label_memories([_obs("ORD-501 is confirmed for 2026-06-10.")],
                                customer_state(conn, ALICE), f"ent:{ALICE}")
        [b], _ = label_memories([_obs("ORD-501 is confirmed for 2026-06-10.", ent=BOB)],
                                customer_state(conn, BOB), f"ent:{BOB}")
        assert (a.label, b.label) == (HISTORICAL, CONSISTENT)

    def test_a_date_the_ledger_never_held_is_uncertain_not_consistent(self) -> None:
        conn = _orders()
        [m], _ = label_memories([_obs("ORD-501 delivery is confirmed for 2026-06-22.")],
                                customer_state(conn, ALICE), f"ent:{ALICE}")
        assert m.label == UNCERTAIN and "not held by the ledger" in m.notes[0]

    def test_text_naming_another_commitment_is_uncertain(self) -> None:
        conn = _orders()
        links = {"sig:c:b1": (_cid(conn, "ORD-777"), 1)}
        [m], _ = label_memories([{"type": "world", "text": "ORD-501 confirmed for 2026-06-10.",
                                  "document_id": "sig:c:b1"}],
                                customer_state(conn, ALICE), f"ent:{ALICE}", links=links)
        assert m.label == UNCERTAIN and "another commitment" in m.notes[-1]


# ── The enforced interface for Hermes tools ───────────────────────────────────

class _Client:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def recall(self, query, **kw):
        self.calls.append(kw)
        return self.reply


class TestEnforcedInterface:
    def test_customer_context_is_tag_scoped_with_sources_and_ledger_first(self) -> None:
        conn = _orders()
        client = _Client({"results": [_obs("ORD-501 delivery is confirmed for 2026-06-10.")]})
        ctx = customer_context(conn, client, "olo-b", ALICE, "When is ORD-501?")
        assert client.calls[0]["tags"] == [f"ent:{ALICE}"]
        assert client.calls[0]["tags_match"] == "all_strict"
        assert client.calls[0]["include_source_facts"] is True
        truth = {s.business_key: s.current_date for s in current_truth(ctx)}
        assert truth == {"ORD-501": "2026-06-15", "ORD-777": "2026-06-10", "ORD-888": "2026-06-20"}
        assert ctx.memories[0].label == HISTORICAL and not ctx.memories[0].authoritative

    @pytest.mark.parametrize("raw", [
        {"results": [_obs("ORD-501 delivery is confirmed for 2026-06-10.")]},   # client.recall reply
        [_obs("ORD-501 delivery is confirmed for 2026-06-10.")],                # its results list
        {"current": [{"business_key": "ORD-501", "current_date": "2026-06-10"}]},  # look-alike dict
    ])
    def test_raw_hindsight_recall_is_never_accepted_as_current_truth(self, raw) -> None:
        with pytest.raises(TypeError, match="never authoritative"):
            current_truth(raw)

    def test_view_dict_is_not_truth_either(self) -> None:
        ctx = customer_context(_orders(), _Client({"results": []}), "olo-b", ALICE, "q")
        with pytest.raises(TypeError):
            current_truth(ctx.as_dict())
        assert current_truth(ctx) == ctx.current

    def test_observation_source_facts_from_recall_are_used(self) -> None:
        conn = _orders()
        reply = {"results": [dict(_obs("The delivery is confirmed for 2026-06-10."),
                                  source_fact_ids=["f1"])],
                 "source_facts": {"f1": {"document_id": f"ledger:{_cid(conn, 'ORD-777')}:v1"}}}
        ctx = customer_context(conn, _Client(reply), "olo-b", ALICE, "q")
        assert ctx.memories[0].link == "observation_sources"
        assert ctx.memories[0].commitment_id == _cid(conn, "ORD-777")
        assert ctx.memories[0].label == CONSISTENT


# ── Projection: authored prose, strategies, cancellation ──────────────────────

QUOTED_REPLY = (REQUEST_BODY + "\n\nThanks,\nAlice\n\nOn Mon, 1 Jun 2026, Owner wrote:\n"
                "> Your order ORD-1 is confirmed for delivery on 2024-04-10.")


class TestProjectionCorrections:
    def test_quoted_history_is_not_retained_and_the_original_is_kept(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1),
              content("m2", sent=False, body=QUOTED_REPLY, day=2))
        doc = p.hindsight.docs(p.bank)["sig:c:m2"]
        assert "Owner wrote" not in doc["content"] and "> Your order" not in doc["content"]
        assert "Could we move order ORD-1" in doc["content"]
        assert doc["metadata"]["text_scope"] == "authored"
        assert doc["metadata"]["quoted_history"] == "omitted"
        conn = connect(p.db)
        payload = conn.execute("SELECT payload FROM signals WHERE external_id='c:m2'").fetchone()[0]
        import json
        loaded = load_content(json.loads(payload), lambda sha: get_blob_raw(
            "gmail_biz", sha, conn))
        conn.close()
        assert "> Your order ORD-1 is confirmed" in loaded.rendered      # evidence intact
        assert doc["metadata"]["content_sha256"] == loaded.sha256
        assert "quoted_history" not in p.hindsight.docs(p.bank)["sig:c:m1"]["metadata"]

    def test_confirmation_reprojects_the_superseded_ledger_document(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1),
              content("m2", sent=False, body=REQUEST_BODY, day=2))
        [cid] = p.commitments()
        assert LEDGER_CURRENT_TAG in p.hindsight.docs(p.bank)[f"ledger:{cid}:v1"]["tags"]
        p.run(content("m3", sent=True, body=CONFIRM_BODY, day=3))
        docs = p.hindsight.docs(p.bank)
        assert LEDGER_SUPERSEDED_TAG in docs[f"ledger:{cid}:v1"]["tags"]
        assert LEDGER_CURRENT_TAG in docs[f"ledger:{cid}:v3"]["tags"]
        assert p.hindsight.unknown_strategy_retains == []

    def test_every_submitted_strategy_is_registered(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
        assert {i["strategy"] for i in p.hindsight.retained[p.bank]} == {"prose", "structured-no-llm"}
        assert p.hindsight.unknown_strategy_retains == []

    def test_an_unregistered_strategy_is_never_submitted(self, tmp_path, monkeypatch) -> None:
        from olo import projection
        real = projection.olo_strategies
        monkeypatch.setattr(projection, "olo_strategies",
                            lambda: {k: v for k, v in real().items() if k != "prose"})
        p = Pipeline(tmp_path)
        r = p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
        assert r.projection["unregistered_strategy"] == 1
        assert all(i["strategy"] != "prose" for i in p.hindsight.retained[p.bank])
        assert p.hindsight.unknown_strategy_retains == []

    def test_cancellation_reports_documents_it_did_not_roll_back(self, tmp_path) -> None:
        clock = Clock()
        p = Pipeline(tmp_path)
        p.hindsight.next_status = "processing"
        p.run(content("m1", sent=True, body="Thanks for the call today.", day=1), clock=clock)
        clock.advance(minutes=61)
        r = p.run(clock=clock)
        assert r.projection["cancelled"] == 1
        assert r.projection["cancelled_persisted"] == 1          # the fake kept the document
        conn = connect(p.db)
        detail = conn.execute(
            "SELECT detail FROM hindsight_operations WHERE status='cancelled'").fetchone()[0]
        conn.close()
        assert "not a rollback" in detail and "sig:c:m1" in detail
        assert "cancelled" in _ops(p).values()

    def test_cancellation_reports_absent_documents_as_absent(self, tmp_path) -> None:
        clock = Clock()
        p = Pipeline(tmp_path)
        p.hindsight.next_status = "processing"
        p.run(content("m1", sent=True, body="Thanks for the call today.", day=1), clock=clock)
        p.hindsight.retained[p.bank] = []                        # nothing reached the server
        clock.advance(minutes=61)
        r = p.run(clock=clock)
        assert r.projection["cancelled_persisted"] == 0
        conn = connect(p.db)
        detail = conn.execute(
            "SELECT detail FROM hindsight_operations WHERE status='cancelled'").fetchone()[0]
        conn.close()
        assert "persisted on server none" in detail and "absent ['sig:c:m1']" in detail
