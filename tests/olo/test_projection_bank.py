"""tests/olo/test_projection_bank.py — S5 preflight: bank preparation and
consolidation in the projection stage.

Bank preparation must:
  * create a missing bank and register the structured-no-llm strategy before
    the first retain (ledger-state documents never reach an unprepared bank);
  * be a read and no write when repeated;
  * keep every unrelated bank setting, including other retain strategies,
    although Hindsight's config PATCH replaces a top-level key wholesale;
  * fail closed — nothing retained — when the bank cannot be prepared.

Consolidation must:
  * run when the server reports prose memories pending consolidation, even
    though server auto-consolidation is off, scoped to prose (``olo:prose``)
    so structured and ledger-state memories never reach the LLM;
  * wait for in-flight retains, and take neither a completed retain nor a
    completed consolidation as proof that observations formed;
  * be tracked as an operation: completed / failed / cancelled (stale or over
    budget), adopted rather than duplicated after a crash, retried a bounded
    number of times, guarded by the usage guard.

The model and Hindsight are mocks (see test_s5_pipeline); no network.
"""
from __future__ import annotations

import pytest

from olo.m3.hindsight.client import HindsightAPIError, HindsightUnavailable
from olo.projection import BankNotPrepared, ProjectionLimits, prepare_bank
from tests.olo.test_projection_guard import Clock, _ops, _outbox, _signals
from tests.olo.test_s5_pipeline import COMMIT_BODY, Hindsight, Pipeline, content

pytestmark = pytest.mark.not_integration

STRUCTURED = "structured-no-llm"
WANTED = {"retain_extraction_mode": "chunks", "enable_observations": False}


@pytest.fixture()
def clock() -> Clock:
    return Clock()


def _offsets(submits: list[tuple[str, int]]) -> list[tuple[int, int]]:
    """(start, size) of each submission within the bank's retained items."""
    out, start = [], 0
    for _, n in submits:
        out.append((start, n))
        start += n
    return out


# ── Bank preparation ──────────────────────────────────────────────────────────

