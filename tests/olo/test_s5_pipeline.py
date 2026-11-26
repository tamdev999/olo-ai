"""tests/olo/test_s5_pipeline.py — S5 part 2: worker → ledger → Hindsight, offline.

Signals are produced by the real Gmail adapters (``gmail_full`` content and
``gmail`` metadata) from Gmail API message shapes and ingested by the real
worker (run_source).  The extraction model and the Hindsight client are
mocks; nothing touches the network.
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from olo.adapters import FetchBatch
from olo.adapters.gmail import GmailAdapter
from olo.adapters.gmail_full import build_email_content_signal
from olo.commitments import CommitmentStageConfig
from olo.ledger import STATUS_CANCELLED, STATUS_SCHEDULED, get_all_versions, get_current_state
from olo.projection import ProjectionConfig, ProjectionLimits
from olo.runner import run_source
from olo.store import connect

pytestmark = pytest.mark.not_integration

OWNER = "owner@biz.example"
SOURCE = "gmail_biz"
ALICE = "Alice <alice@x.com>"


# ── Gmail-shaped signals ──────────────────────────────────────────────────────

def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _raw(msg_id: str, *, sent: bool, body: str, day: int, frm: str = OWNER,
         to: str = ALICE, subject: str = "Order", thread: str = "t1") -> dict:
    if not sent and frm == OWNER:
        frm, to = to, OWNER
    return {
        "id": msg_id, "threadId": thread, "historyId": "1",
        "labelIds": ["SENT"] if sent else ["INBOX"],
        "internalDate": str(int(datetime(2024, 3, day, 9, tzinfo=UTC).timestamp() * 1000)),
        "sizeEstimate": 100,
        "payload": {
            "mimeType": "multipart/alternative", "filename": "", "body": {"size": 0},
            "headers": [{"name": "From", "value": frm}, {"name": "To", "value": to},
                        {"name": "Subject", "value": subject},
                        {"name": "Message-ID", "value": f"<{msg_id}@m>"}],
            "parts": [{"partId": "0", "mimeType": "text/plain", "filename": "",
                       "body": {"size": len(body), "data": _b64(body)}}],
        },
    }


def content(msg_id: str, **kw):
    return build_email_content_signal(SOURCE, _raw(msg_id, **kw), "1")


def metadata(msg_id: str, **kw):
    return GmailAdapter({"account": "biz"})._message_to_signal(_raw(msg_id, **kw), "1")


class Adapter:
    """Serves the queued signals once per run."""

    def __init__(self) -> None:
        self.source = SOURCE
        self.queue: list = []
        self.n = 0

    def fetch(self, cursor, *, limit):
        batch, self.queue = self.queue, []
        self.n += 1
        return FetchBatch(signals=batch, has_more=False, next_cursor=f"c{self.n}")


# ── Mock model and Hindsight ──────────────────────────────────────────────────

def answer(action: str, passage: str, *, event_type="delivery", key=None, confirmed=None,
           proposed=None, prior=None, confidence=0.9) -> str:
    return json.dumps({
        "event_type": event_type, "action": action, "business_key": key,
        "confirmed_date": confirmed, "proposed_date": proposed,
        "prior_confirmed_date": prior, "supporting_passage": passage,
        "confidence": confidence,
    })


NOTHING = json.dumps({"event_type": None, "action": None, "business_key": None,
                      "confirmed_date": None, "proposed_date": None,
                      "prior_confirmed_date": None, "supporting_passage": None,
                      "confidence": 0.0})


class Model:
    """Answers by the first registered body fragment found in the prompt."""

    model_id = "mock-model"
    max_output_tokens = 200

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.fail: set[str] = set()

    def __call__(self, prompt: str):
        for fragment, reply in self.routes.items():
            if fragment in prompt:
                self.calls.append(fragment)
                if fragment in self.fail:
                    raise ConnectionError("model unavailable")
                return reply, 50
        self.calls.append("<none>")
        return NOTHING, 20


class Hindsight:
    """Records retains per bank; operations complete unless told otherwise.

    Bank config follows Hindsight 1.2.1: GET /config on an unknown bank is a
    404, PUT creates it, and PATCH replaces each top-level key it is given.
    A completed prose retain leaves its memories pending consolidation until
    a consolidation completes (auto-consolidation off).  As Hindsight 1.2.1:
    a retain naming an unregistered strategy is accepted (recorded in
    ``unknown_strategy_retains``), and cancellation is cooperative — documents
    already written stay (``get_document`` still finds them).
    """

    def __init__(self, *, raise_on_retain: bool = False) -> None:
        self.retained: dict[str, list[dict]] = {}
        self.ops: dict[str, str] = {}
        self.raise_on_retain = raise_on_retain
        self.next_status = "completed"      # status new operations report
        self.cancelled: list[str] = []
        self.submits: list[tuple[str, int]] = []
        self.banks: dict[str, dict] = {}    # bank → bank-level config overrides
        self.global_strategies: dict = {}   # server-level retain_strategies
        self.patches: list[tuple[str, dict]] = []
        self.config_error: Exception | None = None
        self.unprepared_retains: list[str] = []
        self.pending: dict[str, int] = {}   # bank → memories pending consolidation
        self.consolidations: list[tuple[str, str]] = []   # (bank, operation id)
        self.scopes: list = []              # observation_scopes of each trigger
        self.server_consolidations: list[tuple[str, str]] = []   # unrecorded, server-side
        self.consolidation_status = "completed"
        self.consolidation_drains = True    # False: completes without consolidating
        self.stats_error = False
        self.trigger_error = False
        self.drop_strategies: set[str] = set()   # PATCHed strategies the server ignores
        self.unknown_strategy_retains: list[tuple[str, str]] = []   # (bank, strategy)

    # Bank configuration
    def get_bank_config(self, bank_id=None):
        from olo.m3.hindsight.client import HindsightAPIError
        if self.config_error is not None:
            raise self.config_error
        if bank_id not in self.banks:
            raise HindsightAPIError("bank not found", status_code=404)
        overrides = self.banks[bank_id]
        resolved = {"retain_extraction_mode": "concise", "enable_auto_consolidation": True,
                    "retain_strategies": self.global_strategies, **overrides}
        return {"bank_id": bank_id, "config": resolved, "overrides": dict(overrides)}

    def ensure_bank(self, bank_id):
        self.banks.setdefault(bank_id, {})
        return {"bank_id": bank_id}

    def patch_bank_config(self, updates, bank_id=None):
        import copy
        self.patches.append((bank_id, copy.deepcopy(updates)))
        updates = copy.deepcopy(updates)
        for name in self.drop_strategies:
            (updates.get("retain_strategies") or {}).pop(name, None)
        self.banks[bank_id].update(updates)
        return self.get_bank_config(bank_id=bank_id)

    def _prepared(self, bank_id) -> bool:
        if bank_id not in self.banks:
            return False
        strategies = self.get_bank_config(bank_id=bank_id)["config"]["retain_strategies"]
        return (strategies.get("structured-no-llm") or {}).get("enable_observations") is False

    def retain(self, items, *, operation_id, async_, bank_id):
        from olo.m3.hindsight.client import HindsightAPIError
        if self.raise_on_retain:
            raise ConnectionError("hindsight down")
        if len({i.get("strategy") for i in items}) > 1:     # as Hindsight 1.2.1
            raise HindsightAPIError(
                "operation_id requires all retain items to resolve to a single strategy",
                status_code=400)
        if not self._prepared(bank_id):
            self.unprepared_retains.append(bank_id)
        strategies = self.get_bank_config(bank_id=bank_id)["config"]["retain_strategies"]
        for strategy in {i.get("strategy") for i in items}:
            if strategy and strategy not in strategies:
                self.unknown_strategy_retains.append((bank_id, strategy))
        self.retained.setdefault(bank_id, []).extend(items)
        self.ops[operation_id] = self.next_status
        self.submits.append((bank_id, len(items)))
        if self.next_status == "completed":
            prose = sum(1 for i in items if "olo:prose" in i.get("tags", []))
            self.pending[bank_id] = self.pending.get(bank_id, 0) + prose
        return {"operation_id": operation_id}

    # Consolidation
    def count_memories(self, *, consolidation_state, tags, bank_id=None):
        if self.stats_error:
            raise ConnectionError("stats down")
        assert tags == ["olo:prose"]
        return self.pending.get(bank_id, 0) if consolidation_state == "pending" else 0

    def list_operations(self, limit=10, status=None, bank_id=None):
        ops = [{"id": op, "task_type": "consolidation", "status": self.ops[op]}
               for bank, op in self.server_consolidations if bank == bank_id]
        return {"operations": [o for o in ops if o["status"] == status][:limit]}

    def trigger_consolidation(self, observation_scopes=None, bank_id=None):
        if self.trigger_error:
            raise ConnectionError("hindsight down")
        op = f"consolidation-{len(self.consolidations) + 1}"
        self.consolidations.append((bank_id, op))
        self.scopes.append(observation_scopes)
        self.ops[op] = self.consolidation_status
        if self.consolidation_status == "completed" and self.consolidation_drains:
            self.pending[bank_id] = 0
        return {"operation_id": op, "deduplicated": False}

    def get_operation_status(self, operation_id, bank_id=None):
        return {"status": self.ops.get(operation_id, "not_found")}

    def cancel_operation(self, operation_id, bank_id=None):
        self.ops[operation_id] = "cancelled"
        self.cancelled.append(operation_id)
        return {"status": "cancelled"}

    def get_document(self, document_id, bank_id=None):
        from olo.m3.hindsight.client import HindsightAPIError
        if document_id not in self.docs(bank_id):
            raise HindsightAPIError("document not found", status_code=404)
        return {"id": document_id, "memory_unit_count": 1}

    def docs(self, bank: str) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        for item in self.retained.get(bank, []):
            latest[item["document_id"]] = item
        return latest


class Guard:
    """UsageGuard stand-in: server-reported spend is driven by the test.

    ``hours`` is what the server's trace still holds per hour bucket — a test
    deletes entries to model Hindsight's one-day retention sweep.
    """

    budget_usd = 8.0

    def __init__(self) -> None:
        self.exceeded = False
        self.unavailable = False
        self.snapshots = 0
        self.hours: dict[str, tuple[int, int]] = {}

    def hourly_usage(self):
        from olo.m3.hindsight.budget import StatsUnavailableError, TokenSnapshot
        if self.unavailable:
            raise StatsUnavailableError("stats endpoint down")
        self.snapshots += 1
        hours = dict(self.hours)
        if self.exceeded:
            hours["2999-01-01T00:00:00Z"] = (0, 10_000_000)     # $12 at output price
        return [(h, TokenSnapshot(i, o)) for h, (i, o) in hours.items()]

    def snapshot(self):
        from olo.m3.hindsight.budget import StatsUnavailableError, TokenSnapshot
        if self.unavailable:
            raise StatsUnavailableError("stats endpoint down")
        self.snapshots += 1
        return TokenSnapshot(1000, 100)

    def check(self, baseline):
        from olo.m3.hindsight.budget import BudgetExceededError, StatsUnavailableError
        if self.unavailable:
            raise StatsUnavailableError("stats endpoint down")
        if self.exceeded:
            raise BudgetExceededError(9.0, 8.0, 1, 1)
        return baseline


# ── Harness ───────────────────────────────────────────────────────────────────

COMMIT_BODY = "Your order ORD-1 is confirmed for delivery on 2024-04-10."
REQUEST_BODY = "Could we move order ORD-1 from 2024-04-10 to 2024-04-15?"
CONFIRM_BODY = "Confirmed: order ORD-1 moved from 2024-04-10 to 2024-04-15."
CANCEL_BODY = "Please cancel order ORD-1 scheduled for 2024-04-10."

ROUTES = {
    COMMIT_BODY: answer("commit", "Your order ORD-1 is confirmed for delivery on 2024-04-10",
                        key="ORD-1", confirmed="2024-04-10"),
    REQUEST_BODY: answer("request_change", REQUEST_BODY.rstrip("?"), key="ORD-1",
                         proposed="2024-04-15", prior="2024-04-10"),
    CONFIRM_BODY: answer("confirm_change", CONFIRM_BODY.rstrip("."), key="ORD-1",
                         confirmed="2024-04-15", prior="2024-04-10"),
    CANCEL_BODY: answer("cancel", CANCEL_BODY.rstrip("."), key="ORD-1", prior="2024-04-10"),
}


class Pipeline:
    def __init__(self, tmp_path: Path, name: str = "biz", routes=None) -> None:
        self.db = tmp_path / f"{name}.db"
        self.adapter = Adapter()
        self.model = Model(dict(routes or ROUTES))
        self.hindsight = Hindsight()
        self.guard = Guard()
        self.limits = ProjectionLimits()
        self.bank = f"olo-{name}"

    def run(self, *signals, commitment=True, projection=True, **kw):
        self.adapter.queue.extend(signals)
        return run_source(
            self.adapter, db_path=self.db,
            commitment=CommitmentStageConfig(model_fn=self.model) if commitment else None,
            projection=(ProjectionConfig(client=self.hindsight, bank_id=self.bank,
                                         usage_guard=self.guard, limits=self.limits,
                                         await_commitment=commitment)
                        if projection else None),
            **kw,
        )

    def resolution(self) -> dict[str, dict]:
        conn = connect(self.db)
        rows = conn.execute("SELECT * FROM signal_resolution").fetchall()
        conn.close()
        return {r["signal_id"]: dict(r) for r in rows}

    def commitments(self) -> list[str]:
        conn = connect(self.db)
        ids = [r[0] for r in conn.execute("SELECT id FROM commitments ORDER BY created_at")]
        conn.close()
        return ids

    def state(self, cid: str) -> dict:
        conn = connect(self.db)
        st = get_current_state(conn, cid)
        conn.close()
        return st

    def versions(self, cid: str) -> list[dict]:
        conn = connect(self.db)
        vs = get_all_versions(conn, cid)
        conn.close()
        return vs


# ── Worker → ledger ───────────────────────────────────────────────────────────

class TestLedgerFlow:
    def test_commit_request_confirm(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        r = p.run(content("m1", sent=True, body=COMMIT_BODY, day=1),
                  content("m2", sent=False, body=REQUEST_BODY, day=2),
                  content("m3", sent=True, body=CONFIRM_BODY, day=3))
        assert r.status == "succeeded" and r.commitment == {"applied": 3}
        [cid] = p.commitments()
        st = p.state(cid)
        assert st["status"] == STATUS_SCHEDULED and st["scheduled_date"] == "2024-04-15"
        assert [v["is_confirmed"] for v in p.versions(cid)] == [True, False, True]
        res = p.resolution()
        assert {res[s]["commitment_outcome"] for s in ("c:m1", "c:m2", "c:m3")} == {"applied"}
        assert all(res[s]["commitment_id"] == cid for s in ("c:m1", "c:m2", "c:m3"))
        conn = connect(p.db)
        contact = conn.execute("SELECT contact_id FROM commitments").fetchone()[0]
        conn.close()
        assert contact == res["c:m1"]["contact_id"] and contact.startswith("cid_")

    def test_customer_cancellation_stays_pending(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1),
              content("m2", sent=False, body=CANCEL_BODY, day=2))
        [cid] = p.commitments()
        st = p.state(cid)
        assert st["status"] == STATUS_SCHEDULED and st["pending_action"] == "cancel"

    def test_business_cancellation_cancels(self, tmp_path) -> None:
        body = "We have cancelled order ORD-1 previously scheduled for 2024-04-10."
        routes = dict(ROUTES)
        routes[body] = answer("cancel", body.rstrip("."), key="ORD-1", prior="2024-04-10")
        p = Pipeline(tmp_path, routes=routes)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1),
              content("m2", sent=True, body=body, day=2))
        [cid] = p.commitments()
        assert p.state(cid)["status"] == STATUS_CANCELLED

    def test_multiple_deliveries_stay_separate(self, tmp_path) -> None:
        b2 = "Your order ORD-2 is confirmed for delivery on 2024-05-01."
        ch2 = "Confirmed: order ORD-2 moved from 2024-05-01 to 2024-05-03."
        routes = dict(ROUTES)
        routes[b2] = answer("commit", b2.rstrip("."), key="ORD-2", confirmed="2024-05-01")
        routes[ch2] = answer("confirm_change", ch2.rstrip("."), key="ORD-2",
                             confirmed="2024-05-03", prior="2024-05-01")
        p = Pipeline(tmp_path, routes=routes)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1),
              content("m2", sent=True, body=b2, day=2, thread="t2"),
              content("m3", sent=True, body=ch2, day=3, thread="t2"))
        dates = {p.state(c)["business_key"]: p.state(c)["scheduled_date"] for c in p.commitments()}
        assert dates == {"ORD-1": "2024-04-10", "ORD-2": "2024-05-03"}

    def test_outcomes_are_distinguished_and_persisted(self, tmp_path) -> None:
        unsupported = "Delivery for order ORD-9 is booked."
        unknown = "Please move order ORD-7 from 2024-06-01 to 2024-06-02."
        chat = "Thanks for the lovely lunch yesterday."
        routes = dict(ROUTES)
        routes[unsupported] = answer("commit", unsupported.rstrip("."), key="ORD-9",
                                     confirmed="2024-07-04")
        routes[unknown] = answer("request_change", unknown.rstrip("."), key="ORD-7",
                                 proposed="2024-06-02", prior="2024-06-01")
        routes[chat] = NOTHING
        p = Pipeline(tmp_path, routes=routes)
        r = p.run(content("m1", sent=True, body=unsupported, day=1),
                  content("m2", sent=False, body=unknown, day=2),
                  content("m3", sent=False, body=chat, day=3))
        assert r.commitment == {"rejected": 1, "ledger_unresolved": 1, "no_commitment": 1}
        res = p.resolution()
        assert (res["c:m1"]["commitment_status"], res["c:m1"]["commitment_outcome"]) == \
            ("unresolved", "rejected")
        assert "not stated" in res["c:m1"]["commitment_reason"]
        assert (res["c:m2"]["commitment_status"], res["c:m2"]["commitment_outcome"]) == \
            ("unresolved", "ledger_unresolved")
        assert (res["c:m3"]["commitment_status"], res["c:m3"]["commitment_outcome"]) == \
            ("resolved", "no_commitment")
        assert p.commitments() == []

    def test_ambiguous_identity_is_never_extracted(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1,
                      to="alice@x.com, bob@y.com"))
        res = p.resolution()["c:m1"]
        assert res["identity_status"] == "ambiguous"
        assert res["commitment_status"] == "pending"
        assert p.model.calls == [] and p.commitments() == []


# ── One authoritative representation per Gmail message ────────────────────────

class TestDuplicateRepresentations:
    def test_metadata_copy_is_superseded_by_content(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        r = p.run(metadata("m1", sent=True, body=COMMIT_BODY, day=1,
                           subject="Order ORD-1 confirmed for 2024-04-10"),
                  content("m1", sent=True, body=COMMIT_BODY, day=1))
        assert r.commitment == {"applied": 1, "superseded": 1}
        [cid] = p.commitments()
        assert len(p.versions(cid)) == 1
        assert p.resolution()["m1"]["commitment_status"] == "skipped"
        assert "sig:m1" not in p.hindsight.docs(p.bank)     # not projected twice
        assert "sig:c:m1" in p.hindsight.docs(p.bank)

    def test_metadata_waits_until_its_content_arrives(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.run(content("m0", sent=True, body=COMMIT_BODY, day=1))     # content enabled
        r = p.run(metadata("m5", sent=True, body="x", day=2, subject="Reminder"))
        assert r.commitment == {"awaiting_content": 1}
        assert p.resolution()["m5"]["commitment_status"] == "pending"
        r2 = p.run(content("m5", sent=True, body="Just a reminder about our call.", day=2))
        assert r2.commitment == {"no_commitment": 1, "superseded": 1}

    def test_same_message_extracted_twice_adds_one_version(self, tmp_path) -> None:
        """Even if both copies were authoritative, the ledger sees one document."""
        from olo.commitments import message_doc_id
        meta = metadata("m1", sent=True, body="", day=1)
        cont = content("m1", sent=True, body="", day=1)
        assert message_doc_id(meta.kind, meta.payload, SOURCE, meta.external_id) == \
            message_doc_id(cont.kind, cont.payload, SOURCE, cont.external_id) == "gmail:m1"

    def test_metadata_only_business_extracts_from_subject(self, tmp_path) -> None:
        subject = "Order ORD-1 confirmed for 2024-04-10"
        routes = {subject: answer("commit", subject, key="ORD-1", confirmed="2024-04-10")}
        p = Pipeline(tmp_path, routes=routes)
        r = p.run(metadata("m1", sent=True, body="", day=1, subject=subject))
        assert r.commitment == {"applied": 1}


# ── Failures, retries, crash and restart ──────────────────────────────────────

class TestRecovery:
    def test_model_failure_is_retried_without_duplicates(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.model.fail.add(COMMIT_BODY)
        r1 = p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
        assert r1.commitment == {"model_error": 1}
        res = p.resolution()["c:m1"]
        assert (res["commitment_status"], res["commitment_attempts"]) == ("failed", 1)
        assert p.commitments() == []
        assert "sig:c:m1" not in p.hindsight.docs(p.bank)   # not projected while failed

        p.model.fail.clear()
        r2 = p.run()
        assert r2.commitment == {"applied": 1}
        [cid] = p.commitments()
        assert len(p.versions(cid)) == 1
        assert p.resolution()["c:m1"]["commitment_attempts"] == 2

    def test_retries_stop_at_max_attempts(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.model.fail.add(COMMIT_BODY)
        for _ in range(7):
            p.run()  if p.adapter.n else p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
        assert p.resolution()["c:m1"]["commitment_attempts"] == 5
        assert p.model.calls.count(COMMIT_BODY) == 5

    def test_crash_before_commit_rolls_back_and_resumes(self, tmp_path) -> None:
        p = Pipeline(tmp_path)

        def crash(name):
            if name == "before_commitment_commit":
                raise RuntimeError("power cut")
        with pytest.raises(RuntimeError):
            p.run(content("m1", sent=True, body=COMMIT_BODY, day=1), _fault=crash)
        assert p.commitments() == []                         # ledger write rolled back
        assert p.resolution()["c:m1"]["commitment_status"] == "pending"

        r = p.run()
        assert r.commitment == {"applied": 1}
        assert p.model.calls.count(COMMIT_BODY) == 1         # second pass hit the cache
        [cid] = p.commitments()
        assert len(p.versions(cid)) == 1

    def test_crash_after_projection_enqueue_resubmits(self, tmp_path) -> None:
        p = Pipeline(tmp_path)

        def crash(name):
            if name == "after_projection_enqueue":
                raise RuntimeError("power cut")
        with pytest.raises(RuntimeError):
            p.run(content("m1", sent=True, body=COMMIT_BODY, day=1), _fault=crash)
        assert p.hindsight.retained == {}
        r = p.run()
        assert r.projection["submitted"] >= 2 and "sig:c:m1" in p.hindsight.docs(p.bank)
        conn = connect(p.db)
        statuses = {r[0] for r in conn.execute("SELECT status FROM hindsight_projection")}
        conn.close()
        assert statuses == {"confirmed"}

    def test_hindsight_outage_is_retried(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.raise_on_retain = True
        r1 = p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
        assert r1.status == "succeeded" and r1.projection["failed"] >= 1
        p.hindsight.raise_on_retain = False
        r2 = p.run()
        assert r2.projection["confirmed"] >= 2
        r3 = p.run()
        assert r3.projection["submitted"] == 0               # idempotent once confirmed


# ── Projection content ────────────────────────────────────────────────────────

class TestProjection:
    def test_signal_and_ledger_state_documents(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1),
              content("m2", sent=False, body=REQUEST_BODY, day=2),
              content("m3", sent=True, body=CONFIRM_BODY, day=3))
        [cid] = p.commitments()
        docs = p.hindsight.docs(p.bank)
        contact = p.resolution()["c:m1"]["contact_id"]

        sig = docs["sig:c:m1"]
        assert sig["entities"] == [{"text": contact, "type": "person"}]
        assert sig["metadata"]["commitment_id"] == cid
        assert sig["observation_scopes"] == [[f"ent:{contact}", "ym:*"], [f"ent:{contact}"]]
        assert sig["content"].startswith("Subject: Order\n\nYour order ORD-1")
        assert sig["strategy"] == "prose"

        assert set(d for d in docs if d.startswith("ledger:")) == {
            f"ledger:{cid}:v1", f"ledger:{cid}:v3"}          # pending v2 is not a state
        v3 = docs[f"ledger:{cid}:v3"]
        assert v3["metadata"]["status"] == "scheduled"
        assert v3["metadata"]["scheduled_date"] == "2024-04-15"
        assert v3["metadata"]["superseded_version"] == "1"
        assert "sig:c:m3" in v3["metadata"]["supporting_documents"]
        assert v3["entities"] == [{"text": contact, "type": "person"}]

    def test_reprojected_when_commitment_link_appears(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1), commitment=False)
        first = p.hindsight.docs(p.bank)["sig:c:m1"]
        assert "commitment_id" not in first["metadata"]
        p.run()                                              # commitment stage now enabled
        second = p.hindsight.docs(p.bank)["sig:c:m1"]
        assert second["metadata"]["commitment_id"] == p.commitments()[0]
        assert sum(1 for i in p.hindsight.retained[p.bank] if i["document_id"] == "sig:c:m1") == 2

    def test_unverified_identity_is_projected_without_link(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1, to="alice@x.com, bob@y.com"))
        doc = p.hindsight.docs(p.bank)["sig:c:m1"]
        assert doc["entities"] == [] and "observation_scopes" not in doc

    def test_nothing_written_without_stage_configs(self, tmp_path) -> None:
        p = Pipeline(tmp_path)
        r = p.run(content("m1", sent=True, body=COMMIT_BODY, day=1),
                  commitment=False, projection=False)
        assert r.commitment == {} and r.projection == {}
        assert p.model.calls == [] and p.hindsight.retained == {}
        assert p.resolution()["c:m1"]["commitment_status"] == "pending"


# ── Business isolation ────────────────────────────────────────────────────────

class TestIsolation:
    def test_two_businesses_share_nothing(self, tmp_path) -> None:
        a = Pipeline(tmp_path, "biz-a")
        b = Pipeline(tmp_path, "biz-b")
        b.hindsight = a.hindsight                            # same Hindsight server
        a.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
        b.run(content("m1", sent=False, body=REQUEST_BODY, day=2))
        assert len(a.commitments()) == 1
        assert b.commitments() == []                          # no commitment to change in B
        assert b.resolution()["c:m1"]["commitment_outcome"] == "ledger_unresolved"
        docs_a, docs_b = a.hindsight.docs(a.bank), a.hindsight.docs(b.bank)
        assert any(d.startswith("ledger:") for d in docs_a)
        assert not any(d.startswith("ledger:") for d in docs_b)
        assert docs_a["sig:c:m1"]["content"] != docs_b["sig:c:m1"]["content"]
