"""Live 201-question Granite benchmark against main-v1-granite bank.

Runs the frozen benchmark_v2 questions against the live Hindsight server
using the actual Granite R2 int8 + hs-rerank-1 + DeepSeek-consolidated bank.
Reports Recall@1/5/10/20/50/100 and per-question results.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, '/mnt/c/oloai')

from olo.m3.benchmark_v2 import build_benchmark_v2, get_benchmark_v2_hash, _compute_v2_hash
from olo.m3.hindsight.client import (
    HindsightClient, HindsightConfig, HindsightAPIError, HindsightUnavailable
)

# ── Config ───────────────────────────────────────────────────────────────────
BANK_ID    = os.environ.get("HINDSIGHT_BANK_ID", "main-v1-granite")
TOP_K_MAX  = 100
K_TARGETS  = [1, 5, 10, 20, 50, 100]
# Historical hash (pre-fix): generator RNG bug caused bank/hash/scorer to use 3 different
# timelines, invalidating 33 question labels (Opus audit §4.1). Label truncations ([:N])
# also reduced label coverage. Both defects are fixed; the hash is now stable.
# OLD_FROZEN_HASH = "e0cbbd985af2ca5a070751b1921f3c6c77d52b2013a3f0d52a40cf6ad98c7592"
# PRE_P1_FIX_HASH = "e9ac10c347caff427e28126bfc336be67d1316145a7f408a2702af7fb6b47ef5"
# PRE_P2_FIX_HASH = "fbede725aeb89d3557e048803e0891d134c10734ac1ff2c4a7e80f139d28a3be"
FROZEN_HASH = "1fb288817e141ee50aaf92a816009e4e76adabcd0ff129a074f7023ac3538972"
# Corpus fingerprint -- SHA-256 of all 388 authoritative source signals.
# Computed from generate_timeline() with _DEFAULT_SEED.
# A mismatch means the bank was built from a different corpus than the benchmark labels expect.
FROZEN_CORPUS_FP = "1877ca71d6fbbe64a6ef0b728159ceb4acc2595a14e05d0f9e44db2aa6759a0b"
OUT_DIR     = Path("/tmp/benchmark_results")

def _connect() -> HindsightClient:
    cfg = HindsightConfig.from_env()
    c = HindsightClient(cfg)
    for attempt in range(5):
        try:
            c.wait_until_ready(timeout_s=30.0)
            return c
        except HindsightUnavailable:
            time.sleep(10)
    raise RuntimeError("Server not ready after 5 attempts")

def _recall_question(c: HindsightClient, q, retries: int = 2) -> dict:
    """Run a single benchmark question against the live bank."""
    tg = []
    if q.entity_id:
        tg.append({"tags": [f"ent:{q.entity_id}"], "match": "all_strict"})
    if q.temporal_ym:
        tg.append({"tags": [q.temporal_ym], "match": "any_strict"})

    for attempt in range(retries + 1):
        t0 = time.monotonic()
        try:
            result = c.recall(
                q.query,
                tag_groups=tg if tg else None,
                max_tokens=8000,   # 8000 returns all 100 reranked candidates
                trace=True,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            return {"result": result, "latency_ms": latency_ms, "error": None}
        except HindsightUnavailable as e:
            if attempt < retries and "Connection reset" in str(e):
                time.sleep(5)
            else:
                return {"result": {}, "latency_ms": -1, "error": str(e)}
        except Exception as e:
            return {"result": {}, "latency_ms": -1, "error": str(e)}

def _score_result(result: dict, expected_ids: set[str], k: int) -> bool:
    """True if any expected signal appears in the top-k hits."""
    hits = result.get("results", [])[:k]
    for hit in hits:
        # Direct document ID match
        doc_id = hit.get("document_id", "") or ""
        if doc_id in expected_ids:
            return True
        # Match via source_fact_ids (observations referencing source docs)
        for sfid in hit.get("source_fact_ids", []) or []:
            sid = sfid.get("document_id", "") if isinstance(sfid, dict) else str(sfid)
            if sid in expected_ids:
                return True
    return False

def _first_rank(result: dict, expected_ids: set[str]) -> int:
    """Return 1-indexed rank of first correct hit, or -1 if not found in top-100."""
    for rank, hit in enumerate(result.get("results", [])[:100], 1):
        doc_id = hit.get("document_id", "") or ""
        if doc_id in expected_ids:
            return rank
        for sfid in hit.get("source_fact_ids", []) or []:
            sid = sfid.get("document_id", "") if isinstance(sfid, dict) else str(sfid)
            if sid in expected_ids:
                return rank
    return -1


class CorpusMismatchError(Exception):
    """Raised when the bank's evidence does not match the expected corpus."""


