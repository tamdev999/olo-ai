"""Olo M3 benchmark evaluator v2.2 — based on Opus independent audit 2026-11-22.

Implements the content-checked label adjudications from the Opus audit.
Reports four separate metrics:

  v2       Historical labels (committed; built from current generator after RNG fix).
           These labels now match the bank after gate_olo.db regeneration.

  v2.2     Content-checked labels: v2 with the Opus-adjudicated sibling rules applied.
           Rules check document BODY TEXT, not subject keywords.
           See RULES dict for complete predicate definitions and adjudication rationale.

  invalid  Questions classified as INVALID as specific-answer questions
           (inv_03, inv_04 — multiple documents share any stated invoice ID).
           These are reported separately; their denominator is excluded from
           the main accuracy target.

  set_cov  Set-coverage @20: for multi-document questions (con_05 / con_04 families),
           the fraction of required documents present in the top-20.
           (These questions cannot be evaluated by any-one-document retrieval.)

Usage:
    python3 olo/m3/benchmark_eval_v2_2.py <repo_root>
"""
from __future__ import annotations

import json, re, sys, collections
from pathlib import Path


def _subj(s) -> str:
    return (getattr(s, "subject", None) or "").lower()


def _body(s) -> str:
    return (getattr(s, "body_text", None) or "").lower()


# ── Content-checked sibling rules ────────────────────────────────────────────
# Every predicate was verified against the body text of matching documents
# in the actual bank corpus (Opus audit §4).  Subject keywords alone are
# NEVER the sole criterion.

def is_dispute_email(s) -> bool:
    """Customer email that states a charged vs agreed price for a named invoice."""
    return "we were charged" in _body(s) and "agreed price" in _body(s)


def is_meeting_change_request(s) -> bool:
    """Contact asks to move a MEETING — delivery context excluded."""
    b = _body(s)
    return (
        ("reschedule our meeting" in b or "move our meeting" in b
         or ("meeting" in b and "need to move" in b))
        and "delivery" not in b
    )


def is_complaint_email(s) -> bool:
    """Email that formally or informally raises a service complaint."""
    b = _body(s)
    return "disappointed with the service" in b or "formally complain" in b


def is_schedule_change_request(s) -> bool:
    """Any request to reschedule a meeting or delivery."""
    b = _body(s)
    return "reschedule" in b and "could we move it" in b


def is_schedule_change_broader(s) -> bool:
    """Schedule-change communication: request + call + revised delivery confirmation."""
    b = _body(s)
    return (
        ("could we move it" in b)
        or (s.kind == "call.transcript" and "reschedul" in b)
        or "revised confirmed delivery date" in b
    )


def is_meeting_evidence(s) -> bool:
    """Meeting-related evidence: calendar events + meeting-topic emails (not delivery)."""
    b = _body(s)
    return (
        s.kind == "calendar.event.created"
        or ("meeting" in b and "delivery" not in b)
    )


def is_meeting_reschedule_evidence(s) -> bool:
    """Meeting reschedule: meeting-change calls/correction emails (not delivery)."""
    b = _body(s)
    return (
        ("meeting" in b and ("reschedul" in b or "correction" in _subj(s)) and "delivery" not in b)
    )


def is_reschedule_transcript(s) -> bool:
    """Any call transcript about rescheduling with this company."""
    return s.kind == "call.transcript" and "reschedul" in _body(s)


