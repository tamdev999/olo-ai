"""Olo M3 benchmark evaluator v2.1 — from Opus independent audit 2026-11-22.

Provides three label sets for scoring committed per-question retrieval results.

  v2       Historical frozen labels — these came from the third generate_timeline()
           call in a process (generator RNG bug, Opus audit §4.1). These are the
           labels that were in the frozen hash e0cbbd98... The labels are read
           directly from the per-question expected_signal_ids in the committed
           result files rather than regenerated, to preserve the historical record.
           These labels are INVALIDATED — documented as historical reference only.

  v2.1a    Corpus-aligned corrected labels: same generator intent predicates,
           applied to the INGESTED corpus (first generate_timeline() call), with
           the arbitrary [:N] truncations removed (design rule 4 of benchmark_v2.py:
           "all signals of the same intent type are labelled").
           Uses the fixed generator with per-call fresh RNG (Opus audit §4.1 fix).

  v2.1b    v2.1a plus 12 entity-independent sibling rules for questions whose
           wording admits every member of a template family.  Rules written from
           query wording, applied identically to all 3 entities.
           PENDING BBC/Opus review before being declared the official baseline.

Scoring is document-level on the committed top-20 retrieved lists.
Observations (entries with no document_id) are NOT credited here — this is
a document-level lower bound.

Usage
-----
    python3 olo/m3/benchmark_eval_v2_1.py <repo_root>

Outputs scores to stdout and saves full results to
    docs/olo/m3/eval_v2_1_result.json

Source: Opus audit eval_v21.py, adapted for repo layout.
"""
from __future__ import annotations

import json
import re
import sys
import collections
from pathlib import Path
from typing import Optional


def _subj(s) -> str:
    return (s.subject or "").lower()


# ── Sibling rules (v2.1b only) ────────────────────────────────────────────────
SIBLING_RULES: dict[str, tuple[str, object]] = {
    "inv_03": (
        "Any invoice-dispute email states a charged vs agreed price; "
        "'the invoice that X disputed' is ambiguous in a corpus with many disputes",
        lambda s: "invoice" in _subj(s) and ("dispute" in _subj(s) or "discrepancy" in _subj(s)),
    ),
    "inv_04": (
        "Every dispute email/call names an invoice ID",
        lambda s: "invoice" in _subj(s) and ("dispute" in _subj(s) or "discrepancy" in _subj(s)),
    ),
    "xent_2": (
        "'What invoice amount did X dispute' — every dispute email/call states the amount",
        lambda s: "invoice" in _subj(s) and ("dispute" in _subj(s) or "discrepancy" in _subj(s)),
    ),
    "mtg_02": (
        "Contact asked to change a meeting date: any reschedule-request email",
        lambda s: "reschedule" in _subj(s) and s.kind == "email.received",
    ),
    "con_06": (
        "Schedule changes requested by X: any reschedule request",
        lambda s: "reschedule" in _subj(s),
    ),
    "del_01": (
        "Requested to reschedule a meeting or delivery: any reschedule request",
        lambda s: "reschedule" in _subj(s),
    ),
    "xent_1": (
        "'The meeting rescheduling request from <contact>' — every reschedule request qualifies",
        lambda s: "reschedule" in _subj(s),
    ),
    "rel_02": (
        "Formal complaint: any complaint email",
        lambda s: "complaint" in _subj(s),
    ),
    "rel_05": (
        "Service issues reported: any complaint email",
        lambda s: "complaint" in _subj(s),
    ),
    "rel_07": (
        "Emails the contact sent us: any received email of that entity",
        lambda s: s.kind == "email.received",
    ),
    "mtg_01": (
        "All meeting-related communications: calendar events + meeting emails",
        lambda s: (
            s.kind == "calendar.event.created"
            or "meeting" in _subj(s)
            or "reschedule" in _subj(s)
        ),
    ),
    "mtg_06": (
        "Calendar events created: any calendar.event.created",
        lambda s: s.kind == "calendar.event.created",
    ),
}


def _template(qid: str) -> str:
    if qid.startswith("v2_xent_"):
        return "xent_" + qid.rsplit("_", 1)[1]
    return re.sub(r"^v2_ent_01\w\w_", "", qid)


def build_labels(
    version: str,
    q_id: str,
    historical_labels: dict,  # {q_id: set[str]} — from committed files (v2 historical)
    corrected_labels: dict,   # {q_id: set[str]} — from fixed generator (v2.1a)
    timeline_sigs: list,
) -> set[str]:
    """Build the label set for a question under a given evaluation version."""
    if version == "v2":
        # Historical: use committed expected_signal_ids from the result files.
        return historical_labels.get(q_id, set())
    else:
        # Corrected: use corpus-aligned labels from the fixed generator.
        base = corrected_labels.get(q_id, set())
        if version == "v2.1b":
            rule = SIBLING_RULES.get(_template(q_id))
            if rule:
                m = re.search(r"ent_01\w\w", q_id)
                if m:
                    ent_prefix = m.group(0)
                    base = base | {
                        f"sig:{s.signal_id}"
                        for s in timeline_sigs
                        if s.entity_id and ent_prefix in s.entity_id and rule[1](s)
                    }
        return base