# ---------------------------------------------------------------------------
# Operations-state check (P0.2)
# ---------------------------------------------------------------------------

#: Operation statuses considered successfully completed.
#: Anything outside this set causes the bank to be rejected — fail closed.
#:
#: Intentionally excludes ``"failed"`` and ``"error"``:
#:   A failed consolidation can leave observations incomplete; there is no way
#:   via the pinned Hindsight 1.2.1 API to distinguish a failure that was later
#:   superseded by a successful re-operation from one that left the bank in an
#:   inconsistent state.  The conservative rule is to block certification until
#:   no failed operations appear in the history.  Banks whose failures were
#:   repaired by a later successful run will pass once the original failed
#:   records are no longer returned by the operations endpoint.
#:   LIMITATION: a bank that was correctly repaired but still has historical
#:   ``failed`` records will be rejected.  This is intentional and documented.
_SUCCESSFUL_OP_STATUSES = frozenset({"completed", "succeeded", "done"})

#: Statuses known to represent actively running (non-terminal) work.
_PENDING_OP_STATUSES = frozenset({"pending", "processing"})

#: Maximum operations to fetch per page.
#: The pinned Hindsight 1.2.1 server enforces a maximum of 100 per request;
#: requests for 500 return HTTP 422.
_OPS_PAGE_SIZE = 100