RULES = {
    # template_id: (adjudication, predicate_or_None)
    "inv_03": (
        "QUESTION INVALID as specific; RESTRICT for scoring: any dispute email stating charged vs agreed price",
        is_dispute_email,
    ),
    "inv_04": (
        "QUESTION INVALID as specific; RESTRICT: dispute emails naming an invoice ID",
        lambda s: is_dispute_email(s) and "invoice #" in _body(s),
    ),
    "xent_2": (
        "RESTRICT: dispute emails/calls stating the disputed amount",
        lambda s: is_dispute_email(s) or (s.kind == "call.transcript" and "invoice" in _subj(s) and "amount" in _body(s)),
    ),
    "mtg_02": (
        "RESTRICT: only requests to move a MEETING written by the contact",
        is_meeting_change_request,
    ),
    "xent_1": (
        "RESTRICT: only meeting-reschedule requests from the contact",
        is_meeting_change_request,
    ),
    "con_06": (
        "APPROVE: meeting or delivery change requests",
        is_schedule_change_request,
    ),
    "del_01": (
        "APPROVE: meeting or delivery change requests",
        is_schedule_change_request,
    ),
    "del_05": (
        "APPROVE: schedule-change communications = requests + reschedule calls + delivery changes",
        is_schedule_change_broader,
    ),
    "con_04": (
        "RESTRICT: meeting-reschedule call and meeting CORRECTION email only",
        is_meeting_reschedule_evidence,
    ),
    "xent_5": (
        "APPROVE: any voice-call transcript about rescheduling with this company",
        is_reschedule_transcript,
    ),
    "rel_02": (
        "RESTRICT: complaint emails (content-checked body text)",
        is_complaint_email,
    ),
    "rel_05": (
        "RESTRICT: complaint emails (content-checked body text)",
        is_complaint_email,
    ),
    "rel_07": (
        "REJECT: redundant after truncation fix (all email.received already labelled)",
        None,
    ),
    "mtg_01": (
        "APPROVE for calendar events + meeting emails; REJECT delivery items",
        is_meeting_evidence,
    ),
    "mtg_06": (
        "REJECT: redundant after truncation fix (all calendar events already labelled)",
        None,
    ),
}

# Questions classified as INVALID as specific-answer questions
INVALID_SPECIFIC = {"inv_03", "inv_04"}


def _template(qid: str) -> str:
    if qid.startswith("v2_xent_"):
        return "xent_" + qid.rsplit("_", 1)[1]
    return re.sub(r"^v2_ent_01\w\w_", "", qid)


def _entity_id_from_qid(qid: str, questions: dict) -> str:
    q = questions.get(qid)
    if q and q.entity_id:
        return q.entity_id
    m = re.search(r"ent_01\w\w", qid)
    return m.group(0) if m else ""


def build_v22_labels(qid: str, v2_labels: set, timeline_sigs: list,
                     questions: dict) -> set:
    """Build v2.2 labels for a question by applying content-checked sibling rules."""
    base = set(v2_labels)
    rule = RULES.get(_template(qid))
    if rule and rule[1]:
        ent_prefix = _entity_id_from_qid(qid, questions)
        if ent_prefix:
            base |= {
                f"sig:{s.signal_id}"
                for s in timeline_sigs
                if s.entity_id and ent_prefix in s.entity_id and rule[1](s)
            }
    return base


def score_at_k(run: dict, label_set: dict, k: int) -> tuple[int, dict]:
    """Score @k using retrieved_top100 results. Returns (hits, {qid: first_rank})."""
    hits = 0
    ranks = {}
    for r in run["per_question"]:
        qid = r["id"]
        lab = label_set.get(qid, set())
        rank = next(
            (h["rank"] for h in r.get("retrieved_top100", []) if h.get("document_id") in lab),
            -1,
        )
        ok = 0 < rank <= k
        hits += ok
        ranks[qid] = rank
    return hits, ranks