def score_run(
    run: dict,
    version: str,
    k: int,
    historical_labels: dict,
    corrected_labels: dict,
    timeline_sigs: list,
) -> tuple[int, dict]:
    """Score a retrieval run at @k. Returns (hits, {q_id: first_rank})."""
    hits = 0
    rows = {}
    for r in run["per_question"]:
        lab = build_labels(version, r["id"], historical_labels, corrected_labels, timeline_sigs)
        rank = next(
            (h["rank"] for h in r.get("retrieved_top20", []) if h.get("document_id") in lab),
            -1,
        )
        ok = 0 < rank <= k
        hits += ok
        rows[r["id"]] = rank
    return hits, rows


def run_eval(repo: str) -> dict:
    """Run the full v2/v2.1a/v2.1b evaluation and return the result dict."""
    sys.path.insert(0, repo)
    from olo.m3.synthetic.generator import generate_timeline
    from olo.m3.benchmark_v2 import build_benchmark_v2

    # Fixed generator: first call always returns the ingested corpus.
    TL = generate_timeline()

    # v2.1a corrected labels: from the fixed generator (truncations already removed
    # from benchmark_v2.py by Opus audit fix).
    qs_corrected = build_benchmark_v2(TL)
    corrected_labels = {
        q.id: {f"sig:{sid}" for sid in q.expected_signal_ids}
        for q in qs_corrected
    }

    runs = {
        # Historical pre-fix results (bank built with generator RNG bug + old labels):
        "A_100_hist": f"{repo}/docs/olo/m3/benchmark_final_m3.json",
        "B_400_hist": f"{repo}/docs/olo/m3/benchmark_config_b_400cands.json",
        # Corrected post-fix result (generator RNG fixed, labels corrected, bank rebuilt):
        "C_400_corrected": f"{repo}/docs/olo/m3/benchmark_corrected_v2_1.json",
    }

    out: dict = {
        "evaluator_version": "v2.1",
        "evaluator_provenance": "Opus independent audit 2026-11-22; adapted for repo",
        "label_note": (
            "v2 labels are INVALIDATED (Opus audit §4.1: generator RNG bug caused "
            "bank, frozen hash, and scorer to use 3 different corpora). "
            "Kept as historical reference only. "
            "v2.1a is the corpus-aligned corrected baseline. "
            "v2.1b rules are PENDING BBC/Opus review before official use."
        ),
        "label_sizes": {},
        "scores": {},
        "sibling_rules": {k: v[0] for k, v in SIBLING_RULES.items()},
        "per_question_first_rank": collections.defaultdict(dict),
    }

    # Pre-compute label sizes from the corrected set (v2 historical sizes vary by run file).
    out["label_sizes"]["v2.1a"] = sum(len(v) for v in corrected_labels.values())

    for run_name, fpath in runs.items():
        p = Path(fpath)
        if not p.exists():
            print(f"  SKIP {run_name}: {fpath} not found")
            continue

        run_data = json.load(p.open())
        per_q = run_data.get("per_question", [])

        # Historical v2 labels: read from committed expected field in this file.
        historical_labels = {
            r["id"]: set(r.get("expected", []))
            for r in per_q
        }

        committed_at10 = sum(1 for r in per_q if r.get("pass_at", {}).get("10"))
        out["scores"][run_name] = {
            "committed_pass@10": committed_at10,
            "note": "committed pass@10 uses the original frozen-hash labels (v2)",
        }

        for version in ("v2", "v2.1a", "v2.1b"):
            out["scores"][run_name][version] = {}
            for k_val in (1, 5, 10, 20, 50):
                h, _ = score_run(
                    run_data, version, k_val,
                    historical_labels, corrected_labels, TL,
                )
                out["scores"][run_name][version][f"@{k_val}"] = h

        # Save first ranks at @20 for per-question analysis
        _, rows_20 = score_run(run_data, "v2.1b", 20, historical_labels, corrected_labels, TL)
        for q_id, rk in rows_20.items():
            out["per_question_first_rank"][q_id][run_name] = rk

    return dict(out)


if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else "/mnt/c/oloai"
    result = run_eval(repo)

    out_path = Path(f"{repo}/docs/olo/m3/eval_v2_1_result.json")
    with open(out_path, "w") as f_out:
        json.dump(result, f_out, indent=1)
    print(f"Full results saved to {out_path}")

    # Print summary
    print("\n=== Evaluation v2.1 Summary ===")
    print(f"Note: {result['label_note'][:120]}...")
    for run_name, scores in result["scores"].items():
        print(f"\n{run_name} (committed @10={scores['committed_pass@10']}):")
        for version in ("v2", "v2.1a", "v2.1b"):
            if version in scores:
                s = scores[version]
                print(f"  {version}: @10={s.get('@10')} @20={s.get('@20')} @50={s.get('@50')}")