def _check_no_pending_operations(
    client: "HindsightClient",
    bank_id: str,
    context: str = "verify_corpus_match",
) -> int:
    """Verify every operation in the bank is in a known-successful state.

    Paginate through ALL operations.  Raise ``CorpusMismatchError`` if:

    - Any operation is pending or processing (bank not yet stable).
    - Any operation is failed, errored, or has an unknown status.
      A failed consolidation can leave observations incomplete; the pinned
      Hindsight 1.2.1 API does not expose whether a failure was later
      superseded by a successful re-run, so the check is conservatively
      blocking.  See ``_SUCCESSFUL_OP_STATUSES`` for the documented limitation.
    - The endpoint returns HTTP 4xx/5xx, is unreachable, or times out.
    - The response is malformed or missing the operations list.

    Returns the total operations seen across all pages.

    Pagination note: Hindsight 1.2.1 enforces a maximum page size of 100.
    Requests for >100 return HTTP 422.  ``_OPS_PAGE_SIZE = 100`` is the
    correct upper bound.
    """
    from olo.m3.hindsight.client import _request, HindsightAPIError, HindsightUnavailable

    offset = 0
    total_seen = 0

    while True:
        path = client._bank_path("/operations", bank_id)
        params = {"limit": str(_OPS_PAGE_SIZE), "offset": str(offset)}
        try:
            data = _request(client._cfg, "GET", path, params=params)
        except HindsightAPIError as exc:
            raise CorpusMismatchError(
                f"{context}: operations endpoint returned HTTP {exc.status_code} "
                f"for bank {bank_id!r}. "
                "Cannot certify corpus without confirmed operation state."
            ) from exc
        except HindsightUnavailable as exc:
            raise CorpusMismatchError(
                f"{context}: operations endpoint unreachable for bank {bank_id!r}: {exc}. "
                "Cannot certify corpus without confirmed operation state."
            ) from exc
        except Exception as exc:
            raise CorpusMismatchError(
                f"{context}: unexpected error querying operations for bank {bank_id!r}: "
                f"{type(exc).__name__}: {exc}. "
                "Cannot certify corpus without confirmed operation state."
            ) from exc

        if not isinstance(data, dict):
            raise CorpusMismatchError(
                f"{context}: operations endpoint returned {type(data).__name__}, "
                "expected dict. Response is malformed."
            )

        # Accept both "operations" (Hindsight 1.2.1) and "items" (alternate schema).
        # Explicitly check key presence to distinguish "key absent" from "key = []".
        if "operations" in data:
            ops: object = data["operations"]
        elif "items" in data:
            ops = data["items"]
        else:
            ops = None

        if ops is None:
            # Neither key present.  Only fail if total indicates operations exist.
            total_hdr = data.get("total")
            if total_hdr and total_hdr > 0:
                raise CorpusMismatchError(
                    f"{context}: operations response has total={total_hdr} but "
                    "no 'items' or 'operations' key. Response is malformed."
                )
            break

        if not isinstance(ops, list):
            raise CorpusMismatchError(
                f"{context}: operations list is {type(ops).__name__}, expected list."
            )

        # --- Classify every operation on this page ---
        # Pending/processing: bank is not yet stable.
        # Failed/error/unknown: may indicate incomplete bank state.
        # Both categories block certification.
        active: list[dict] = []    # pending or processing
        failures: list[dict] = []  # failed, error, or unrecognised status
        for op in ops:
            status = op.get("status")
            if status in _PENDING_OP_STATUSES:
                active.append(op)
            elif status not in _SUCCESSFUL_OP_STATUSES:
                failures.append(op)

        if active:
            detail_parts = [
                f"{op.get('status', '<missing>')}/{op.get('task_type') or op.get('type', '?')}"
                for op in active[:5]
            ]
            raise CorpusMismatchError(
                f"{context}: bank {bank_id!r} has {len(active)} pending/processing "
                f"operation(s) at offset={offset}: {', '.join(detail_parts)}. "
                "Corpus cannot be verified while operations are pending or processing."
            )

        if failures:
            detail_parts = [
                f"{op.get('status', '<missing>')}/{op.get('task_type') or op.get('type', '?')}"
                for op in failures[:5]
            ]
            raise CorpusMismatchError(
                f"{context}: bank {bank_id!r} has {len(failures)} failed/unrecognised "
                f"operation(s) at offset={offset}: {', '.join(detail_parts)}. "
                "Failed operations may have left the bank in an inconsistent state. "
                "LIMITATION: the Hindsight 1.2.1 API does not distinguish historical "
                "failures that were later superseded; this check conservatively blocks "
                "certification until the operations history contains no failures."
            )

        total_seen += len(ops)

        # --- Pagination ---
        total_declared = data.get("total")
        if len(ops) < _OPS_PAGE_SIZE:
            break  # This page was smaller — we have all operations
        if total_declared is not None and total_seen >= total_declared:
            break  # Covered all operations per total field
        offset += _OPS_PAGE_SIZE

    return total_seen