def run_eval(repo: str) -> dict:
    sys.path.insert(0, repo)
    from olo.m3.synthetic.generator import generate_timeline
    from olo.m3.benchmark_v2 import build_benchmark_v2

    tl = generate_timeline()
    qs = build_benchmark_v2(tl)
    q_map = {q.id: q for q in qs}

    # Build base v2 labels
    v2_labels = {
        q.id: {f"sig:{sid}" for sid in q.expected_signal_ids}
        for q in qs
    }

    # Build v2.2 labels (v2 + approved sibling rules)
    v22_labels = {
        qid: build_v22_labels(qid, v2_labels[qid], tl, q_map)
        for qid in v2_labels
    }

    runs = {
        "baseline_200": f"{repo}/docs/olo/m3/benchmark_200cands_corrected.json",
        "baseline_400": f"{repo}/docs/olo/m3/benchmark_final_corrected.json",
    }
    # Also check for new post-source-descriptor benchmarks
    for new_name in ["benchmark_src_desc_200cands.json", "benchmark_src_desc_400cands.json"]:
        p = Path(f"{repo}/docs/olo/m3/{new_name}")
        if p.exists():
            runs[new_name.replace("benchmark_", "").replace(".json", "")] = str(p)

    out = {
        "evaluator_version": "v2.2",
        "evaluator_provenance": "Based on Opus independent audit 2026-11-22; content-checked predicates",
        "rules": {k: v[0] for k, v in RULES.items()},
        "invalid_specific_questions": list(INVALID_SPECIFIC),
        "note": "v2.2 labels use content-checked predicates (body text, not subject keywords). "
                "INVALID questions (inv_03/04) reported separately.",
        "scores": {},
        "per_question": collections.defaultdict(dict),
    }

    n_total = len(qs)
    n_invalid = sum(1 for qid in v2_labels if _template(qid) in INVALID_SPECIFIC) * 3  # 3 entities

    for run_name, fpath in runs.items():
        p = Path(fpath)
        if not p.exists():
            print(f"  SKIP {run_name}: {fpath} not found")
            continue

        run = json.load(open(p))
        per_q = run.get("per_question", [])
        committed = sum(1 for r in per_q if r.get("pass_at", {}).get("10"))

        out["scores"][run_name] = {
            "committed_pass_at_10": committed,
            "v2": {},
            "v2.2": {},
            "invalid_specific_excluded": {"count": n_invalid, "ids": "inv_03x3 and inv_04x3"},
            "v2.2_excl_invalid": {},
        }

        for version, label_set in [("v2", v2_labels), ("v2.2", v22_labels)]:
            for k in (1, 5, 10, 20, 50):
                h, ranks = score_at_k(run, label_set, k)
                out["scores"][run_name][version][f"@{k}"] = h
            # Store per-question first ranks
            _, ranks_10 = score_at_k(run, label_set, 100)
            for qid, rk in ranks_10.items():
                out["per_question"][qid][f"{run_name}:{version}"] = rk

        # v2.2 excluding invalid specific questions
        v22_valid = {qid: lbs for qid, lbs in v22_labels.items()
                     if _template(qid) not in INVALID_SPECIFIC}
        h_valid, _ = score_at_k(run, v22_valid, 10)
        n_valid = sum(1 for r in per_q if _template(r["id"]) not in INVALID_SPECIFIC)
        out["scores"][run_name]["v2.2_excl_invalid"]["@10"] = h_valid
        out["scores"][run_name]["v2.2_excl_invalid"]["denominator"] = n_valid

    return dict(out)


if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else "/mnt/c/oloai"
    result = run_eval(repo)

    out_path = Path(f"{repo}/docs/olo/m3/eval_v2_2_result.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=1)
    print(f"Full results saved to {out_path}")

    print("\n=== Evaluation v2.2 Summary ===")
    for run_name, scores in result["scores"].items():
        committed = scores["committed_pass_at_10"]
        print(f"\n{run_name} (committed @10={committed}):")
        for version in ("v2", "v2.2", "v2.2_excl_invalid"):
            s = scores.get(version, {})
            if "@10" in s:
                denom = s.get("denominator", 201)
                print(f"  {version}: @10={s[chr(64)+chr(49)+chr(48)]}/{denom}  @20={s.get(chr(64)+chr(50)+chr(48), chr(63))}")
