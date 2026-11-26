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
    CURRENT_LEDGER,
    HISTORICAL,
    MEMORY,
    SUPERSEDED_LEDGER,
    UNCONFIRMED_REQUEST,
    customer_state,
    dates_mentioned,
    label_memories,
    recall_for_customer,
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
        assert m["label"] == UNCONFIRMED_REQUEST
        assert any("not confirmed by the business" in n for n in m["notes"])

    def test_after_confirmation_the_live_stale_observation_is_labelled_historical(self) -> None:
        conn = _ledger(COMMIT, REQUEST, CONFIRM)
        stale = ("The ORD-501 delivery is confirmed for 2026-06-10. (2026-06-03 09:00: Order "
                 "ORD-501 delivery was rescheduled from 2026-06-10 to 2026-06-15.)")
        [m], _ = label_memories([_obs(stale)], customer_state(conn, ALICE), f"ent:{ALICE}")
        assert m["label"] == HISTORICAL
        assert any("2026-06-10 is no longer in force" in n and "current is scheduled, 2026-06-15" in n
                   for n in m["notes"])

    def test_ledger_documents_are_labelled_by_version_from_the_ledger(self) -> None:
        conn = _ledger(COMMIT, REQUEST, CONFIRM)
        cid = conn.execute("SELECT id FROM commitments").fetchone()[0]
        results = [{"type": "world", "text": "Status as of version 1: scheduled, date 2026-06-10.",
                    "document_id": f"ledger:{cid}:v1", "tags": [f"ent:{ALICE}"]},
                   {"type": "world", "text": "Status as of version 3: scheduled, date 2026-06-15.",
                    "document_id": f"ledger:{cid}:v3", "tags": [f"ent:{ALICE}"]}]
        labelled, _ = label_memories(results, customer_state(conn, ALICE), f"ent:{ALICE}")
        assert [m["label"] for m in labelled] == [SUPERSEDED_LEDGER, CURRENT_LEDGER]

    def test_another_customers_memory_is_excluded(self) -> None:
        conn = _ledger(COMMIT)
        labelled, excluded = label_memories(
            [_obs("Bob's order is due 2026-06-10.", ent=BOB), _obs("Thanks for the call.")],
            customer_state(conn, ALICE), f"ent:{ALICE}")
        assert [m["text"] for m in labelled] == ["Thanks for the call."]
        assert labelled[0]["label"] == MEMORY and len(excluded) == 1

    def test_recall_for_customer_is_tag_scoped_and_leads_with_ledger_state(self) -> None:
        conn = _ledger(COMMIT, REQUEST, CONFIRM)
        calls = []

        class Client:
            def recall(self, query, **kw):
                calls.append(kw)
                return {"results": [_obs("The ORD-501 delivery is confirmed for 2026-06-10.")]}

        view = recall_for_customer(conn, Client(), "olo-b", ALICE, "When is ORD-501?")
        assert calls[0]["tags"] == [f"ent:{ALICE}"] and calls[0]["tags_match"] == "all_strict"
        assert view["current"][0]["current_date"] == "2026-06-15"
        assert view["memories"][0]["label"] == HISTORICAL

    def test_month_day_dates_resolve_in_the_ledgers_years(self) -> None:
        assert dates_mentioned("on 15th of June and Jun 10, or 2026-06-05", {2026}) == {
            "2026-06-15", "2026-06-10", "2026-06-05"}


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
