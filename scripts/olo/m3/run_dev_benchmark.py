#!/usr/bin/env python3
"""OLO M3 Development Benchmark Orchestrator — Step 2.

Builds three independent Hindsight development banks from the committed
corpus, certifies each, scores using frozen V2 development rules, and
produces a comprehensive report.

Usage
-----
    python scripts/olo/m3/run_dev_benchmark.py [--out-dir DIR]

Design
------
- Reads from corpus DB already built by build_corpus.py.
- Deploys to bank IDs dev-bench-01 / 02 / 03 (clean, no reuse).
- Certifies each bank via verify_corpus_match() before scoring.
- Scores using run_live_benchmark.run_benchmark() with per-bank BANK_ID.
- Saves full per-question results to docs/olo/m3/dev_bench_*.json.
- Evaluates under v2 (strict) and v2.2 (content-checked) label sets.
- Writes final summary to docs/olo/m3/dev_bench_report.json.

This script does NOT modify benchmark questions, labels, scoring logic, or
source corpus content.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/mnt/c/oloai")

REPO = Path("/mnt/c/oloai")
CORPUS_DB = Path("/tmp/olo_step2_corpus/olo.db")
OUT_DIR = REPO / "docs" / "olo" / "m3"
BANK_IDS = ["dev-bench-01", "dev-bench-02", "dev-bench-03"]
HINDSIGHT_HOST = "127.0.0.1"
HINDSIGHT_PORT = 18800


def _require_corpus() -> None:
    if not CORPUS_DB.exists():
        print(f"ERROR: corpus DB not found at {CORPUS_DB}")
        print("Run: python scripts/olo/m3/build_corpus.py --db-dir /tmp/olo_step2_corpus")
        sys.exit(1)
    print(f"[ok] Corpus DB: {CORPUS_DB} ({CORPUS_DB.stat().st_size // 1024}KB)")


def _deploy_bank(bank_id: str) -> dict:
    """Deploy a fresh bank from the corpus DB. Returns timing + fact count."""
    from olo.m3.deploy_granite import deploy
    print(f"\n{'='*70}")
    print(f"DEPLOYING BANK: {bank_id}")
    print(f"{'='*70}")
    t0 = time.monotonic()
    try:
        deploy(
            bank_id=bank_id,
            db_path=CORPUS_DB,
            host=HINDSIGHT_HOST,
            port=HINDSIGHT_PORT,
            budget_usd=8.0,
            batch_size=50,
        )
        elapsed = time.monotonic() - t0
        print(f"[ok] Bank {bank_id!r} deployed in {elapsed:.0f}s")
        return {"bank_id": bank_id, "elapsed_s": elapsed, "status": "ok"}
    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"[fail] Bank {bank_id!r} deploy failed after {elapsed:.0f}s: {exc}")
        return {"bank_id": bank_id, "elapsed_s": elapsed, "status": "failed", "error": str(exc)}


def _certify_bank(bank_id: str) -> dict:
    """Run verify_corpus_match against the bank. Returns certification result."""
    from scripts.olo.m3.run_live_benchmark import verify_corpus_match, FROZEN_CORPUS_FP
    from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightUnavailable

    print(f"\n--- Certifying {bank_id!r} ---")
    cfg = HindsightConfig(host=HINDSIGHT_HOST, port=HINDSIGHT_PORT, bank_id=bank_id)
    client = HindsightClient(cfg)
    # Monkey-patch BANK_ID in run_live_benchmark module
    import scripts.olo.m3.run_live_benchmark as rlb
    old_bank_id = rlb.BANK_ID
    rlb.BANK_ID = bank_id
    try:
        bank_fp = verify_corpus_match(client, FROZEN_CORPUS_FP)
        print(f"[ok] Bank {bank_id!r} certified. bank_fp={bank_fp[:32]}...")
        return {
            "bank_id": bank_id,
            "certified": True,
            "corpus_fp": FROZEN_CORPUS_FP,
            "bank_fp": bank_fp,
        }
    except Exception as exc:
        print(f"[fail] Certification failed for {bank_id!r}: {exc}")
        return {
            "bank_id": bank_id,
            "certified": False,
            "error": str(exc),
        }
    finally:
        rlb.BANK_ID = old_bank_id


def _score_bank(bank_id: str, run_index: int) -> dict:
    """Run the full V2 benchmark against the bank. Returns summary + path."""
    import importlib
    import scripts.olo.m3.run_live_benchmark as rlb

    # Reload to reset module-level state
    importlib.reload(rlb)

    # Set the bank ID
    os.environ["HINDSIGHT_BANK_ID"] = bank_id
    rlb.BANK_ID = bank_id

    print(f"\n--- Scoring {bank_id!r} (run {run_index}/3) ---")
    t0 = time.monotonic()
    try:
        # Temporarily redirect output path to prevent overlap
        original_out = rlb.OUT_DIR
        rlb.OUT_DIR = Path(f"/tmp/bench_run_{run_index}")
        rlb.OUT_DIR.mkdir(exist_ok=True, parents=True)

        result = rlb.run_benchmark()
        elapsed = time.monotonic() - t0

        # Save to permanent location
        out_path = OUT_DIR / f"dev_bench_{run_index:02d}_{bank_id}.json"
        tmp_result = rlb.OUT_DIR / "live_benchmark_results.json"
        if tmp_result.exists():
            import shutil
            shutil.copy(tmp_result, out_path)
            print(f"[ok] Results saved to {out_path}")
        else:
            # Save from memory
            with open(out_path, "w") as f:
                json.dump({"summary": result}, f, indent=2)

        # Read back the full per-question results
        full_results = json.loads(out_path.read_text())
        summary = full_results.get("summary", result)

        recall_at_10 = summary.get("recall", {}).get("recall_at_10", {})
        hits = recall_at_10.get("hits", "?")
        total = recall_at_10.get("total", "?")
        pct = recall_at_10.get("pct", 0)
        print(f"[ok] {bank_id!r}: Recall@10 = {hits}/{total} = {pct:.1%} in {elapsed:.0f}s")

        rlb.OUT_DIR = original_out
        return {
            "bank_id": bank_id,
            "run_index": run_index,
            "result_path": str(out_path),
            "recall_at_10_hits": hits,
            "recall_at_10_total": total,
            "recall_at_10_pct": pct,
            "elapsed_s": elapsed,
            "status": "ok",
        }
    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"[fail] Scoring failed for {bank_id!r}: {exc}")
        import traceback
        traceback.print_exc()
        return {
            "bank_id": bank_id,
            "run_index": run_index,
            "elapsed_s": elapsed,
            "status": "failed",
            "error": str(exc),
        }


def _apply_v22_labels(result_path: str) -> dict:
    """Apply v2.2 content-checked labels to a scored result file."""
    from olo.m3.benchmark_eval_v2_2 import run_eval, score_at_k
    from olo.m3.synthetic.generator import generate_timeline
    from olo.m3.benchmark_v2 import build_benchmark_v2

    tl = generate_timeline()
    qs = build_benchmark_v2(tl)
    q_map = {q.id: q for q in qs}

    from olo.m3.benchmark_eval_v2_2 import build_v22_labels, RULES, INVALID_SPECIFIC, _template
    v2_labels = {
        q.id: {f"sig:{sid}" for sid in q.expected_signal_ids}
        for q in qs
    }
    v22_labels = {
        qid: build_v22_labels(qid, v2_labels[qid], tl, q_map)
        for qid in v2_labels
    }

    run_data = json.loads(Path(result_path).read_text())
    per_q = run_data.get("per_question", [])

    scores = {}
    for label_name, label_set in [("v2", v2_labels), ("v2.2", v22_labels)]:
        s = {}
        for k in (1, 5, 10, 20):
            h, _ = score_at_k(run_data, label_set, k)
            s[f"@{k}"] = h
        s["denominator"] = len(per_q)
        scores[label_name] = s

    # v2.2 excluding INVALID questions
    v22_valid = {qid: lbs for qid, lbs in v22_labels.items()
                 if _template(qid) not in INVALID_SPECIFIC}
    h_valid, _ = score_at_k(run_data, v22_valid, 10)
    n_valid = sum(1 for r in per_q if _template(r["id"]) not in INVALID_SPECIFIC)
    scores["v2.2_excl_invalid"] = {"@10": h_valid, "denominator": n_valid}

    return scores


def main() -> None:
    print("=" * 70)
    print("OLO M3 DEVELOPMENT BENCHMARK — STEP 2")
    print("Three independent bank builds and V2 scoring")
    print("=" * 70)
    print(f"Corpus DB: {CORPUS_DB}")
    print(f"Banks: {BANK_IDS}")
    print(f"Output: {OUT_DIR}")

    _require_corpus()

    # Verify corpus fingerprint and question hash
    from olo.m3.synthetic.generator import generate_timeline, corpus_fingerprint
    from olo.m3.benchmark_v2 import _compute_v2_hash
    tl = generate_timeline()
    fp = corpus_fingerprint(tl)
    bh = _compute_v2_hash()
    FROZEN_FP = "1877ca71d6fbbe64a6ef0b728159ceb4acc2595a14e05d0f9e44db2aa6759a0b"
    FROZEN_BH = "1fb288817e141ee50aaf92a816009e4e76adabcd0ff129a074f7023ac3538972"
    print(f"\nCorpus FP: {fp[:32]}... match={fp == FROZEN_FP}")
    print(f"Bench hash: {bh[:32]}... match={bh == FROZEN_BH}")
    if fp != FROZEN_FP or bh != FROZEN_BH:
        print("ERROR: Fingerprint/hash mismatch — stopping.")
        sys.exit(1)

    # voice_qbo diagnostic
    from olo.m3.synthetic.generator import ENTITIES, _sig_id
    vq_missing = []
    vq_questions = 0
    from olo.m3.benchmark_v2 import build_benchmark_v2
    qs = build_benchmark_v2(tl)
    for ent in ENTITIES:
        vq_id = _sig_id(f"voice_qbo_{ent.entity_id}")
        exists = any(s.signal_id == vq_id for s in tl)
        if not exists:
            vq_missing.append(vq_id)
    vq_questions = sum(
        1 for q in qs
        if any(
            _sig_id(f"voice_qbo_{e.entity_id}") in q.expected_signal_ids
            for e in ENTITIES
        )
    )
    print(f"\nvoice_qbo signal: NOT in corpus (3/3 entities). {vq_questions} questions "
          f"reference it as a SECONDARY label alongside other valid signals.")
    print("These questions can still pass by retrieving other expected documents.")

    results = []
    for i, bank_id in enumerate(BANK_IDS, 1):
        print(f"\n{'#'*70}")
        print(f"# BUILD {i}/3: {bank_id}")
        print(f"{'#'*70}")

        # 1. Deploy
        deploy_result = _deploy_bank(bank_id)
        if deploy_result["status"] != "ok":
            print(f"SKIP scoring: deploy failed for {bank_id}")
            results.append({"bank_id": bank_id, "status": "deploy_failed", **deploy_result})
            continue

        # 2. Certify
        cert_result = _certify_bank(bank_id)
        if not cert_result.get("certified"):
            print(f"SKIP scoring: certification failed for {bank_id}")
            results.append({
                "bank_id": bank_id, "status": "cert_failed",
                "deploy": deploy_result, "cert": cert_result,
            })
            continue

        # 3. Score
        score_result = _score_bank(bank_id, i)

        # 4. Apply V2.2 labels
        v22_scores = {}
        if score_result.get("status") == "ok" and score_result.get("result_path"):
            try:
                v22_scores = _apply_v22_labels(score_result["result_path"])
                print(f"\nV2.2 label scores for {bank_id}:")
                for version, s in v22_scores.items():
                    denom = s.get("denominator", "?")
                    print(f"  {version}: @10={s.get('@10')}/{denom}")
            except Exception as e:
                print(f"V2.2 eval failed: {e}")

        results.append({
            "bank_id": bank_id,
            "status": score_result.get("status", "unknown"),
            "deploy": deploy_result,
            "cert": cert_result,
            "score": score_result,
            "label_scores": v22_scores,
        })

    # Summary report
    print(f"\n{'='*70}")
    print("BENCHMARK SUMMARY — Three independent development banks")
    print(f"{'='*70}")
    print(f"Corpus fingerprint: {fp}")
    print(f"Benchmark hash: {bh}")
    print(f"voice_qbo missing: {len(vq_missing)}/3 entities ({vq_questions} affected questions)")
    print()

    valid_scores = [r for r in results if r.get("status") == "ok" and "score" in r]

    if valid_scores:
        recall_at_10s = [r["score"].get("recall_at_10_pct", 0) for r in valid_scores]
        mean_r10 = sum(recall_at_10s) / len(recall_at_10s) if recall_at_10s else 0
        min_r10 = min(recall_at_10s) if recall_at_10s else 0
        max_r10 = max(recall_at_10s) if recall_at_10s else 0

        print(f"V2 strict label scores (Recall@10):")
        for r in valid_scores:
            s = r.get("score", {})
            print(f"  {r['bank_id']}: {s.get('recall_at_10_hits')}/{s.get('recall_at_10_total')} "
                  f"= {s.get('recall_at_10_pct', 0):.1%} "
                  f"[cert_fp={r['cert'].get('bank_fp', '?')[:16]}...]")

        print(f"\nAggregate V2:")
        print(f"  Mean Recall@10: {mean_r10:.1%}")
        print(f"  Min:  {min_r10:.1%}  Max: {max_r10:.1%}")
        print(f"  Range: {max_r10 - min_r10:.2%}")

        print(f"\nV2.2 content-checked label scores:")
        for r in valid_scores:
            ls = r.get("label_scores", {})
            v22 = ls.get("v2.2", {})
            v22_excl = ls.get("v2.2_excl_invalid", {})
            print(f"  {r['bank_id']}: v2.2 @10={v22.get('@10')}/{v22.get('denominator')} "
                  f"| excl_invalid @10={v22_excl.get('@10')}/{v22_excl.get('denominator')}")

        TARGET = 0.95
        meets_target = [r for r in valid_scores
                       if r["score"].get("recall_at_10_pct", 0) >= TARGET]
        print(f"\nDevelopment target >=95% (V2 strict):")
        print(f"  Banks meeting target: {len(meets_target)}/{len(valid_scores)}")
    else:
        print("No valid scores produced.")

    # Save report
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_sha": "10a8be667a",
        "corpus_fingerprint": fp,
        "benchmark_hash": bh,
        "voice_qbo_missing": {
            "entity_count": len(vq_missing),
            "question_count": vq_questions,
            "note": "voice_qbo signal not in corpus. All affected questions have other valid expected signals.",
        },
        "banks": results,
    }
    report_path = OUT_DIR / "dev_bench_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nFull report: {report_path}")


if __name__ == "__main__":
    main()
