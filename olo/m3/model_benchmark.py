"""Olo M3 — Embedding model benchmark.

Compares E5-small fp32, E5-small int8, and IBM Granite R2 int8 on:
  - V1 recall@10 (corrected benchmark, no answer leakage)
  - Idle PSS (after model load, before ingestion)
  - Steady-state PSS (after ingestion and queries)
  - Peak PSS during ingestion
  - Per-query latency
  - Per-document ingestion throughput

Each model runs in an isolated Hindsight instance with an isolated bank.
PostgreSQL PSS is measured separately and reported as combined total.

No shared state between models. No PyTorch required.
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
    recall_at_10: float
    hits: int
    total: int
    idle_pss_mb: float = 0.0
    steady_pss_mb: float = 0.0
    peak_pss_mb: float = 0.0
    pg_pss_mb: float = 0.0
    combined_pss_mb: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    ingestion_docs_per_s: float = 0.0
    error: Optional[str] = None
    per_question: list[dict] = field(default_factory=list)


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
) -> BenchmarkResult:
    """Run V1 benchmark for one embedding model on an isolated Hindsight instance.

    Args:
        model_cfg:        Entry from MODEL_CONFIGS.
        db_path:          Populated olo.db (source of truth; not modified).
        hindsight_bin:    Path to hindsight-api binary.
        port:             Port for this Hindsight instance.
        pg0_home:         HOME directory for the olo_hs process (pg0 storage).
        timeout_startup:  Seconds to wait for Hindsight to become healthy.

    Returns BenchmarkResult with all measured metrics.
    """
    from olo.m3.hindsight.client import (  # noqa: PLC0415
        HindsightClient, HindsightConfig, HindsightUnavailable, HindsightAPIError,
    )
    from olo.m3.synthetic.generator import build_labelled_questions
    from olo.m3.feeder import build_hindsight_items

    model_id = model_cfg["id"]
    display = model_cfg["display"]
    bank_id = f"bench-{model_id}"
    result = BenchmarkResult(model_id=model_id, display=display,
                             recall_at_10=0.0, hits=0, total=0)

    # Base env for Hindsight subprocess
    socket_dir = str(pg0_home / "run")
    subprocess.run(
        ["sudo", "-u", "olo_hs", "mkdir", "-p", socket_dir],
        check=False, capture_output=True,
    )
    base_env = {
        "HOME": str(pg0_home),
        "HF_HOME": "/var/cache/huggingface",
        # Offline mode: models are pre-downloaded, avoid lock-file writes to
        # /var/cache/huggingface/hub/.locks/ (owned by root; olo_hs can't create).
        "HF_HUB_OFFLINE": "1",
        # Route pg0 sockets into the per-model home — never /tmp
        "HINDSIGHT_API_DATABASE_URL": f"pg0://bench-{model_id}?unix_socket_directories={socket_dir}",
        "HINDSIGHT_API_HOST": "127.0.0.1",
        "HINDSIGHT_API_PORT": str(port),
        "HINDSIGHT_API_LLM_PROVIDER": "none",
        "HINDSIGHT_API_DECISION_MODEL_PROVIDER": "none",
        "HINDSIGHT_API_MCP_ENABLED": "false",
        "HINDSIGHT_API_RERANKER_PROVIDER": "rrf",
        "HINDSIGHT_API_STORE_DOCUMENT_TEXT": "true",
        "OTEL_SDK_DISABLED": "true",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        **model_cfg["env"],
    }

    # Start Hindsight as olo_hs (pg0 requires non-root for initdb)
    env_list = [f"{k}={v}" for k, v in base_env.items()]
    proc = subprocess.Popen(
        ["sudo", "-u", "olo_hs", "env"] + env_list + [hindsight_bin],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    cfg = HindsightConfig(host="127.0.0.1", port=port, bank_id=bank_id)
    client = HindsightClient(cfg)

    try:
        client.wait_until_ready(timeout_s=timeout_startup)
    except HindsightUnavailable as exc:
        result.error = f"Startup timeout: {exc}"
        proc.kill()
        return result

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
        proc.kill()
        return result

    # Ingest (prose + structured, no LLM)
    t_ingest_start = time.monotonic()
    prose = [i for i in items if i.get("strategy") == "prose"]
    structured = [i for i in items if i.get("strategy") != "prose"]
    batch_size = 50
    for label, batch_list in [("prose", prose), ("structured", structured)]:
        for start in range(0, len(batch_list), batch_size):
            batch = batch_list[start:start + batch_size]
            op_id = str(_uuid.uuid5(
                _uuid.NAMESPACE_OID, f"bench-{model_id}-{label}-{start}"
            ))
            client.retain(batch, operation_id=op_id, async_=False, bank_id=bank_id)
    ingest_time_s = time.monotonic() - t_ingest_start
    result.ingestion_docs_per_s = len(items) / max(ingest_time_s, 0.001)

    # Measure steady-state PSS after ingestion
    if pid:
        result.steady_pss_mb = _measure_pss(pid)
        result.peak_pss_mb = max(result.idle_pss_mb, result.steady_pss_mb)
        result.combined_pss_mb = result.steady_pss_mb + result.pg_pss_mb

    # Run V1 benchmark
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
        try:
            recall_result = client.recall(
                q["query"],
                tag_groups=tg if tg else None,
                max_tokens=4000,
                trace=True,
                bank_id=bank_id,
            )
        except HindsightUnavailable:
            time.sleep(5)
            try:
                recall_result = client.recall(
                    q["query"],
                    tag_groups=tg if tg else None,
                    max_tokens=4000,
                    bank_id=bank_id,
                )
            except Exception as exc:
                result.error = f"Recall error: {exc}"
                break
        latencies.append((time.monotonic() - t0) * 1000)

        expected = {f"sig:{sid}" for sid in q["expected_signal_ids"]}
        hit = False
        for r in recall_result.get("results", [])[:10]:
            doc_id = r.get("document_id", "") or ""
            if doc_id in expected:
                hit = True
                break
            for sfid in r.get("source_fact_ids", []) or []:
                sid = sfid.get("document_id", "") if isinstance(sfid, dict) else str(sfid)
                if sid in expected:
                    hit = True
                    break
            if hit:
                break

        if hit:
            hits += 1
        result.per_question.append({
            "id": q["id"],
            "query": q["query"],
            "hit": hit,
            "expected": list(expected)[:1],
            "top_doc_ids": [
                r.get("document_id", "") or ""
                for r in recall_result.get("results", [])[:5]
            ],
        })

        time.sleep(0.1)  # brief pause between queries

    result.hits = hits
    result.total = len(scored)
    result.recall_at_10 = hits / max(len(scored), 1)

    if latencies:
        sorted_lat = sorted(latencies)
        result.p50_latency_ms = sorted_lat[len(sorted_lat) // 2]
        result.p95_latency_ms = sorted_lat[int(len(sorted_lat) * 0.95)]

    # Cleanup: kill the server process group, then remove pg0 data
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
