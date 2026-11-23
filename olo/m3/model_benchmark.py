"""Olo M3 — Embedding model benchmark + multi-config retrieval matrix.

Benchmarks across:
  1. Embedding models: E5-small fp32, E5-small int8, IBM Granite R2 int8.
  2. Reranker configs: RRF passthrough (baseline) vs hs-rerank-1 ONNX.
  3. Recency configs: linear decay (default) vs recency=none.

Runs the benchmark v2 (200+ question) set in addition to the v1 regression set.

Each configuration uses an isolated Hindsight instance with an isolated bank.
PostgreSQL PSS is measured separately and reported as combined total.

No PyTorch required.  IBM granite-reranker-english-r2 conversion requires
PyTorch + optimum; that arm is NOT RUN if those are unavailable.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Model pinned snapshot paths
_HF_CACHE = "/var/cache/huggingface/hub"

MODEL_CONFIGS: list[dict] = [
    {
        "id": "e5-small-fp32",
        "display": "multilingual-E5-small fp32",
        "env": {
            "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "onnx",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID": "intfloat/multilingual-e5-small",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_PATH": (
                f"{_HF_CACHE}/models--intfloat--multilingual-e5-small/"
                "snapshots/614241f622f53c4eeff9890bdc4f31cfecc418b3/onnx/model.onnx"
            ),
            "HINDSIGHT_API_EMBEDDINGS_ONNX_POOLING": "mean",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_NORMALIZE": "true",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_QUERY_PREFIX": "query: ",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_PASSAGE_PREFIX": "passage: ",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_CPU_MEM_ARENA": "false",
        },
        "onnx_revision": "614241f622f53c4eeff9890bdc4f31cfecc418b3",
    },
    {
        "id": "e5-small-int8",
        "display": "multilingual-E5-small int8 (Xenova)",
        "env": {
            "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "onnx",
            # Tokenizer from the fully-cached intfloat snapshot (same tokenizer,
            # Xenova snapshot only ships the ONNX file without config.json).
            "HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID": "intfloat/multilingual-e5-small",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_PATH": (
                f"{_HF_CACHE}/models--Xenova--multilingual-e5-small/"
                "snapshots/761b726dd34fb83930e26aab4e9ac3899aa1fa78/onnx/model_int8.onnx"
            ),
            "HINDSIGHT_API_EMBEDDINGS_ONNX_POOLING": "mean",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_NORMALIZE": "true",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_QUERY_PREFIX": "query: ",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_PASSAGE_PREFIX": "passage: ",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_CPU_MEM_ARENA": "false",
        },
        "onnx_revision": "761b726dd34fb83930e26aab4e9ac3899aa1fa78",
    },
    {
        "id": "granite-r2-int8",
        "display": "IBM Granite Embedding 97M R2 int8 (CLS pooling)",
        "env": {
            "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "onnx",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID": (
                "ibm-granite/granite-embedding-97m-multilingual-r2"
            ),
            "HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_PATH": (
                f"{_HF_CACHE}/models--ibm-granite--granite-embedding-97m-multilingual-r2/"
                "snapshots/835ad14087e140460703cf0fae09f97d469d65c2/onnx/model_quint8_avx2.onnx"
            ),
            "HINDSIGHT_API_EMBEDDINGS_ONNX_POOLING": "cls",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_NORMALIZE": "true",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_QUERY_PREFIX": "",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_PASSAGE_PREFIX": "",
            "HINDSIGHT_API_EMBEDDINGS_ONNX_CPU_MEM_ARENA": "false",
            # output_name not set → auto-detect (last_hidden_state)
        },
        "onnx_revision": "835ad14087e140460703cf0fae09f97d469d65c2",
        "notes": (
            "ModernBERT architecture, CLS pooling. No query/passage prefixes "
            "(symmetric retrieval). Multilingual. 93.7 MB int8 ONNX."
        ),
    },
]


@dataclass
class BenchmarkResult:
    model_id: str
    display: str
    # v1 (48-question regression) metrics
    recall_at_10: float
    hits: int
    total: int
    # v2 (200-question) metrics
    v2_recall_at_10: float = 0.0
    v2_recall_at_20: float = 0.0
    v2_recall_at_50: float = 0.0
    v2_hits_10: int = 0
    v2_total: int = 0
    # Configuration
    reranker: str = "rrf"       # "rrf" | "hs-rerank-1" | "granite-reranker"
    recency: str = "linear"     # "linear" | "none"
    # Memory
    idle_pss_mb: float = 0.0
    steady_pss_mb: float = 0.0
    peak_pss_mb: float = 0.0
    pg_pss_mb: float = 0.0
    combined_pss_mb: float = 0.0
    # Latency
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    # Throughput
    ingestion_docs_per_s: float = 0.0
    error: Optional[str] = None
    per_question: list[dict] = field(default_factory=list)
    v2_per_question: list[dict] = field(default_factory=list)


def _measure_pss(pid: int) -> float:
    """Measure PSS from /proc/{pid}/smaps_rollup, fall back to VmRSS."""
    try:
        smaps = Path(f"/proc/{pid}/smaps_rollup").read_text()
        m = re.search(r"^Pss:\s+(\d+)\s+kB", smaps, re.MULTILINE)
        if m:
            return int(m.group(1)) / 1024
    except (PermissionError, FileNotFoundError):
        pass
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        m = re.search(r"^VmRSS:\s+(\d+)\s+kB", status, re.MULTILINE)
        if m:
            return int(m.group(1)) / 1024
    except (PermissionError, FileNotFoundError):
        pass
    return 0.0


def _measure_pg_pss() -> float:
    """Sum PSS of all postgres processes."""
    total = 0.0
    try:
        result = subprocess.run(
            ["pgrep", "-a", "postgres"], capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            parts = line.split(None, 1)
            if parts:
                try:
                    total += _measure_pss(int(parts[0]))
                except (ValueError, FileNotFoundError):
                    pass
    except Exception:
        pass
    return total


def _find_pid_on_port(port: int) -> Optional[int]:
    """Find PID listening on port via ss."""
    try:
        result = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True, text=True,
        )
        m = re.search(r"pid=(\d+)", result.stdout)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def run_benchmark(
    model_cfg: dict,
    db_path: Path,
    hindsight_bin: str,
    port: int,
    pg0_home: Path,
    *,
    timeout_startup: float = 120.0,
    reranker: str = "rrf",
    recency: str = "linear",
    run_v2: bool = True,
) -> "BenchmarkResult":
    """Run v1 (and optionally v2) benchmark for one configuration.

    Args:
        model_cfg:       Entry from MODEL_CONFIGS.
        db_path:         Populated olo.db (source of truth; not modified).
        hindsight_bin:   Path to hindsight-api binary.
        port:            Port for this Hindsight instance.
        pg0_home:        HOME directory for the olo_hs process (pg0 storage).
        timeout_startup: Seconds to wait for Hindsight to become healthy.
        reranker:        "rrf" (passthrough) | "hs-rerank-1" | "granite-reranker-int8"
        recency:         "linear" (default) | "none"
        run_v2:          Also score the 200-question v2 benchmark.
    """
    from olo.m3.hindsight.client import (  # noqa: PLC0415
        HindsightClient, HindsightConfig, HindsightUnavailable, HindsightAPIError,
    )
    from olo.m3.synthetic.generator import build_labelled_questions
    from olo.m3.feeder import build_hindsight_items

    model_id = model_cfg["id"]
    # Include config in display name
    reranker_tag = "" if reranker == "rrf" else f"+{reranker}"
    recency_tag  = "" if recency == "linear" else "+recency=none"
    display = f"{model_cfg['display']}{reranker_tag}{recency_tag}"
    bench_id = f"bench-{model_id}-{reranker[:6]}-{recency[:4]}"
    bank_id  = bench_id

    result = BenchmarkResult(
        model_id=f"{model_id}/{reranker}/{recency}",
        display=display,
        recall_at_10=0.0, hits=0, total=0,
        reranker=reranker, recency=recency,
    )

    # ── Build reranker env ────────────────────────────────────────────────────
    _HS_RERANK_SNAP = (
        f"{_HF_CACHE}/models--hindsight-ai--hs-rerank-1/"
        "snapshots/927f5be31280298a51cf29af8a29cb111ed812c5"
    )
    _GRANITE_RERANK_INT8 = (
        f"{_HF_CACHE}/onnx_exports/granite-reranker-int8/model_int8.onnx"
    )

    reranker_env: dict[str, str] = {}
    if reranker == "rrf":
        reranker_env["HINDSIGHT_API_RERANKER_PROVIDER"] = "rrf"
    elif reranker == "hs-rerank-1":
        reranker_env.update({
            "HINDSIGHT_API_RERANKER_PROVIDER": "onnx",
            "HINDSIGHT_API_RERANKER_ONNX_MODEL_ID": "hindsight-ai/hs-rerank-1",
            "HINDSIGHT_API_RERANKER_ONNX_MODEL_PATH": f"{_HS_RERANK_SNAP}/onnx/model.onnx",
            "HINDSIGHT_API_RERANKER_ONNX_TOKENIZER_NAME_OR_PATH": _HS_RERANK_SNAP,
            # 400 candidates covers the full 385-doc corpus for cross-entity questions,
            # ensuring no target document is excluded before reranking.
            # Production deployment uses 100 for latency budget.
            "HINDSIGHT_API_RERANKER_MAX_CANDIDATES": "400",
        })
    elif reranker == "granite-reranker-int8":
        if not Path(_GRANITE_RERANK_INT8).exists():
            result.error = "Granite reranker ONNX not available (conversion requires PyTorch)"
            return result
        _granite_snap = (
            f"{_HF_CACHE}/models--ibm-granite--granite-embedding-reranker-english-r2/"
            "snapshots/d09d3d6971b689bf9c23839e45a470874d46e13a"
        )
        reranker_env.update({
            "HINDSIGHT_API_RERANKER_PROVIDER": "onnx",
            "HINDSIGHT_API_RERANKER_ONNX_MODEL_ID": "ibm-granite/granite-embedding-reranker-english-r2",
            "HINDSIGHT_API_RERANKER_ONNX_MODEL_PATH": _GRANITE_RERANK_INT8,
            "HINDSIGHT_API_RERANKER_ONNX_TOKENIZER_NAME_OR_PATH": _granite_snap,
            "HINDSIGHT_API_RERANKER_MAX_CANDIDATES": "100",
        })
    else:
        result.error = f"Unknown reranker: {reranker}"
        return result

    # ── Build recency env ─────────────────────────────────────────────────────
    recency_env: dict[str, str] = {}
    if recency == "none":
        recency_env["HINDSIGHT_API_RECENCY_DECAY_FUNCTION"] = "none"
    # "linear" = server default, no override needed

    # ── Base env ──────────────────────────────────────────────────────────────
    socket_dir = str(pg0_home / "run")
    # N6/N2 fix: use safe_pg0_delete_home which ENUMERATES all pg0 instances
    # (not a guessed name).  The old code called pg0_stop(guessed_name) with
    # `except: pass`, then unconditionally deleted the HOME — a live postmaster
    # from a differently named instance would be orphaned.
    if pg0_home.exists():
        from olo.m3.pg0_lifecycle import safe_pg0_delete_home as _safe_del
        from olo.m3.pg0_lifecycle import Pg0StillRunningError as _PgErr
        try:
            _safe_del(pg0_home)
        except _PgErr as _exc:
            # Raise: a live database in pg0_home means this benchmark run
            # is not starting from a clean state.  Fail loudly.
            result.error = f"pg0 pre-clean failed: {_exc}"
            return result
    subprocess.run(
        ["sudo", "-u", "olo_hs", "mkdir", "-p", socket_dir],
        check=False, capture_output=True,
    )
    base_env = {
        "HOME": str(pg0_home),
        "HF_HOME": "/var/cache/huggingface",
        "HF_HUB_OFFLINE": "1",
        "HINDSIGHT_API_DATABASE_URL": f"pg0://{bench_id}?unix_socket_directories={socket_dir}",
        "HINDSIGHT_API_HOST": "127.0.0.1",
        "HINDSIGHT_API_PORT": str(port),
        "HINDSIGHT_API_LLM_PROVIDER": "none",
        "HINDSIGHT_API_DECISION_MODEL_PROVIDER": "none",
        "HINDSIGHT_API_MCP_ENABLED": "false",
        "HINDSIGHT_API_STORE_DOCUMENT_TEXT": "true",
        "OTEL_SDK_DISABLED": "true",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        **model_cfg["env"],
        **reranker_env,
        **recency_env,
    }

    env_list = [f"{k}={v}" for k, v in base_env.items()]
    # Log stderr to a temporary file for diagnosability; cleaned up after startup.
    _stderr_log = Path(f"/tmp/hindsight_bench_{port}.log")
    _stderr_fh = open(_stderr_log, "w")
    proc = subprocess.Popen(
        ["sudo", "-u", "olo_hs", "env"] + env_list + [hindsight_bin],
        stdout=_stderr_fh,
        stderr=_stderr_fh,
        start_new_session=True,
    )

    cfg = HindsightConfig(host="127.0.0.1", port=port, bank_id=bank_id)
    client = HindsightClient(cfg)

    try:
        client.wait_until_ready(timeout_s=timeout_startup)
    except HindsightUnavailable as exc:
        result.error = f"Startup timeout: {exc}"
        try:
            _stderr_fh.flush(); _stderr_fh.close()
            tail = _stderr_log.read_text()[-3000:]
            print(f"  [STDERR last 3000 chars]: {tail}")
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        # N5 fix: stop pg0 after killing the server process group
        from olo.m3.pg0_lifecycle import safe_pg0_delete_home as _safe_del_err
        from olo.m3.pg0_lifecycle import Pg0StillRunningError as _PgErrTeardown
        try:
            _safe_del_err(pg0_home)
        except (_PgErrTeardown, Exception):
            pass  # best-effort on error path
        return result
    _stderr_fh.flush(); _stderr_fh.close()
    _stderr_log.unlink(missing_ok=True)

    # Measure idle PSS after model load
    pid = _find_pid_on_port(port)
    if pid:
        result.idle_pss_mb = _measure_pss(pid)
        result.pg_pss_mb = _measure_pg_pss()

    # Load evidence from olo.db
    try:
        items = build_hindsight_items(db_path)
    except Exception as exc:
        result.error = f"Feeder error: {exc}"
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return result

    # Ingest (prose + structured, no LLM for benchmark isolation).
    # P0.4: model_benchmark uses HINDSIGHT_API_LLM_PROVIDER=none — no LLM calls
    # are made and there is no spend to guard.  budget_guard=None is an explicit
    # bypass documented here; this is NOT an accident.  Strategy validation still
    # runs via batch_retain_to_hindsight.
    t_ingest_start = time.monotonic()
    from olo.m3.feeder import batch_retain_to_hindsight, prepare_hindsight_bank
    prepare_hindsight_bank(client, bank_id)
    batch_counters = batch_retain_to_hindsight(
        items,
        client,
        bank_id=bank_id,
        batch_size=50,
        budget_guard=None,  # explicit: LLM_PROVIDER=none, no spend to guard
        async_=False,
        prepare_bank=False,  # already called above
    )
    ingest_time_s = time.monotonic() - t_ingest_start
    result.ingestion_docs_per_s = len(items) / max(ingest_time_s, 0.001)

    # Measure steady-state PSS after ingestion
    if pid:
        result.steady_pss_mb = _measure_pss(pid)
        result.peak_pss_mb = max(result.idle_pss_mb, result.steady_pss_mb)
        result.combined_pss_mb = result.steady_pss_mb + result.pg_pss_mb

    def _run_recall(query: str, tg: list, max_tok: int) -> dict:
        """Retry-safe recall call."""
        for _attempt in range(2):
            try:
                return client.recall(
                    query,
                    tag_groups=tg if tg else None,
                    max_tokens=max_tok,
                    trace=True,
                    include_source_facts=True,  # credit observations via source docs
                    bank_id=bank_id,
                )
            except Exception:
                if _attempt == 0:
                    time.sleep(5)
        return {"results": []}

    def _score_hit(recall_result: dict, expected: set[str], top_n: int) -> tuple[bool, int]:
        """Document-level hit detection with source_facts.

        Returns (hit, rank) where rank is 1-indexed, -1 if miss.
        Deduplicates by document_id so a doc can't occupy multiple slots.
        """
        sf_map = recall_result.get("source_facts", {})
        seen_docs: set[str] = set()
        deduplicated: list[tuple[str, dict]] = []
        for r in recall_result.get("results", []):
            doc_id = r.get("document_id", "") or ""
            if doc_id and doc_id not in seen_docs:
                seen_docs.add(doc_id)
                deduplicated.append((doc_id, r))
            elif not doc_id:
                deduplicated.append((doc_id, r))  # observation without direct doc_id

        for rank, (doc_id, r) in enumerate(deduplicated[:top_n], start=1):
            if doc_id in expected:
                return True, rank
            # Credit via inline source_fact_ids
            for sfid in r.get("source_fact_ids", []) or []:
                sd = sfid.get("document_id", "") if isinstance(sfid, dict) else str(sfid)
                if sd in expected:
                    return True, rank
            # Credit via full source_facts map
            for sfid in r.get("source_fact_ids", []) or []:
                fact_key = sfid.get("id", "") if isinstance(sfid, dict) else str(sfid)
                if fact_key in sf_map:
                    sf = sf_map[fact_key]
                    sd = sf.get("document_id", "") if isinstance(sf, dict) else ""
                    if sd in expected:
                        return True, rank
        return False, -1

    # ── V1 regression benchmark (48 questions) ────────────────────────────────
    questions = build_labelled_questions()
    scored = [q for q in questions if q.get("expected_signal_ids") and not q.get("time_window_only")]
    hits = 0
    latencies: list[float] = []

    for q in scored:
        tg: list[dict] = []
        if q.get("entity_id"):
            tg.append({"tags": [f"ent:{q['entity_id']}"], "match": "all_strict"})
        if q.get("temporal_ym"):
            tg.append({"tags": [q["temporal_ym"]], "match": "any_strict"})

        t0 = time.monotonic()
        rr = _run_recall(q["query"], tg, 4000)
        latencies.append((time.monotonic() - t0) * 1000)

        expected = {f"sig:{sid}" for sid in q["expected_signal_ids"]}
        hit, rank = _score_hit(rr, expected, 10)
        if hit:
            hits += 1
        result.per_question.append({
            "id": q["id"],
            "query": q["query"],
            "hit": hit,
            "rank": rank,
            "expected": list(expected)[:1],
            "top_doc_ids": [r.get("document_id", "") or "" for r in rr.get("results", [])[:5]],
            "top_types":   [r.get("type", "?") for r in rr.get("results", [])[:5]],
        })
        time.sleep(0.05)

    result.hits = hits
    result.total = len(scored)
    result.recall_at_10 = hits / max(len(scored), 1)

    if latencies:
        sorted_lat = sorted(latencies)
        result.p50_latency_ms = sorted_lat[len(sorted_lat) // 2]
        result.p95_latency_ms = sorted_lat[int(len(sorted_lat) * 0.95)]
        result.max_latency_ms = sorted_lat[-1]

    # ── V2 benchmark (200+ questions) ─────────────────────────────────────────
    if run_v2:
        from olo.m3.benchmark_v2 import build_benchmark_v2, score_recall  # noqa: PLC0415
        from olo.m3.synthetic.generator import generate_timeline           # noqa: PLC0415

        tl = generate_timeline(months=12)
        v2_questions = build_benchmark_v2(tl)
        v2_hits_10 = v2_hits_20 = v2_hits_50 = 0
        v2_total = 0

        for q in v2_questions:
            if not q.expected_signal_ids:
                continue
            v2_total += 1

            tg = []
            if q.entity_id:
                tg.append({"tags": [f"ent:{q.entity_id}"], "match": "all_strict"})
            if q.temporal_ym:
                tg.append({"tags": [q.temporal_ym], "match": "any_strict"})

            # Request enough results for @50 scoring
            rr = _run_recall(q.query, tg, 16000)
            expected = {f"sig:{sid}" for sid in q.expected_signal_ids}

            hit10, rank10 = _score_hit(rr, expected, 10)
            hit20, _      = _score_hit(rr, expected, 20)
            hit50, _      = _score_hit(rr, expected, 50)

            if hit10: v2_hits_10 += 1
            if hit20: v2_hits_20 += 1
            if hit50: v2_hits_50 += 1

            result.v2_per_question.append({
                "id": q.id,
                "query": q.query,
                "category": q.category,
                "is_specific": q.is_specific,
                "hit@10": hit10,
                "hit@20": hit20,
                "hit@50": hit50,
                "rank@10": rank10,
                "expected_count": len(q.expected_signal_ids),
                "top_doc_ids": [r.get("document_id", "") or "" for r in rr.get("results", [])[:5]],
            })
            time.sleep(0.05)

        result.v2_hits_10    = v2_hits_10
        result.v2_total      = v2_total
        result.v2_recall_at_10 = v2_hits_10 / max(v2_total, 1)
        result.v2_recall_at_20 = v2_hits_20 / max(v2_total, 1)
        result.v2_recall_at_50 = v2_hits_50 / max(v2_total, 1)

    # Cleanup: kill the server process group, then stop pg0, then remove pg0 data.
    # N5 fix: SIGKILL on the Hindsight group does NOT stop the detached pg0 postmaster.
    # Always attempt pg0 stop after killing the server.
    _kill_on_port(port)
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    # Stop all pg0 instances and delete HOME
    from olo.m3.pg0_lifecycle import safe_pg0_delete_home as _safe_del_done
    from olo.m3.pg0_lifecycle import Pg0StillRunningError as _PgErrDone
    try:
        _safe_del_done(pg0_home)
    except (_PgErrDone, Exception):
        pass  # best-effort; caller (gate test) will also attempt cleanup

    return result


def _kill_on_port(port: int) -> None:
    """Kill the process listening on port."""
    try:
        result = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True, text=True,
        )
        for m in re.finditer(r"pid=(\d+)", result.stdout):
            try:
                os.kill(int(m.group(1)), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass


def print_results(results: list[BenchmarkResult]) -> None:
    """Print benchmark results in a table."""
    print("\n" + "=" * 100)
    print("EMBEDDING MODEL BENCHMARK RESULTS — M3.0")
    print("=" * 100)
    print(f"{'Model':<45} {'R@10':>6} {'Idle PSS':>9} {'Steady PSS':>11} "
          f"{'Combined':>9} {'p50 lat':>8} {'p95 lat':>8}")
    print("-" * 100)
    for r in results:
        if r.error:
            print(f"{r.display:<45} ERROR: {r.error}")
            continue
        meets_quality = "✓" if r.recall_at_10 >= 0.90 else "✗"
        meets_ram = "✓" if r.combined_pss_mb <= 1024 else "✗"
        print(
            f"{r.display:<45} "
            f"{r.recall_at_10:>5.1%} {meets_quality} "
            f"{r.idle_pss_mb:>8.0f}M "
            f"{r.steady_pss_mb:>10.0f}M "
            f"{r.combined_pss_mb:>8.0f}M {meets_ram} "
            f"{r.p50_latency_ms:>7.0f}ms "
            f"{r.p95_latency_ms:>7.0f}ms"
        )
    print("-" * 100)
    print("R@10: recall@10 (≥90% required)")
    print("Idle PSS: Hindsight PSS after model load, before ingestion")
    print("Steady PSS: Hindsight PSS after ingestion + queries")
    print("Combined: Steady PSS + PostgreSQL PSS (1 GB BBC target)")

    # Recommendation
    passing = [r for r in results if r.error is None and r.recall_at_10 >= 0.90]
    if passing:
        best = min(passing, key=lambda r: r.combined_pss_mb)
        print(f"\nRECOMMENDATION: {best.display}")
        print(f"  Recall@10: {best.recall_at_10:.1%}  |  "
              f"Combined PSS: {best.combined_pss_mb:.0f} MB  |  "
              f"p50 latency: {best.p50_latency_ms:.0f} ms")
        if best.combined_pss_mb > 1024:
            print(f"  NOTE: Combined PSS {best.combined_pss_mb:.0f} MB exceeds 1 GB BBC target.")
            print("  BBC decides if this is acceptable or if additional capacity is needed.")
    print("=" * 100)
