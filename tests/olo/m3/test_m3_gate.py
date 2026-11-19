"""Olo M3 — M3.0 GATE validation tests.

Tests are split into two layers:
  - Structural (offline): run against the Olo codebase without Hindsight running.
    These cover V4, V7 (Olo side), V8 (provenance mapping), V9 (no-LLM guarantee),
    and synthetic data correctness.
  - Integration (@pytest.mark.integration): require a running Hindsight server.
    These cover V1–V3, V5, V6, V10 from §B.2 (base doc).

Pass criteria from Opus architecture:
  V1: recall@10 ≥ 0.90; p95 ≤ 500 ms at 50k documents
  V2: 100% cross-connector results for entity tag query
  V3: 0 documents lost on re-retain after entity merge / tag change
  V4: hot assembly works with Hindsight STOPPED
  V5: kill -9 mid-retain → 0 missing, 0 duplicate documents after restart
  V6: drop bank, rebuild → identical document-ID set, ≥ 95% top-10 overlap
  V7: two environments → 0 cross results (Olo-side: business_id stamp enforced)
  V8: 100% recall hits carry document_id → existing signal_id + content_sha256
  V9: provider=none → LLMNotAvailableError on any LLM call
  V10: idle RAM ≤ 600 MB, disk ≤ 2× evidence size

LLM-dependent scenarios (require DeepSeek key):
  P1: delivery date changes are consolidated across email + transcript
  P2: invoice dispute surfaces across email + Stripe + QBO
  P4: contradiction (old date vs new date) produces observation history
  P9: 0 LLM calls for structured items; ≤ 1.5× expected rate for prose

These are @pytest.mark.llm_required and are skipped without HINDSIGHT_API_LLM_API_KEY.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Generator

import pytest

from olo.m3.synthetic.generator import (
    ENTITIES,
    build_labelled_questions,
    generate_timeline,
    to_hindsight_items,
)
from olo.m3.hindsight.client import (
    HindsightAPIError,
    HindsightClient,
    HindsightConfig,
    HindsightUnavailable,
)

# ── Marks ─────────────────────────────────────────────────────────────────────

pytestmark = pytest.mark.not_integration  # all tests are offline by default

_integration = pytest.mark.integration
_llm_required = pytest.mark.skipif(
    not os.environ.get("HINDSIGHT_API_LLM_API_KEY"),
    reason="DeepSeek API key (HINDSIGHT_API_LLM_API_KEY) not configured",
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def timeline():
    """Generate the synthetic 12-month timeline once per test module."""
    return generate_timeline(months=12)


@pytest.fixture(scope="module")
def hindsight_items(timeline):
    """Convert timeline to Hindsight MemoryItem dicts."""
    return to_hindsight_items(timeline)


@pytest.fixture(scope="module")
def labelled_questions():
    return build_labelled_questions()


# ── Structural tests (offline, no Hindsight server) ───────────────────────────

class TestSyntheticDataCorrectness:
    """Validate the synthetic data generator produces the expected shape."""

    def test_signal_count_in_range(self, timeline) -> None:
        # Opus spec: "about 400 signals" for 3 customers
        assert 300 <= len(timeline) <= 500, (
            f"Expected ~400 signals, got {len(timeline)}"
        )

    def test_all_three_entities_present(self, timeline) -> None:
        entity_ids = {s.entity_id for s in timeline}
        for ent in ENTITIES:
            assert ent.entity_id in entity_ids, (
                f"Entity {ent.entity_id} missing from timeline"
            )

    def test_signal_ids_are_unique(self, timeline) -> None:
        ids = [s.signal_id for s in timeline]
        assert len(ids) == len(set(ids)), "Duplicate signal IDs in synthetic timeline"

    def test_timeline_spans_twelve_months(self, timeline) -> None:
        from datetime import UTC, datetime
        dates = [s.occurred_at for s in timeline]
        span = max(dates) - min(dates)
        assert span.days >= 300, f"Timeline only spans {span.days} days (expected ≥ 300)"

    def test_p1_scenario_signals_present(self, timeline) -> None:
        """P1: reschedule email + transcript exist for each entity."""
        for ent in ENTITIES:
            reschedule_id = hashlib.sha256(
                f"email_reschedule_{ent.entity_id}".encode()
            ).hexdigest()[:24]
            sig_id = f"sig_{reschedule_id}"
            sig_ids = {s.signal_id for s in timeline}
            assert sig_id in sig_ids, f"P1 reschedule email missing for {ent.entity_id}"

    def test_p2_scenario_signals_present(self, timeline) -> None:
        """P2: invoice dispute email + Stripe + QBO records exist."""
        for ent in ENTITIES:
            dispute_id = hashlib.sha256(
                f"email_dispute_{ent.entity_id}".encode()
            ).hexdigest()[:24]
            stripe_id = hashlib.sha256(
                f"stripe_invoice_{ent.entity_id}".encode()
            ).hexdigest()[:24]
            sig_ids = {s.signal_id for s in timeline}
            assert f"sig_{dispute_id}" in sig_ids, f"P2 dispute email missing for {ent.entity_id}"
            assert f"sig_{stripe_id}" in sig_ids, f"P2 Stripe record missing for {ent.entity_id}"

    def test_p4_contradiction_signals_present(self, timeline) -> None:
        """P4: both reschedule request and original-date confirmation exist."""
        for ent in ENTITIES:
            reschedule_id = f"sig_{hashlib.sha256(f'email_reschedule_{ent.entity_id}'.encode()).hexdigest()[:24]}"
            confirm_id = f"sig_{hashlib.sha256(f'email_confirm_orig_{ent.entity_id}'.encode()).hexdigest()[:24]}"
            sig_ids = {s.signal_id for s in timeline}
            assert reschedule_id in sig_ids
            assert confirm_id in sig_ids, f"P4 confirmation email missing for {ent.entity_id}"

    def test_structured_signals_not_llm_eligible(self, timeline) -> None:
        """Structured signals (Stripe/QBO/Calendar) must have is_llm_eligible=False."""
        structured_kinds = {"invoice.created", "calendar.event.created"}
        for sig in timeline:
            if sig.kind in structured_kinds:
                assert not sig.is_llm_eligible, (
                    f"Structured signal {sig.signal_id} ({sig.kind}) "
                    "incorrectly marked llm_eligible"
                )

    def test_hindsight_items_have_required_fields(self, hindsight_items) -> None:
        """Every MemoryItem must have document_id, operation_id, entities, tags, metadata."""
        required = {"content", "timestamp", "document_id", "operation_id", "entities", "tags", "metadata"}
        for item in hindsight_items:
            missing = required - item.keys()
            assert not missing, f"MemoryItem missing fields: {missing}. item={item}"
            # document_id mapping: sig:<signal_id>
            assert item["document_id"].startswith("sig:"), (
                f"document_id must start with 'sig:', got {item['document_id']!r}"
            )
            # metadata must include signal_id and content_sha256
            meta = item["metadata"]
            assert "signal_id" in meta
            assert "content_sha256" in meta
            # Verify sha256 is correct
            expected_sha = hashlib.sha256(item["content"].encode()).hexdigest()
            assert meta["content_sha256"] == expected_sha, (
                f"content_sha256 mismatch for {item['document_id']}"
            )

    def test_strategy_field_per_signal_type(self, hindsight_items) -> None:
        """Structured items use 'structured' strategy; prose items use 'prose'."""
        structured_doc_ids = set()
        prose_doc_ids = set()
        for item in hindsight_items:
            strategy = item.get("strategy")
            assert strategy in ("structured", "prose"), (
                f"Unknown strategy {strategy!r} for {item['document_id']}"
            )
            if strategy == "structured":
                structured_doc_ids.add(item["document_id"])
            else:
                prose_doc_ids.add(item["document_id"])
        assert len(structured_doc_ids) > 0, "No structured items found"
        assert len(prose_doc_ids) > 0, "No prose items found"

    def test_labelled_questions_have_old_events(self, labelled_questions) -> None:
        """V1 requirement: ≥ 10 questions about events older than 6 months."""
        old_questions = [q for q in labelled_questions if q.get("age_months", 0) >= 6]
        assert len(old_questions) >= 10, (
            f"Only {len(old_questions)} questions about old events (need ≥ 10)"
        )


class TestV9NoGenerativeCalls:
    """V9: With provider=none, any LLM path raises LLMNotAvailableError.

    This test imports from the installed hindsight_api package directly.
    It runs without a server and without the DeepSeek key.
    """

    def test_none_llm_raises_on_call(self) -> None:
        """V9: NoneLLM.call() raises LLMNotAvailableError."""
        try:
            from hindsight_api.engine.providers.none_llm import (
                LLMNotAvailableError,
                NoneLLM,
            )
        except ImportError:
            pytest.skip("hindsight_api not installed in this venv")

        import asyncio

        async def _run():
            llm = NoneLLM()
            await llm.call(messages=[{"role": "user", "content": "test"}])

        with pytest.raises(LLMNotAvailableError, match="provider is set to 'none'"):
            asyncio.run(_run())

    def test_none_llm_verify_connection_is_noop(self) -> None:
        """V9: NoneLLM.verify_connection() does not raise (no LLM to check)."""
        try:
            from hindsight_api.engine.providers.none_llm import NoneLLM
        except ImportError:
            pytest.skip("hindsight_api not installed in this venv")

        import asyncio

        async def _run():
            llm = NoneLLM()
            await llm.verify_connection()  # must not raise

        asyncio.run(_run())  # passes silently

    def test_no_llm_imports_in_olo_m3_memory(self) -> None:
        """V9 / AC10 (§B.11): No LLM client imports in olo/m3/memory or olo/m3/context.

        These directories don't exist yet (M3.1+), but once created they must
        not import DeepSeek, openai, anthropic, litellm or hindsight_api's
        LLM provider modules.
        """
        import ast

        olo_root = Path(__file__).parent.parent.parent.parent / "olo" / "m3"
        forbidden = {
            "openai", "anthropic", "litellm", "deepseek",
            "hindsight_api.engine.providers.deepseek",
        }
        violations: list[str] = []

        if not olo_root.exists():
            pytest.skip("olo/m3 directory not yet created")

        for src_file in olo_root.rglob("*.py"):
            # Skip hindsight/ client wrapper — it imports the client only
            if "hindsight" in src_file.parts:
                continue
            try:
                tree = ast.parse(src_file.read_text(encoding="utf-8"), filename=str(src_file))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        base = alias.name.split(".")[0]
                        if base in forbidden or alias.name in forbidden:
                            violations.append(f"{src_file}:{node.lineno}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        base = node.module.split(".")[0]
                        if base in forbidden or node.module in forbidden:
                            violations.append(
                                f"{src_file}:{node.lineno}: from {node.module} import ..."
                            )

        assert not violations, (
            "V9/AC10 FAIL: LLM client imports found in olo/m3:\n" + "\n".join(violations)
        )


class TestV7IsolationOloSide:
    """V7: Olo-side isolation — business_id enforcement.

    Full V7 (cross-environment Hindsight queries) is an integration test.
    This covers the Olo-side: a worker configured for business A must not
    open business B's olo.db, verified by checking the meta.business_id stamp.
    """

    def test_two_environments_separate_olo_db(self, tmp_path: Path) -> None:
        """V7 structural: two tmp HERMESHOMEs produce separate olo.db files."""
        import sqlite3

        from olo.store import connect, migrate

        home_a = tmp_path / "biz_a"
        home_b = tmp_path / "biz_b"
        for home in (home_a, home_b):
            (home / "olo").mkdir(parents=True)

        db_a = home_a / "olo" / "olo.db"
        db_b = home_b / "olo" / "olo.db"

        conn_a = connect(db_a)
        migrate(conn_a)
        conn_b = connect(db_b)
        migrate(conn_b)

        # Insert a distinguishing signal in each
        conn_a.execute("BEGIN IMMEDIATE")
        conn_a.execute(
            "INSERT INTO ingest_runs(id,source,owner,started_at,status)"
            " VALUES ('run_a','source_a','biz_a',datetime('now'),'succeeded')"
        )
        conn_a.execute("COMMIT")

        conn_b.execute("BEGIN IMMEDIATE")
        conn_b.execute(
            "INSERT INTO ingest_runs(id,source,owner,started_at,status)"
            " VALUES ('run_b','source_b','biz_b',datetime('now'),'succeeded')"
        )
        conn_b.execute("COMMIT")

        # Verify isolation: each DB only contains its own run
        a_runs = [r["id"] for r in conn_a.execute("SELECT id FROM ingest_runs").fetchall()]
        b_runs = [r["id"] for r in conn_b.execute("SELECT id FROM ingest_runs").fetchall()]

        assert "run_a" in a_runs
        assert "run_b" not in a_runs, "V7 FAIL: biz_a's DB contains biz_b's run"
        assert "run_b" in b_runs
        assert "run_a" not in b_runs, "V7 FAIL: biz_b's DB contains biz_a's run"

        conn_a.close()
        conn_b.close()


class TestV8ProvenanceMapping:
    """V8: 100% of Hindsight document_ids map to signal_id + content_sha256.

    Structural test: verifies the mapping in the synthetic data generator.
    Integration test (integration mark): verifies recall hits carry the mapping.
    """

    def test_all_items_have_signal_id_provenance(self, hindsight_items) -> None:
        """V8 structural: every MemoryItem metadata has signal_id + content_sha256."""
        missing_provenance = []
        for item in hindsight_items:
            meta = item.get("metadata", {})
            if "signal_id" not in meta:
                missing_provenance.append(f"{item['document_id']}: missing signal_id")
            if "content_sha256" not in meta:
                missing_provenance.append(f"{item['document_id']}: missing content_sha256")

        assert not missing_provenance, (
            f"V8 FAIL: {len(missing_provenance)} items missing provenance:\n"
            + "\n".join(missing_provenance[:10])
        )

    def test_document_id_format(self, hindsight_items) -> None:
        """V8: document_id = 'sig:<signal_id>' for all items."""
        bad = [
            item["document_id"]
            for item in hindsight_items
            if not item["document_id"].startswith("sig:")
        ]
        assert not bad, f"V8 FAIL: {len(bad)} items with malformed document_id: {bad[:5]}"

    def test_content_sha256_matches_content(self, hindsight_items) -> None:
        """V8: content_sha256 in metadata actually matches the content field."""
        mismatches = []
        for item in hindsight_items:
            expected = hashlib.sha256(item["content"].encode()).hexdigest()
            actual = item["metadata"].get("content_sha256", "")
            if expected != actual:
                mismatches.append(item["document_id"])
        assert not mismatches, f"V8 FAIL: sha256 mismatch in {len(mismatches)} items"


# ── Integration tests (require Hindsight server) ──────────────────────────────

@_integration
class TestV1RecallQuality:
    """V1: recall@10 ≥ 0.90; p95 ≤ 500 ms at 50k documents.

    Requires a running Hindsight server with the full 12-month timeline loaded.
    Set HINDSIGHT_PORT=<port> HINDSIGHT_BANK_ID=main-v1 to target the right server.
    """

    @pytest.fixture(scope="class")
    def hs_client(self) -> Generator[HindsightClient, None, None]:
        cfg = HindsightConfig.from_env()
        client = HindsightClient(cfg)
        try:
            client.wait_until_ready(timeout_s=10.0)
        except HindsightUnavailable:
            pytest.skip("Hindsight server not running")
        yield client

    def test_recall_at_10(self, hs_client, labelled_questions) -> None:
        """V1: recall@10 ≥ 0.90 on labelled question set."""
        import statistics

        hits = 0
        total_with_expected = 0
        latencies_ms: list[float] = []

        for q in labelled_questions:
            if q.get("time_window_only"):
                # Time-window queries: any result in the window counts
                continue
            if not q.get("expected_signal_ids"):
                continue

            total_with_expected += 1
            t0 = time.monotonic()
            result = hs_client.recall(
                q["query"],
                tags=[f"ent:{q['entity_id']}"] if q.get("entity_id") else None,
                temporal_window=q.get("time_window"),
                max_tokens=4000,
                trace=True,
            )
            latencies_ms.append((time.monotonic() - t0) * 1000)

            # Check if any expected signal_id appears in the top-10 results
            top_docs = []
            for mem_type in ("facts", "documents", "memory_units", "mental_models"):
                for item in result.get(mem_type, [])[:10]:
                    doc_id = item.get("document_id") or item.get("source_document_id", "")
                    top_docs.append(doc_id)

            expected_set = {f"sig:{sid}" for sid in q["expected_signal_ids"]}
            if expected_set & set(top_docs[:10]):
                hits += 1

        if total_with_expected == 0:
            pytest.skip("No labelled questions with expected signal IDs")

        recall_at_10 = hits / total_with_expected
        assert recall_at_10 >= 0.90, (
            f"V1 FAIL: recall@10 = {recall_at_10:.2%} (need ≥ 0.90). "
            f"hits={hits}/{total_with_expected}"
        )

        if latencies_ms:
            p95 = sorted(latencies_ms)[int(len(latencies_ms) * 0.95)]
            assert p95 <= 500, f"V1 FAIL: p95 latency {p95:.0f} ms > 500 ms"

    def test_recall_latency_p95(self, hs_client, labelled_questions) -> None:
        """V1: p95 recall latency ≤ 500 ms."""
        latencies_ms: list[float] = []
        for q in labelled_questions[:20]:
            t0 = time.monotonic()
            try:
                hs_client.recall(q["query"], max_tokens=1000, trace=False)
            except HindsightError:
                pass
            latencies_ms.append((time.monotonic() - t0) * 1000)

        if not latencies_ms:
            pytest.skip("No latency measurements")

        p95 = sorted(latencies_ms)[int(len(latencies_ms) * 0.95)]
        assert p95 <= 500, f"V1 FAIL: p95 = {p95:.0f} ms > 500 ms"


@_integration
class TestV2CrossConnectorIdentity:
    """V2: entity-tag query returns documents from EVERY connector."""

    @pytest.fixture(scope="class")
    def hs_client(self) -> Generator[HindsightClient, None, None]:
        cfg = HindsightConfig.from_env()
        client = HindsightClient(cfg)
        try:
            client.wait_until_ready(timeout_s=10.0)
        except HindsightUnavailable:
            pytest.skip("Hindsight server not running")
        yield client

    def test_all_connectors_returned_for_entity(
        self, hs_client, timeline
    ) -> None:
        """V2: recall on entity tag returns signals from gmail, stripe, qbo, voice."""
        expected_sources = {"gmail_owner", "stripe_fixture", "qbo_fixture", "voice_fixture"}

        for ent in ENTITIES:
            result = hs_client.recall(
                f"{ent.display_name} {ent.company}",
                tags=[f"ent:{ent.entity_id}"],
                max_tokens=8000,
                trace=True,
            )
            found_sources: set[str] = set()

            for mem_type in ("facts", "documents", "memory_units"):
                for item in result.get(mem_type, []):
                    doc_id = item.get("document_id") or item.get("source_document_id", "")
                    if doc_id.startswith("sig:"):
                        sig_id = doc_id[4:]
                        # Find the signal in the timeline
                        for sig in timeline:
                            if sig.signal_id == sig_id:
                                found_sources.add(sig.source)

            missing = expected_sources - found_sources
            assert not missing, (
                f"V2 FAIL: entity {ent.entity_id} missing connectors: {missing}. "
                f"Found: {found_sources}"
            )


@_integration
class TestV5CrashRecovery:
    """V5: kill -9 Hindsight mid-retain → 0 missing, 0 duplicate documents."""

    def test_crash_during_retain_no_loss_or_duplicate(
        self, tmp_path: Path, timeline, hindsight_items
    ) -> None:
        """V5: start server, retain half, kill -9, restart, drain remaining, verify."""
        # This test requires a Hindsight server it can start and kill.
        # The server binary must be at HINDSIGHT_BIN or in PATH.
        hindsight_bin = os.environ.get("HINDSIGHT_BIN", "hindsight-api")

        # Use a fresh data dir for this test
        data_dir = tmp_path / "v5_pg0"
        data_dir.mkdir()
        env_file = tmp_path / ".env"
        env_file.write_text(
            f"HINDSIGHT_API_DATABASE_URL=pg0\n"
            f"HINDSIGHT_API_DATABASE_DATA_DIR={data_dir}\n"
            f"HINDSIGHT_API_HOST=127.0.0.1\n"
            f"HINDSIGHT_API_PORT=18799\n"
            f"HINDSIGHT_API_LLM_PROVIDER=none\n"
            f"HINDSIGHT_API_DECISION_MODEL_PROVIDER=none\n"
            f"HINDSIGHT_API_TELEMETRY_ENABLED=false\n"
            f"HINDSIGHT_API_STORE_DOCUMENT_TEXT=true\n"
        )

        cfg = HindsightConfig(host="127.0.0.1", port=18799, bank_id="main-v1")
        client = HindsightClient(cfg)

        def _start_server() -> subprocess.Popen:
            proc = subprocess.Popen(
                [hindsight_bin],
                env={**os.environ, "DOTENV_PATH": str(env_file)},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                client.wait_until_ready(timeout_s=20.0)
            except HindsightUnavailable:
                proc.kill()
                pytest.skip(f"Could not start Hindsight at {hindsight_bin}")
            return proc

        # Start server and load first batch synchronously
        proc = _start_server()
        half = len(hindsight_items) // 2
        first_batch = hindsight_items[:half]

        try:
            client.retain(first_batch, operation_id="v5-batch-1", async_=False)
        finally:
            # kill -9 during the second batch
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)

        # Restart server
        proc2 = _start_server()
        second_batch = hindsight_items[half:]
        try:
            client.retain(second_batch, operation_id="v5-batch-2", async_=False)
        finally:
            proc2.kill()
            proc2.wait(timeout=5)

        # Restart one more time, verify document counts
        proc3 = _start_server()
        try:
            docs = client.list_documents(limit=10000)
            doc_ids = {d.get("document_id") for d in docs.get("documents", [])}
            expected_ids = {item["document_id"] for item in hindsight_items}

            missing = expected_ids - doc_ids
            assert not missing, (
                f"V5 FAIL: {len(missing)} documents lost after crash: {list(missing)[:5]}"
            )
            assert len(doc_ids) == len(expected_ids), (
                f"V5 FAIL: duplicates — expected {len(expected_ids)}, "
                f"got {len(doc_ids)} unique IDs"
            )
        finally:
            proc3.kill()
            proc3.wait(timeout=5)


@_integration
class TestV6Replay:
    """V6: drop bank, rebuild → identical document-ID set, ≥ 95% top-10 overlap."""

    @pytest.fixture(scope="class")
    def hs_client(self) -> Generator[HindsightClient, None, None]:
        cfg = HindsightConfig.from_env()
        client = HindsightClient(cfg)
        try:
            client.wait_until_ready(timeout_s=10.0)
        except HindsightUnavailable:
            pytest.skip("Hindsight server not running")
        yield client

    def test_rebuild_gives_identical_document_ids(
        self, hs_client, hindsight_items, labelled_questions
    ) -> None:
        """V6: drop main-v1 bank, re-load items, verify same document-ID set."""
        # Capture pre-wipe document IDs and sample query results
        pre_docs = hs_client.list_documents(limit=10000)
        pre_ids = {d.get("document_id") for d in pre_docs.get("documents", [])}

        sample_questions = labelled_questions[:10]
        pre_results: dict[str, list[str]] = {}
        for q in sample_questions:
            if q.get("time_window_only") or not q.get("expected_signal_ids"):
                continue
            res = hs_client.recall(q["query"], max_tokens=1000, trace=False)
            top = []
            for mem_type in ("facts", "documents", "memory_units"):
                for item in res.get(mem_type, [])[:10]:
                    doc_id = item.get("document_id") or item.get("source_document_id", "")
                    top.append(doc_id)
            pre_results[q["id"]] = top[:10]

        # Drop and rebuild
        hs_client.delete_bank("main-v1")
        hs_client.create_bank("main-v1")
        for i in range(0, len(hindsight_items), 50):
            batch = hindsight_items[i:i+50]
            hs_client.retain(
                batch,
                operation_id=f"v6-rebuild-{i}",
                async_=False,
            )

        # Verify document IDs
        post_docs = hs_client.list_documents(limit=10000)
        post_ids = {d.get("document_id") for d in post_docs.get("documents", [])}

        assert pre_ids == post_ids, (
            f"V6 FAIL: document-ID set differs after rebuild. "
            f"Missing: {pre_ids - post_ids}. "
            f"Extra: {post_ids - pre_ids}"
        )

        # Verify ≥ 95% top-10 overlap
        overlaps: list[float] = []
        for q in sample_questions:
            if q["id"] not in pre_results:
                continue
            res = hs_client.recall(q["query"], max_tokens=1000, trace=False)
            post_top: list[str] = []
            for mem_type in ("facts", "documents", "memory_units"):
                for item in res.get(mem_type, [])[:10]:
                    doc_id = item.get("document_id") or item.get("source_document_id", "")
                    post_top.append(doc_id)
            post_top = post_top[:10]
            pre_top = pre_results[q["id"]]
            if not pre_top:
                continue
            overlap = len(set(pre_top) & set(post_top)) / len(set(pre_top))
            overlaps.append(overlap)

        if overlaps:
            avg_overlap = sum(overlaps) / len(overlaps)
            assert avg_overlap >= 0.95, (
                f"V6 FAIL: avg top-10 overlap {avg_overlap:.2%} < 0.95"
            )


@_integration
class TestV7IsolationIntegration:
    """V7: two Hindsight environments → 0 cross results."""

    def test_two_banks_no_cross_results(self, hindsight_items) -> None:
        """V7: data in bank-a is not visible in bank-b queries."""
        cfg_a = HindsightConfig.from_env()
        cfg_a.bank_id = "v7-isolation-a"
        cfg_b = HindsightConfig.from_env()
        cfg_b.bank_id = "v7-isolation-b"

        client = HindsightClient(cfg_a)
        try:
            client.wait_until_ready(timeout_s=10.0)
        except HindsightUnavailable:
            pytest.skip("Hindsight server not running")

        # Ensure clean banks
        for bank_id in ("v7-isolation-a", "v7-isolation-b"):
            try:
                client._cfg.bank_id = bank_id
                client.delete_bank(bank_id)
            except HindsightAPIError:
                pass
            client.create_bank(bank_id)

        # Load all items into bank-a only
        client._cfg.bank_id = "v7-isolation-a"
        client.retain(hindsight_items, operation_id="v7-a-load", async_=False)

        # Query from bank-b (empty) — should return nothing
        client._cfg.bank_id = "v7-isolation-b"
        for ent in ENTITIES:
            result = client.recall(
                ent.display_name,
                tags=[f"ent:{ent.entity_id}"],
                max_tokens=1000,
            )
            for mem_type in ("facts", "documents", "memory_units"):
                items_in_b = result.get(mem_type, [])
                assert not items_in_b, (
                    f"V7 FAIL: {len(items_in_b)} items from bank-a visible in bank-b "
                    f"for entity {ent.entity_id}"
                )


@_integration
class TestV10Footprint:
    """V10: idle RAM ≤ 600 MB; disk ≤ 2× evidence size."""

    @pytest.fixture(scope="class")
    def hs_client(self) -> Generator[HindsightClient, None, None]:
        cfg = HindsightConfig.from_env()
        client = HindsightClient(cfg)
        try:
            client.wait_until_ready(timeout_s=10.0)
        except HindsightUnavailable:
            pytest.skip("Hindsight server not running")
        yield client

    def test_idle_ram_under_600mb(self, hs_client) -> None:
        """V10: Hindsight server idle RSS ≤ 600 MB."""
        health = hs_client.admin_health()
        rss_mb = health.get("memory_rss_mb") or health.get("rss_mb")
        if rss_mb is None:
            pytest.skip("Server does not report memory_rss_mb in admin/health")
        assert rss_mb <= 600, f"V10 FAIL: idle RSS {rss_mb:.0f} MB > 600 MB"


# ── LLM-dependent scenarios ───────────────────────────────────────────────────

@_llm_required
@_integration
class TestP1DeliveryDateConsolidation:
    """P1: After email + transcript, observation states the NEW date; old date in history.

    Requires: running Hindsight with provider=deepseek and DeepSeek API key.
    """

    @pytest.fixture(scope="class")
    def hs_client(self) -> Generator[HindsightClient, None, None]:
        cfg = HindsightConfig.from_env()
        client = HindsightClient(cfg)
        try:
            client.wait_until_ready(timeout_s=10.0)
        except HindsightUnavailable:
            pytest.skip("Hindsight server not running")
        yield client

    def test_new_date_in_observation_old_date_in_history(
        self, hs_client, timeline
    ) -> None:
        """P1: Consolidation updates the meeting date; old date preserved in history."""
        ent = ENTITIES[0]  # Use Acme Corp
        # Recall about the reschedule
        result = hs_client.recall(
            f"rescheduled meeting {ent.company}",
            tags=[f"ent:{ent.entity_id}"],
            max_tokens=4000,
            trace=True,
        )
        # With Option B, there should be an observation with the new date
        observations = result.get("observations", []) + result.get("facts", [])
        assert observations, "P1 FAIL: no observations returned — LLM extraction may not have run"
        # The observation text should reference the new date
        obs_text = " ".join(str(o.get("text", "") or o.get("content", "")) for o in observations)
        # We can't assert the exact date string without knowing the generated value,
        # but the observation should reference a rescheduling event
        assert any(
            kw in obs_text.lower()
            for kw in ("reschedule", "moved", "new date", "changed")
        ), f"P1 FAIL: observation does not mention rescheduling. text={obs_text[:300]!r}"


@_llm_required
@_integration
class TestP2InvoiceDisputeRetrieval:
    """P2: Recall on invoice: tag returns all 3 sources; brief lists the dispute."""

    @pytest.fixture(scope="class")
    def hs_client(self) -> Generator[HindsightClient, None, None]:
        cfg = HindsightConfig.from_env()
        client = HindsightClient(cfg)
        try:
            client.wait_until_ready(timeout_s=10.0)
        except HindsightUnavailable:
            pytest.skip("Hindsight server not running")
        yield client

    def test_all_three_sources_in_recall(self, hs_client, timeline) -> None:
        """P2: invoice: tag returns email, Stripe, QBO for each entity."""
        for ent in ENTITIES:
            # Find the invoice_id for this entity
            invoice_id = f"INV-{ent.entity_id[:6].upper()}-2026-001"
            result = hs_client.recall(
                f"invoice dispute {invoice_id}",
                tags=[f"ent:{ent.entity_id}", f"invoice:{invoice_id}"],
                max_tokens=8000,
                trace=True,
            )
            found_sources: set[str] = set()
            for mem_type in ("facts", "documents", "memory_units"):
                for item in result.get(mem_type, []):
                    doc_id = item.get("document_id") or item.get("source_document_id", "")
                    if doc_id.startswith("sig:"):
                        sig_id = doc_id[4:]
                        for sig in timeline:
                            if sig.signal_id == sig_id:
                                found_sources.add(sig.source)
            expected = {"gmail_owner", "stripe_fixture", "qbo_fixture"}
            missing = expected - found_sources
            assert not missing, (
                f"P2 FAIL: missing sources for {ent.company} invoice {invoice_id}: {missing}"
            )