class TestBankPreparation:
    def test_new_bank_is_created_and_prepared_before_first_retain(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        r = p.run(content("m1", sent=True, body=COMMIT_BODY, day=1), clock=clock)
        assert r.projection["bank_prepared"] == "created"
        assert p.bank in p.hindsight.retained
        assert any(d.startswith("ledger:") for d in p.hindsight.docs(p.bank))
        assert p.hindsight.unprepared_retains == []
        strategies = p.hindsight.banks[p.bank]["retain_strategies"]
        assert strategies[STRUCTURED] == WANTED
        assert p.hindsight.banks[p.bank]["enable_auto_consolidation"] is False

    def test_auto_consolidation_is_turned_off_for_the_bank(self, tmp_path, clock) -> None:
        """Hindsight's automatic consolidations are bank-wide and on by default:
        they would hand ledger-state memories to the consolidation LLM."""
        p = Pipeline(tmp_path)
        p.hindsight.banks[p.bank] = {"retain_strategies": {STRUCTURED: dict(WANTED)},
                                     "enable_auto_consolidation": True}
        r = p.run(*_signals(1), clock=clock)
        assert r.projection["bank_prepared"] == "updated"
        assert p.hindsight.patches == [(p.bank, {"enable_auto_consolidation": False})]

    def test_repeat_preparation_writes_nothing(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.run(*_signals(1), clock=clock)
        patches = len(p.hindsight.patches)
        for _ in range(3):
            assert p.run(clock=clock).projection["bank_prepared"] == "unchanged"
        assert len(p.hindsight.patches) == patches == 1

    def test_unrelated_bank_settings_are_kept(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        other = {"retain_extraction_mode": "verbatim"}
        p.hindsight.banks[p.bank] = {"retain_extraction_mode": "verbatim",
                                     "retain_strategies": {"legal-verbatim": other},
                                     "enable_observations": True}
        p.run(*_signals(1), clock=clock)
        bank = p.hindsight.banks[p.bank]
        assert bank["retain_extraction_mode"] == "verbatim"
        assert bank["enable_observations"] is True
        assert bank["retain_strategies"] == {"legal-verbatim": other, STRUCTURED: WANTED}
        assert [set(u) for _, u in p.hindsight.patches] == [
            {"retain_strategies", "enable_auto_consolidation"}]

    def test_server_level_strategies_survive_the_bank_override(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.global_strategies = {"server-wide": {"retain_extraction_mode": "chunks"}}
        p.run(*_signals(1), clock=clock)
        assert set(p.hindsight.banks[p.bank]["retain_strategies"]) == {"server-wide", STRUCTURED}

    def test_wrong_structured_strategy_is_corrected_keeping_its_extra_fields(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.banks[p.bank] = {"retain_strategies": {
            STRUCTURED: {"retain_extraction_mode": "concise", "enable_observations": True,
                         "retain_chunk_size": 900}}}
        r = p.run(*_signals(1), clock=clock)
        assert r.projection["bank_prepared"] == "updated"
        assert p.hindsight.banks[p.bank]["retain_strategies"][STRUCTURED] == {
            **WANTED, "retain_chunk_size": 900}
        assert p.hindsight.unprepared_retains == []

    @pytest.mark.parametrize("error", [
        HindsightAPIError("method not allowed", status_code=405),
        HindsightAPIError("server error", status_code=500),
        HindsightUnavailable("connection refused"),
    ])
    def test_unpreparable_bank_receives_nothing(self, tmp_path, clock, error) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.config_error = error
        r = p.run(content("m1", sent=True, body=COMMIT_BODY, day=1), clock=clock)
        assert r.status == "succeeded"
        assert r.projection["bank_unprepared"] == 1 and r.projection["submitted"] == 0
        assert p.hindsight.retained == {} and _outbox(p) == {}
        assert p.hindsight.consolidations == []
        p.hindsight.config_error = None                       # recovers on a later run
        assert p.run(clock=clock).projection["submitted"] > 0

    def test_update_that_does_not_take_effect_fails_closed(self) -> None:
        class Ignores(Hindsight):
            def patch_bank_config(self, updates, bank_id=None):
                self.patches.append((bank_id, updates))
                return self.get_bank_config(bank_id=bank_id)
        hs = Ignores()
        with pytest.raises(BankNotPrepared):
            prepare_bank(hs, "olo-x")
        assert "olo-x" in hs.banks and hs.retained == {}

    def test_each_submission_holds_one_strategy(self, tmp_path, clock) -> None:
        """Hindsight 1.2.1 rejects a retain whose items mix strategies under one
        operation_id (HTTP 400): prose signals and ledger-state documents go in
        separate submissions."""
        p = Pipeline(tmp_path)
        r = p.run(content("m1", sent=True, body=COMMIT_BODY, day=1), clock=clock)
        assert r.projection["failed"] == 0 and r.projection["submitted"] == 2
        assert set(_outbox(p).values()) == {"confirmed"}
        strategies = [{d["strategy"] for d in p.hindsight.retained[p.bank][i:i + n]}
                      for i, n in _offsets(p.hindsight.submits)]
        assert sorted(map(sorted, strategies)) == [["prose"], ["structured-no-llm"]]

    def test_existing_prepared_bank_is_left_untouched(self) -> None:
        hs = Hindsight()
        hs.banks["olo-x"] = {"retain_strategies": {STRUCTURED: dict(WANTED)},
                             "enable_auto_consolidation": False}
        assert prepare_bank(hs, "olo-x") == "unchanged"
        assert hs.patches == []


# ── Consolidation ─────────────────────────────────────────────────────────────

class TestConsolidation:
    def test_prose_retains_are_consolidated_and_tracked(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        r = p.run(*_signals(2), clock=clock)
        assert r.projection["consolidation_pending"] == 2      # read from the server
        assert r.projection["consolidation_submitted"] == 1
        assert r.projection["consolidation_completed"] == 1
        assert r.projection["consolidation_pending_after"] == 0
        assert list(_ops(p, "consolidation").values()) == ["completed"]
        again = p.run(clock=clock)
        assert again.projection["consolidation_pending"] == 0
        assert again.projection["consolidation_submitted"] == 0
        assert len(p.hindsight.consolidations) == 1

    def test_consolidation_is_scoped_to_prose_only(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1), clock=clock)
        docs = p.hindsight.docs(p.bank)
        assert "olo:prose" in docs["sig:c:m1"]["tags"]
        ledger = [d for k, d in docs.items() if k.startswith("ledger:")]
        assert ledger and all("olo:prose" not in d["tags"] for d in ledger)
        assert p.hindsight.scopes == [[["olo:prose"]]]

    def test_each_customer_keeps_its_own_observation_scopes(self, tmp_path, clock) -> None:
        """The prose tag is a candidate filter only: each verified customer's
        document keeps its per-customer, cross-month scopes, which Hindsight
        uses for batching, related-observation lookup and writes."""
        p = Pipeline(tmp_path)
        p.run(content("m1", sent=True, day=1, to="alice@x.com", body="Alice prefers mornings."),
              content("m2", sent=True, day=2, to="bob@y.com", body="Bob prefers evenings."),
              clock=clock)
        docs = p.hindsight.docs(p.bank)
        res = p.resolution()
        for sig in ("c:m1", "c:m2"):
            ent = f"ent:{res[sig]['contact_id']}"
            assert docs[f"sig:{sig}"]["observation_scopes"] == [[ent, "ym:*"], [ent]]
            assert all("olo:prose" not in s for s in docs[f"sig:{sig}"]["observation_scopes"])
        assert res["c:m1"]["contact_id"] != res["c:m2"]["contact_id"]

    def test_unverified_prose_is_never_consolidated(self, tmp_path, clock) -> None:
        """Unlinked prose has no customer scope; Hindsight would consolidate it
        over its own tags ({ym:YYYY-MM}), pooling different senders."""
        p = Pipeline(tmp_path)
        r = p.run(content("m1", sent=True, day=1, to="alice@x.com, bob@y.com", body="Group note."),
                  content("m2", sent=True, day=2, to="carol@z.com, dan@w.com", body="Other note."),
                  clock=clock)
        docs = p.hindsight.docs(p.bank)
        for sig in ("sig:c:m1", "sig:c:m2"):
            assert docs[sig]["strategy"] == "prose" and "observation_scopes" not in docs[sig]
            assert "olo:prose" not in docs[sig]["tags"]
        assert r.projection["consolidation_pending"] == 0
        assert p.hindsight.consolidations == []

    def test_waits_while_retains_are_in_flight(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.pending[p.bank] = 1                      # earlier memories pending
        p.hindsight.next_status = "processing"
        r = p.run(*_signals(1), clock=clock)
        assert r.projection["consolidation_deferred"] == 1
        assert p.hindsight.consolidations == []

    def test_completed_retain_is_not_taken_as_consolidated(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.consolidation_status = "processing"
        r = p.run(*_signals(1), clock=clock)
        assert set(_outbox(p).values()) == {"confirmed"}        # retain done …
        assert list(_ops(p, "consolidation").values()) == ["submitted"]   # … observations not
        assert r.projection["consolidation_completed"] == 0
        nxt = p.run(clock=clock)
        assert nxt.projection["consolidation_in_flight"] == 1
        assert len(p.hindsight.consolidations) == 1             # not triggered twice

    def test_completed_consolidation_that_leaves_prose_pending_is_bounded(self, tmp_path, clock) -> None:
        """The server reports a skipped consolidation as completed: completion
        alone is not proof, and re-triggering stops after the attempt bound."""
        p = Pipeline(tmp_path)
        p.limits = ProjectionLimits(max_consolidation_attempts=2)
        p.hindsight.consolidation_drains = False
        r1 = p.run(*_signals(1), clock=clock)
        assert r1.projection["consolidation_completed"] == 1
        assert r1.projection["consolidation_pending_after"] == 1
        assert p.run(clock=clock).projection["consolidation_submitted"] == 1
        r3 = p.run(clock=clock)
        assert r3.projection["consolidation_retries_exhausted"] == 1
        assert len(p.hindsight.consolidations) == 2
        p.hindsight.consolidation_drains = True               # new prose resets the bound
        r4 = p.run(content("m9", sent=True, day=9, body="Another note."), clock=clock)
        assert r4.projection["consolidation_submitted"] == 1
        assert r4.projection["consolidation_pending_after"] == 0

    def test_nothing_pending_triggers_nothing(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.run(*_signals(1), clock=clock)
        before = len(p.hindsight.consolidations)
        p.run(clock=clock)
        assert len(p.hindsight.consolidations) == before == 1

    def test_failed_consolidation_is_retried_then_stops(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.limits = ProjectionLimits(max_consolidation_attempts=2)
        p.hindsight.consolidation_status = "failed"
        r1 = p.run(*_signals(1), clock=clock)
        assert r1.projection["consolidation_failed"] == 1
        r2 = p.run(clock=clock)
        assert r2.projection["consolidation_failed"] == 1
        r3 = p.run(clock=clock)
        assert r3.projection["consolidation_retries_exhausted"] == 1
        assert r3.projection["consolidation_submitted"] == 0
        assert list(_ops(p, "consolidation").values()) == ["failed", "failed"]

    def test_trigger_error_counts_as_a_failed_attempt(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.limits = ProjectionLimits(max_consolidation_attempts=1)
        p.hindsight.trigger_error = True
        r1 = p.run(*_signals(1), clock=clock)
        assert r1.projection["consolidation_failed"] == 1
        p.hindsight.trigger_error = False
        r2 = p.run(clock=clock)
        assert r2.projection["consolidation_retries_exhausted"] == 1
        assert p.hindsight.consolidations == []

    def test_stale_consolidation_is_cancelled(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.consolidation_status = "processing"
        p.run(*_signals(1), clock=clock)
        [op] = _ops(p, "consolidation")
        clock.advance(minutes=61)
        r = p.run(clock=clock)
        assert r.projection["consolidation_cancelled"] == 1 and op in p.hindsight.cancelled
        assert _ops(p, "consolidation")[op] == "cancelled"

    def test_overspend_cancels_running_consolidation(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.consolidation_status = "processing"
        p.run(*_signals(1), clock=clock)
        [op] = _ops(p, "consolidation")
        p.guard.exceeded = True
        r = p.run(clock=clock)                                 # nothing new to submit
        assert r.projection["budget_exceeded"] == 1
        assert r.projection["consolidation_cancelled"] == 1 and op in p.hindsight.cancelled

    def test_guard_stop_prevents_the_trigger(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.run(*_signals(1), clock=clock)
        p.hindsight.pending[p.bank] = 3                      # new memories pending
        p.guard.exceeded = True
        r = p.run(clock=clock)
        assert r.projection["budget_exceeded"] == 1
        assert len(p.hindsight.consolidations) == 1          # only the first run's

    def test_unrecorded_server_consolidation_is_adopted_not_duplicated(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.next_status = "processing"
        p.run(*_signals(1), clock=clock)                      # retain still running
        p.hindsight.pending[p.bank] = 1
        p.hindsight.ops = {k: "completed" for k in p.hindsight.ops}
        p.hindsight.ops["srv-1"] = "processing"               # trigger sent, crash before record
        p.hindsight.server_consolidations.append((p.bank, "srv-1"))
        r = p.run(clock=clock)
        assert r.projection["consolidation_in_flight"] == 1
        assert p.hindsight.consolidations == []
        assert _ops(p, "consolidation") == {"srv-1": "submitted"}

    def test_unknown_pending_state_triggers_nothing(self, tmp_path, clock) -> None:
        p = Pipeline(tmp_path)
        p.hindsight.stats_error = True
        r = p.run(*_signals(1), clock=clock)
        assert r.projection["consolidation_unknown"] == 1
        assert p.hindsight.consolidations == []

    def test_banks_consolidate_separately(self, tmp_path, clock) -> None:
        a, b = Pipeline(tmp_path, "biz-a"), Pipeline(tmp_path, "biz-b")
        b.hindsight = a.hindsight
        a.run(*_signals(1), clock=clock)
        b.run(clock=clock)                                    # b has nothing pending
        assert [bank for bank, _ in a.hindsight.consolidations] == ["olo-biz-a"]