def verify_corpus_match(client: HindsightClient, frozen_corpus_fp: str) -> str:
    """Verify the bank contains ALL expected source documents with correct content.

    Per Opus sixth-remediation P0.2 audit:
    1. Verifies corpus fingerprint (generator vs frozen expectation).
    2. Checks ALL operations are terminal BEFORE document enumeration (P0.2 fix).
       Uses authenticated, paginated requests — never swallows exceptions.
    3. Enumerates every bank document through all pagination pages.
    4. Counts ALL documents (not just sig: prefix) when checking for extras.
    5. Compares the sig: document set with the authoritative corpus.
    6. Rejects missing IDs, extra IDs (any prefix), content-hash mismatches,
       missing/empty hashes.
    7. Checks ALL operations are still terminal AFTER document enumeration
       (race guard: catches operations started during the enumeration window).
    8. Generates and returns a deterministic bank fingerprint from sorted
       (document_id, content_hash) pairs.

    Returns the bank fingerprint string on success.
    Raises CorpusMismatchError before any question is scored.
    """
    import hashlib
    from olo.m3.synthetic.generator import generate_timeline, corpus_fingerprint

    tl = generate_timeline()
    actual_fp = corpus_fingerprint(tl)

    if frozen_corpus_fp and actual_fp != frozen_corpus_fp:
        raise CorpusMismatchError(
            f"Corpus fingerprint mismatch: expected {frozen_corpus_fp[:16]}... "
            f"got {actual_fp[:16]}... -- the benchmark was built against a "
            f"different corpus than the current generator produces."
        )

    # Build expected manifest: sig_id → expected SHA-256 of body_text.
    expected_hashes: dict[str, str] = {}
    for sig in tl:
        sig_id = f"sig:{sig.signal_id}"
        body_sha = hashlib.sha256(sig.body_text.encode("utf-8")).hexdigest()
        expected_hashes[sig_id] = body_sha

    # --- P0.2 fix: Check 1 of 2 — all operations terminal BEFORE enumeration ---
    # Uses authenticated _request with pagination; never swallows exceptions.
    # Previous implementation used limit=1 (missed older pending ops) and
    # swallowed ALL exceptions with `except Exception: pass`.
    n_ops_before = _check_no_pending_operations(
        client, BANK_ID, context="verify_corpus_match[pre-enum]"
    )

    # Enumerate ALL bank documents through all pagination pages.
    # Track ALL doc IDs (not just sig:) to detect contamination from gate tests.
    actual_ids_all: set[str] = set()     # every doc in the bank
    actual_ids_sig: set[str] = set()     # sig: docs
    stale: list[str] = []
    missing_hash: list[str] = []
    bank_fingerprint_rows: list[tuple[str, str]] = []  # (doc_id, content_hash)
    offset = 0
    page_size = 200
    while True:
        try:
            resp = client.list_documents(limit=page_size, offset=offset, bank_id=BANK_ID)
        except Exception as exc:
            raise CorpusMismatchError(f"Cannot list bank documents: {exc}") from exc
        docs = resp.get("items", [])
        if not docs:
            break
        for doc in docs:
            doc_id = doc.get("document_id") or doc.get("id") or ""
            if not doc_id:
                continue
            actual_ids_all.add(doc_id)

            # Use server-generated content_hash — not client-supplied metadata hash.
            content_hash = doc.get("content_hash") or ""

            if doc_id.startswith("sig:"):
                actual_ids_sig.add(doc_id)
                if not content_hash:
                    missing_hash.append(doc_id)
                else:
                    expected_sha = expected_hashes.get(doc_id, "")
                    if expected_sha and content_hash != expected_sha:
                        stale.append(doc_id)
                    bank_fingerprint_rows.append((doc_id, content_hash))

        if len(docs) < page_size:
            break
        offset += page_size

    missing = [sid for sid in expected_hashes if sid not in actual_ids_sig]
    # Extra sig: docs not in the expected corpus
    extra_sig = [d for d in actual_ids_sig if d not in expected_hashes]
    # Non-sig: docs are always extra (gate test artifacts, etc.)
    extra_non_sig = [d for d in actual_ids_all if not d.startswith("sig:")]

    errors = []
    if missing:
        errors.append(
            f"{len(missing)} expected signal(s) missing from bank "
            f"(first 3: {missing[:3]}). Rebuild the bank."
        )
    if stale:
        errors.append(
            f"{len(stale)} bank document(s) have stale content "
            f"(server content_hash != expected body_text SHA-256; "
            f"first 3: {stale[:3]}). Bank was built from a different corpus."
        )
    if missing_hash:
        errors.append(
            f"{len(missing_hash)} bank document(s) have no server-generated "
            f"content_hash (first 3: {missing_hash[:3]}). "
            "Cannot verify corpus integrity without the server hash."
        )
    if extra_sig:
        errors.append(
            f"{len(extra_sig)} unexpected sig: document(s) in bank "
            f"(first 3: {extra_sig[:3]}). Bank may be contaminated."
        )
    if extra_non_sig:
        errors.append(
            f"{len(extra_non_sig)} non-sig: document(s) in bank "
            f"(first 3: {extra_non_sig[:3]}). Bank contains unexpected documents."
        )

    if errors:
        raise CorpusMismatchError(
            "Bank content does not match expected corpus:\n" +
            "\n".join(f"  {e}" for e in errors)
        )

    # --- P0.2 fix: Check 2 of 2 — all operations still terminal AFTER enumeration ---
    # Catches a new pending operation started during the document-enumeration window.
    n_ops_after = _check_no_pending_operations(
        client, BANK_ID, context="verify_corpus_match[post-enum]"
    )

    # Generate deterministic bank fingerprint from sorted (doc_id, content_hash)
    bank_fingerprint_rows.sort()
    bank_fp_data = json.dumps(bank_fingerprint_rows, separators=(",", ":"))
    bank_fp = hashlib.sha256(bank_fp_data.encode()).hexdigest()

    print(f"Corpus verified: {len(tl)} signals, {len(actual_ids_sig)} in bank, "
          f"ops_seen={n_ops_before}/{n_ops_after}, "
          f"corpus_fp={actual_fp[:16]}..., bank_fp={bank_fp[:16]}...")
    return bank_fp


