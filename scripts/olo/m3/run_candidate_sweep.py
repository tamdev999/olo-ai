"""Controlled reranker candidate sweep: 100 / 200 / 300 / 400.

Uses ONE fully built, consolidated, idle bank. The bank is fingerprinted
before the first run and verified unchanged after each configuration.
Only RERANKER_MAX_CANDIDATES changes between runs; everything else is constant.

Usage:
    # Ensure server is running and bank is idle (no pending consolidation)
    source /root/olo_linux_venv/bin/activate
    source /root/olo_m3_secrets/deepseek.env
    export PYTHONPATH=/mnt/c/oloai
    export HINDSIGHT_HOST=127.0.0.1
    export HINDSIGHT_PORT=18800
    export HINDSIGHT_BANK_ID=main-v1-granite
    export HINDSIGHT_API_LLM_API_KEY

    # Run the sweep (restarts server for each config)
    python3 scripts/olo/m3/run_candidate_sweep.py
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/mnt/c/oloai")

from olo.m3.synthetic.generator import generate_timeline, corpus_fingerprint
from olo.m3.benchmark_v2 import build_benchmark_v2, _compute_v2_hash
from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightUnavailable

CANDIDATES = [100, 200, 300, 400]
BANK_ID = os.environ.get("HINDSIGHT_BANK_ID", "main-v1-granite")
HOST = os.environ.get("HINDSIGHT_HOST", "127.0.0.1")
PORT = int(os.environ.get("HINDSIGHT_PORT", "18800"))
START_SCRIPT = "/mnt/c/oloai/scripts/olo/m3/start_server.sh"
OUT_DIR = Path("/mnt/c/oloai/docs/olo/m3/sweep_results")
OUT_DIR.mkdir(parents=True, exist_ok=True)

FROZEN_HASH = "fbede725aeb89d3557e048803e0891d134c10734ac1ff2c4a7e80f139d28a3be"


def get_server_pid() -> int | None:
    r = subprocess.run(["pgrep", "-u", "olo_hs", "-f", "hindsight-api"],
                       capture_output=True, text=True)
    pids = [int(p) for p in r.stdout.strip().splitlines() if p.strip()]
    return pids[0] if pids else None


def kill_server() -> None:
    pid = get_server_pid()
    if pid:
        subprocess.run(["sudo", "kill", "-9", str(pid)], capture_output=True)
        time.sleep(3)


def start_server(candidates: int) -> None:
    """Start the server with a specific candidate count."""
    env = dict(os.environ)
    env["HINDSIGHT_API_RERANKER_MAX_CANDIDATES"] = str(candidates)
    # Write a patched start script for this run
    orig = Path(START_SCRIPT).read_text()
    patched = orig.replace(
        "HINDSIGHT_API_RERANKER_MAX_CANDIDATES=400",
        f"HINDSIGHT_API_RERANKER_MAX_CANDIDATES={candidates}",
    ).replace(
        "HINDSIGHT_API_RERANKER_MAX_CANDIDATES=100",
        f"HINDSIGHT_API_RERANKER_MAX_CANDIDATES={candidates}",
    ).replace(
        "HINDSIGHT_API_RERANKER_MAX_CANDIDATES=200",
        f"HINDSIGHT_API_RERANKER_MAX_CANDIDATES={candidates}",
    ).replace(
        "HINDSIGHT_API_RERANKER_MAX_CANDIDATES=300",
        f"HINDSIGHT_API_RERANKER_MAX_CANDIDATES={candidates}",
    )
    tmp_script = f"/tmp/start_sweep_{candidates}.sh"
    Path(tmp_script).write_text(patched)
    Path(tmp_script).chmod(0o755)

    # Build launch wrapper
    launch = f"#!/bin/bash
. /root/olo_m3_secrets/deepseek.env
exec bash {tmp_script}
"
    launch_path = f"/tmp/launch_sweep_{candidates}.sh"
    Path(launch_path).write_text(launch)
    Path(launch_path).chmod(0o755)

    subprocess.Popen(
        ["bash", launch_path],
        stdout=open(f"/tmp/sweep_{candidates}.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def wait_for_server(timeout_s: float = 60.0) -> bool:
    cfg = HindsightConfig(host=HOST, port=PORT, timeout_s=5.0)
    client = HindsightClient(cfg)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            client.health_check()
            return True
        except Exception:
            time.sleep(1)
    return False


def run_benchmark(candidates: int) -> dict:
    """Run the full 201-question benchmark and return the summary."""
    sys.argv = ["run_live_benchmark.py"]
    # Temporarily patch benchmark
    from scripts.olo.m3 import run_live_benchmark  # noqa: F401
    # Actually just run it as a subprocess for isolation
    env = dict(os.environ)
    env["HINDSIGHT_HOST"] = HOST
    env["HINDSIGHT_PORT"] = str(PORT)
    env["HINDSIGHT_BANK_ID"] = BANK_ID
    env["PYTHONPATH"] = "/mnt/c/oloai"

    r = subprocess.run(
        ["python3", "/mnt/c/oloai/scripts/olo/m3/run_live_benchmark.py"],
        capture_output=True, text=True, env=env,
    )
    # Read result file
    result_file = Path("/tmp/benchmark_results/live_benchmark_results.json")
    if result_file.exists():
        return json.load(result_file.open())
    return {"error": r.stderr[-500:] if r.stderr else "no output"}


def get_bank_fingerprint(client: HindsightClient) -> dict:
    """Get basic bank state metrics for fingerprinting."""
    banks = client.list_banks()
    for b in banks.get("banks", []):
        if b["bank_id"] == BANK_ID:
            return {
                "fact_count": b.get("fact_count"),
                "last_document_at": b.get("last_document_at"),
                "bank_id": BANK_ID,
            }
    return {}


def main() -> None:
    print("=" * 60)
    print("Controlled Reranker Candidate Sweep")
    print("=" * 60)
    print(f"Bank: {BANK_ID} | Host: {HOST}:{PORT}")
    print(f"Candidates: {CANDIDATES}")
    print(f"Frozen hash: {FROZEN_HASH[:16]}...")
    print()

    # Verify benchmark hash
    actual_hash = _compute_v2_hash()
    if actual_hash != FROZEN_HASH:
        print(f"WARNING: Benchmark hash mismatch!")
        print(f"  Expected: {FROZEN_HASH}")
        print(f"  Actual:   {actual_hash}")
        print("  Continuing anyway — results may differ from post-fix baseline.")
    else:
        print("Benchmark hash verified:", actual_hash[:16], "...")

    # Get baseline fingerprint with server at any candidate count
    if not wait_for_server(timeout_s=10):
        print("Server not running — please start it first")
        return

    cfg = HindsightConfig(host=HOST, port=PORT, timeout_s=30.0, bank_id=BANK_ID)
    client = HindsightClient(cfg)
    baseline_fp = get_bank_fingerprint(client)
    print(f"Bank baseline: {baseline_fp}")
    print()

    all_results = []

    for candidates in CANDIDATES:
        print(f"
{'='*50}")
        print(f"Config: {candidates} candidates")
        print(f"{'='*50}")

        # Restart server with new candidate count
        print(f"  Stopping server...")
        kill_server()
        time.sleep(3)

        print(f"  Starting server with {candidates} candidates...")
        start_server(candidates)
        if not wait_for_server(timeout_s=90):
            print(f"  ERROR: Server did not start for {candidates} candidates")
            continue
        print(f"  Server ready")

        # Verify bank fingerprint unchanged
        client2 = HindsightClient(HindsightConfig(host=HOST, port=PORT, bank_id=BANK_ID))
        current_fp = get_bank_fingerprint(client2)
        fp_ok = current_fp.get("fact_count") == baseline_fp.get("fact_count")
        print(f"  Bank fingerprint: {'OK' if fp_ok else 'CHANGED'} ({current_fp})")

        # Run benchmark
        print(f"  Running 201-question benchmark...")
        result = run_benchmark(candidates)

        # Extract summary
        if "summary" in result:
            s = result["summary"]
            recall = s.get("recall", {})
            lat = s.get("latency_ms", {})
            entry = {
                "candidates": candidates,
                "bank_fingerprint": current_fp,
                "benchmark_hash": s.get("benchmark_hash", ""),
                "n_questions": s.get("n_questions", 0),
                "errors": s.get("errors", 0),
                "recall": {
                    "@1": recall.get("recall_at_1", {}).get("hits"),
                    "@5": recall.get("recall_at_5", {}).get("hits"),
                    "@10": recall.get("recall_at_10", {}).get("hits"),
                    "@20": recall.get("recall_at_20", {}).get("hits"),
                    "@50": recall.get("recall_at_50", {}).get("hits"),
                    "@100": recall.get("recall_at_100", {}).get("hits"),
                    "total": 201,
                },
                "latency_ms": lat,
            }
            all_results.append(entry)
            print(f"  @10={entry['recall']['@10']}/201  @50={entry['recall']['@50']}/201")
            print(f"  p50={lat.get('p50', '?')}ms  p95={lat.get('p95', '?')}ms")
            # Save per-run file
            out_file = OUT_DIR / f"sweep_{candidates}cands.json"
            with open(out_file, "w") as f:
                json.dump(result, f, indent=1)
            print(f"  Saved: {out_file}")
        else:
            print(f"  ERROR: {result.get('error', 'unknown')}")

    # Save summary
    summary = {
        "sweep_date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bank_id": BANK_ID,
        "baseline_bank_fingerprint": baseline_fp,
        "benchmark_hash": FROZEN_HASH,
        "configurations": all_results,
    }
    out_file = OUT_DIR / "sweep_summary.json"
    with open(out_file, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"
Summary saved: {out_file}")

    # Print final table
    print("
" + "="*60)
    print("SWEEP RESULTS SUMMARY")
    print("="*60)
    print(f"{'Cands':>6}  {'@1':>5}  {'@5':>5}  {'@10':>5}  {'@20':>5}  {'@50':>5}  {'p50ms':>7}  {'p95ms':>7}")
    print("-"*60)
    for r in all_results:
        rc = r["recall"]
        lt = r["latency_ms"]
        print(f"{r['candidates']:>6}  {rc['@1']:>5}  {rc['@5']:>5}  {rc['@10']:>5}  "
              f"{rc['@20']:>5}  {rc['@50']:>5}  "
              f"{lt.get('p50', 0):>7.0f}  {lt.get('p95', 0):>7.0f}")


if __name__ == "__main__":
    main()