def run_benchmark() -> dict:
    # Build questions first so _RNG state is consistent; verify hash after.
    questions = build_benchmark_v2()
    benchmark_hash = _compute_v2_hash()   # always recomputes deterministically
    if benchmark_hash != FROZEN_HASH:
        raise ValueError(
            f"BENCHMARK HASH MISMATCH — refusing to score.\n"
            f"  Computed: {benchmark_hash}\n"
            f"  Expected: {FROZEN_HASH}\n"
            f"The benchmark question set changed. Update FROZEN_HASH to the new "
            f"hash and document the change, or revert the question/label changes."
        )
    print(f"Benchmark hash verified: {benchmark_hash[:20]}...")

    c = _connect()
    print(f"Server ready. Bank: {BANK_ID}")
    bank_fp = verify_corpus_match(c, FROZEN_CORPUS_FP)
    # Verify model
    pid = None
    try:
        import subprocess
        p = subprocess.run(
            ["pgrep", "-u", "olo_hs", "-a", "hindsight-api"],
            capture_output=True, text=True,
        )
        if "granite" in p.stdout.lower():
            print("Model: Granite confirmed in server process")
        else:
            print("Model: check server env manually")
    except Exception:
        pass

    questions = build_benchmark_v2()
    print(f"Running {len(questions)} questions against {BANK_ID}...")

    per_question = []
    latencies_ms = []
    errors = 0

    for i, q in enumerate(questions, 1):
        expected = {f"sig:{sid}" for sid in q.expected_signal_ids}
        resp = _recall_question(c, q)

        if resp["error"]:
            errors += 1
            per_question.append({
                "id": q.id, "category": q.category,
                "query": q.query, "entity_id": q.entity_id,
                "expected": list(expected),
                "first_rank": -1,
                "pass_at": {str(k): False for k in K_TARGETS},
                "error": resp["error"],
                "latency_ms": -1,
            })
            if i % 20 == 0:
                print(f"  [{i}/{len(questions)}] errors so far: {errors}")
            continue

        result = resp["result"]
        latency = resp["latency_ms"]
        latencies_ms.append(latency)

        first_r = _first_rank(result, expected)
        pass_at = {str(k): _score_result(result, expected, k) for k in K_TARGETS}

        # Retrieve actual result IDs for per-question record
        retrieved = []
        for rank, hit in enumerate(result.get("results", [])[:100], 1):
            doc_id = hit.get("document_id", "") or ""
            retrieved.append({
                "rank": rank,
                "document_id": doc_id,
                "is_expected": doc_id in expected,
                "source_fact_ids": [
                    (sfid.get("document_id","") if isinstance(sfid,dict) else str(sfid))
                    for sfid in (hit.get("source_fact_ids",[]) or [])
                ][:3],
            })

        per_question.append({
            "id": q.id, "category": q.category,
            "query": q.query, "entity_id": q.entity_id,
            "expected": list(expected),
            "first_rank": first_r,
            "pass_at": pass_at,
            "latency_ms": round(latency, 1),
            "retrieved_top100": retrieved,
            "error": None,
        })

        if i % 20 == 0 or i == len(questions):
            so_far_at10 = sum(1 for r in per_question if r["pass_at"].get("10"))
            print(f"  [{i}/{len(questions)}] recall@10 so far: {so_far_at10}/{i} = {so_far_at10/i:.1%}")
            time.sleep(0.5)  # brief pause to keep server healthy
        time.sleep(0.05)  # 50ms between questions

    # Aggregate metrics
    n = len(questions)
    recall = {}
    for k in K_TARGETS:
        hits = sum(1 for r in per_question if r["pass_at"].get(str(k)))
        recall[f"recall_at_{k}"] = {"hits": hits, "total": n, "pct": hits / n}

    lat_ok = sorted(latencies_ms)
    lat_p50 = lat_ok[len(lat_ok)//2] if lat_ok else 0
    lat_p95 = lat_ok[int(len(lat_ok)*0.95)] if lat_ok else 0
    lat_max = max(lat_ok) if lat_ok else 0

    # Category breakdown for recall@10
    cat_results = {}
    for r in per_question:
        cat = r["category"]
        if cat not in cat_results:
            cat_results[cat] = {"hits": 0, "total": 0}
        cat_results[cat]["total"] += 1
        if r["pass_at"].get("10"):
            cat_results[cat]["hits"] += 1
    for cat in cat_results:
        d = cat_results[cat]
        d["pct"] = d["hits"] / d["total"]

    # Failures: questions that miss at recall@10
    failures = [r for r in per_question if not r["pass_at"].get("10")]
    failure_cats = {}
    for f in failures:
        fc = f["category"]
        failure_cats[fc] = failure_cats.get(fc, 0) + 1

    summary = {
        "benchmark_hash": benchmark_hash,
        "bank_id": BANK_ID,
        "corpus_fingerprint": FROZEN_CORPUS_FP,
        "bank_fingerprint": bank_fp,
        "n_questions": n,
        "errors": errors,
        "recall": recall,
        "latency_ms": {"p50": round(lat_p50,1), "p95": round(lat_p95,1), "max": round(lat_max,1)},
        "category_recall_at_10": {k: f"{v['hits']}/{v['total']} ({v['pct']:.1%})" for k,v in cat_results.items()},
        "failure_categories": failure_cats,
    }

    # Print summary
    print("\n" + "="*70)
    print("LIVE GRANITE BENCHMARK RESULTS")
    print("="*70)
    print(f"Bank: {BANK_ID}  |  Questions: {n}  |  Errors: {errors}")
    print(f"Hash: {benchmark_hash}")
    print()
    for k in K_TARGETS:
        d = recall[f"recall_at_{k}"]
        bar = "█" * int(d["pct"]*40)
        print(f"  Recall@{k:<4} {d['hits']:3}/{d['total']} = {d['pct']:6.2%}  {bar}")
    print()
    print(f"Latency: p50={lat_p50:.0f}ms  p95={lat_p95:.0f}ms  max={lat_max:.0f}ms")
    print()
    print("Category breakdown (recall@10):")
    for cat, v in sorted(cat_results.items(), key=lambda x: x[1]['pct']):
        bar = "█" * int(v['pct']*20)
        print(f"  {cat:<40} {v['hits']:3}/{v['total']} = {v['pct']:5.1%}  {bar}")
    print()
    print(f"Failures at @10: {len(failures)}")
    print("Top failure categories:", dict(sorted(failure_cats.items(), key=lambda x:-x[1])[:8]))

    # Save results
    out_file = OUT_DIR / "live_benchmark_results.json"
    OUT_DIR.mkdir(exist_ok=True, parents=True)
    with open(out_file, "w") as f:
        json.dump({"summary": summary, "per_question": per_question}, f, indent=2)
    print(f"\nFull results saved to: {out_file}")
    return summary

if __name__ == "__main__":
    run_benchmark()
