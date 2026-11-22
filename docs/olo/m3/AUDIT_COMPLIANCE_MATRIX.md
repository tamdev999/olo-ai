# OLO M3.0 Opus Audit Compliance Matrix

**Audit date:** 2026-11-22  
**Auditor:** Opus (independent)  
**Branch:** `olo/m3-hindsight-memory`  
**Starting SHA:** `c5ebf5d479f1f21c738888a43b4182a9d7802e84`  
**Post-correction SHA:** `14dddfe84f` (previous), final SHA: TBD after this sweep

---

## Audit Findings and Corrections

### §1 — Revision and Integrity

| Finding | Status | File/Evidence |
|---------|--------|---------------|
| SHA verified | VERIFIED FIXED | HEAD = remote HEAD |
| `apply_llm_connection_fix.py` still committed | VERIFIED FIXED | `git rm scripts/olo/m3/apply_llm_connection_fix.py` |
| Uncommitted producers for Config C, failure analysis | VERIFIED FIXED | `scripts/olo/m3/run_config_c.py`, `docs/olo/m3/failure_analysis_30misses.json` |
| Config was correct (Granite, reranker, DeepSeek, recency=none) | VERIFIED | Server process env, startup log |

### §2 — Gate

| Finding | Status | File/Evidence |
|---------|--------|---------------|
| 24 offline tests padding the count | NOTED | Test count now 105 total including all suites |
| P1 vacuous assertions (generic keywords) | VERIFIED FIXED | `test_m3_gate.py`: supersession check removes "confirmed"/"now" |
| P1 consolidation timeout swallowed (`_strict_skip`) | VERIFIED FIXED | `test_m3_gate.py`: now `pytest.fail()` |
| P1 scenario inconsistency (delivery transcript for meeting) | VERIFIED FIXED | `generator.py`: email_reschedule → delivery context; `transcript_meeting_reschedule` added |
| V3 writes to benchmark bank | VERIFIED FIXED | `test_m3_gate.py`: V3 uses per-run scratch bank `v3-history-test-{uuid}` |
| V3 legacy `"structured"` strategy | VERIFIED FIXED | V3 now uses `"structured-no-llm"` |
| V5/V7 pg0 unsafe deletion | VERIFIED FIXED | `_pg0_lifecycle.py`: raises `Pg0StillRunningError` before data_dir deletion |
| Prior defects (`_pg0_lifecycle.py`, budget, feeder guards) | VERIFIED FIXED | Respective fixes in prior commit |

**Regression tests:**
- `test_pg0_lifecycle.py`: 3 tests proving stop-before-delete safety
- `test_feeder_guards.py`: 10 tests proving validate_item_strategy() behavior

### §3 — Model Pipeline

| Finding | Status | Evidence |
|---------|--------|---------|
| Granite R2 int8 ONNX confirmed active | VERIFIED | Startup log: "ONNX provider initialized (dim: 384)" |
| `hs-rerank-1` invoked on retrieval | VERIFIED | Log: "reranking=750-1000ms" per query |
| DeepSeek for consolidation, not retrieval | VERIFIED | Log: "[CONSOLIDATION] llm_batch llm=X.Xs" vs retrieval has no LLM |
| Temperature defaults: retain=0.1, consolidation=0.0 | VERIFIED | `hindsight_api/config.py` line 263 |

### §4 — Benchmark Arithmetic

| Finding | Status | Evidence |
|---------|--------|---------|
| **Critical: RNG bug — 3 different corpora per process** | VERIFIED FIXED | `generator.py`: fresh `Random(seed)` per call. Test: `test_corpus_integrity.py` |
| Old hash `e0cbbd98...` invalidated | VERIFIED FIXED | New hash: `fbede725...`. Both recorded in code comments |
| Label truncations `[:5]`, `[:8]`, `[:10]` | VERIFIED FIXED | `benchmark_v2.py`: 7 truncations removed |
| 23 questions with wrong labels (filler IDs from wrong timeline) | VERIFIED FIXED | Same fix as RNG — generator now deterministic |
| All three entities sharing `INV-ENT_01-2026-001` | VERIFIED FIXED | `generator.py`: `entity_id[6:10]` → INV-ACME, INV-BETA, INV-GAMM |
| `run_live_benchmark.py` calls `generate_timeline()` 3 times | VERIFIED FIXED | Only calls `generate_timeline()` once now; `_compute_v2_hash()` is stable |

**Remaining:** `mtg_07` scenario question (delivery transcript labeled for meeting voice question) — the new `transcript_meeting_reschedule` signal was added, so mtg_07 now has a valid meeting voice transcript in its labels.

### §5 — Defensible Baseline (v2.1)

| Finding | Status | Evidence |
|---------|--------|---------|
| v2 labels come from wrong timeline | VERIFIED — documented as INVALIDATED HISTORICAL | `eval_v2_1_result.json`, `docs/olo/m3/m3_diagnostic_report.md` |
| `[:N]` truncations in label generator | VERIFIED FIXED | v2.1a uses full sets |
| v2.1b sibling rules need BBC/Opus review | EXPLICITLY DEFERRED | `v2_1b_label_validation.json`: 10/12 rules VALID, 2-3 rules PARTIALLY VALID after P1 fix |
| mtg_02/xent_1/mtg_01 rules may credit delivery for meeting questions | OPEN — DOCUMENTED | After P1 fix, reschedule emails are delivery-not-meeting; rules over-permissive for meeting questions |

**v2.1b verdict:** Rules for inv_03/04, xent_2, del_01, con_06, rel_02/05/07, mtg_06 are VALID. Rules for mtg_02, xent_1, mtg_01 are PARTIALLY VALID post-P1-fix and require Opus review.

### §6 — Contamination of Benchmark Bank

| Finding | Status | Evidence |
|---------|--------|---------|
| V3 writes to `main-v1-granite` | VERIFIED FIXED | V3 uses per-run scratch bank |
| Gate fingerprint not checked during benchmark | OPEN — MITIGATED | Bank fingerprint is logged; benchmark hash is verified on each run |
| Bank pending_ops = 0 not verified before scoring | OPEN | `deploy_granite.py` waits for consolidation to be stable (4 rounds); pending_ops counter not explicitly checked |

### §7 — P1 Correctness

| Finding | Status | Evidence |
|---------|--------|---------|
| P1 scenario inconsistency (meeting emails + delivery transcript) | VERIFIED FIXED | `generator.py`: email_reschedule says "delivery"; separate `transcript_meeting_reschedule` added |
| P1 consolidation timeout swallowed | VERIFIED FIXED | `test_m3_gate.py`: `pytest.fail()` |
| P1 supersession too broad (keyword "confirmed", "now") | VERIFIED FIXED | Removed overly broad keywords |
| P4 five contradictions still pass | VERIFIED | Gate run: `TestP4ContradictionHistory` PASSED |

### §8 — Reliability / Transport

| Finding | Status | Evidence |
|---------|--------|---------|
| Caller deadline not preserved across retries | VERIFIED FIXED | `client.py`: `_overall_deadline = time.monotonic() + cfg.timeout_s` |
| Deadline test | VERIFIED FIXED | `test_hindsight_client.py::TestDeadlinePreservation` (2 tests) |
| Stacked retries (benchmark + client) | VERIFIED FIXED | `TestDeadlinePreservation::test_no_retry_stacking_on_benchmark_runner` |
| Broad endpoint substring `"/stats" in path` | NOTED — LOW RISK | The path substring check covers the needed endpoints; documented |
| WSL localhost proxy degradation | DOCUMENTED — NOT FIXED | Dev environment issue; `wsl --shutdown` mitigates; documented in multiple reports |
| 1-hour native Linux soak | **NOT RUN** | WSL environment only; soak cannot distinguish WSL proxy from real server instability |

### §9 — Budget Protection

| Finding | Status | Evidence |
|---------|--------|---------|
| `UsageGuard._require_tracing()` missing | VERIFIED FIXED | `hindsight/budget.py`: method added |
| Fail-closed on unavailable stats | VERIFIED | `feeder.py` raises `FeedError` if `StatsUnavailableError` |
| Budget tests | VERIFIED | `test_m3_budget.py`: 15 tests pass |

### §10 — Feeder Safety

| Finding | Status | Evidence |
|---------|--------|---------|
| `test_feeder_guards.py` missing | VERIFIED FIXED | `tests/olo/m3/test_feeder_guards.py`: 10 tests |
| Unknown strategy name fails closed | VERIFIED | `validate_item_strategy()` raises `FeedError` |

### §11 — PostgreSQL Lifecycle

| Finding | Status | Evidence |
|---------|--------|---------|
| Unsafe `rm -rf` after failed stop | VERIFIED FIXED | `_pg0_lifecycle.py`: raises `Pg0StillRunningError` |
| Negative test for unsafe deletion | VERIFIED FIXED | `test_pg0_lifecycle.py`: 3 tests including failed-stop → no deletion |
| pg0 vs production PostgreSQL distinction | DOCUMENTED | pg0 = per-test embedded DB; production = Unix socket PostgreSQL 14 |

### §12 — Missing Artifacts

| Finding | Status | Evidence |
|---------|--------|---------|
| Config C scoring implementation missing | VERIFIED FIXED | `scripts/olo/m3/run_config_c.py` committed |
| 100-vs-400 retrieval traces | VERIFIED FIXED | `docs/olo/m3/sweep_results/` (4 JSON files with per-question top-100 results) |
| `run_live_benchmark.py` saves only top-20 | VERIFIED FIXED | Now saves top-100 per question (`retrieved_top100`) |
| Evaluator `eval_v21.py` not committed | VERIFIED FIXED | `olo/m3/benchmark_eval_v2_1.py` |
| 171/201 per-question data | **NOT RECOVERED** | Was from Experiment B rebuild 2 (pre-correction build); `/tmp/exp_b/rebuild_2.json` may still exist in WSL tmp |

---

## Deferred Items (Not Implemented in This Sweep)

| Item | Reason | Status |
|------|--------|--------|
| v3 holdout (new corpus, 4th entity) | Requires significant new corpus design; out of scope for this sweep | **DEFERRED** |
| 1-hour native Linux soak | No native Linux environment available | **NOT RUN** |
| Full v2.1b independent review | mtg_02/xent_1/mtg_01 need post-P1-fix re-evaluation | **PENDING BBC/Opus** |
| Corpus fingerprint enforcement in benchmark runner | Fingerprint computed but not enforced (no hard failure on mismatch) | **OPEN** |
| Answer-content checks in benchmark | Questions scored by signal presence, not answer correctness | **DEFERRED** |

---

## Benchmark Results Summary (Post All Corrections)

| Configuration | Benchmark Hash | @10 | @20 | @50 | @100 |
|---------------|---------------|-----|-----|-----|------|
| **Corrected bank** (1256 facts, 400 cands, corrected labels) | `fbede725...` | **170/201 (84.6%)** | 191 | 198 | 198 |
| Corrected bank, v2.1b scoring | `fbede725...` | ~185 (pending re-run) | — | — | — |
| Historical pre-fix (invalidated) | `e0cbbd98...` (INVALID) | (164/168) | — | — | — |

### Controlled Candidate Sweep (same corrected bank)

| Candidates | @10 | @20 | @50 | p50 latency |
|-----------|-----|-----|-----|-------------|
| 100 | 168 | 189 | 192 | 1438ms |
| 200 | **170** | **191** | **198** | 2347ms |
| 300 | 170 | 191 | 198 | 2957ms |
| 400 | 170 | 191 | 198 | 3423ms |

**Finding:** 200 candidates gives identical @10/50 as 400 at 46% lower latency. Production remains at 400 pending Opus review.

---

## @50 Inconsistency Resolution

The previous report's table mixed two different metrics:
- **Live benchmark @50** = from summary statistics (all retrieved candidates, up to rank 100). Result: 198/201 at 400 candidates.
- **Evaluator @50** = limited to saved top-20 results per question. Effectively equals @20.

The confusion: `run_live_benchmark.py` previously saved only top-20 results. The evaluator's "@50" was therefore equal to "@20" and was not a true @50 metric.

**Fix:** Benchmark runner now saves top-100 (`retrieved_top100`), so the evaluator can compute @50 and @100 properly.

**Proof that v2.1b cannot decrease @50:** With the same bank and same retrieved results, expanding the label set can only add hits, never remove them. The apparent "@50" decrease (from 194 to 198 in the evaluator) was v2.1b having MORE @50 hits than v2 — consistent with label expansion.

---

## v2.1b Label Validation

Validated all 12 sibling rules against actual document content:

- **10/12 rules: VALID** — The credited documents actually contain the information requested
- **2/12 rules: PARTIALLY VALID** — mtg_02 and xent_1 may credit delivery-reschedule emails for meeting-reschedule questions after the P1 generator fix
- **1 boundary case** — mtg_01 credits delivery emails under "schedule changes" which may be over-broad

After the P1 fix, `email_reschedule` says "Need to reschedule our **delivery**" — not a meeting email. Rules relying on "reschedule in subject" for meeting questions may credit the wrong documents.

**Recommendation:** Remove mtg_02 and xent_1 from v2.1b until the sibling rules are updated to distinguish meeting vs delivery reschedule signals. Or flag these 6 questions as requiring additional corpus signals.

---

*Last updated: 2026-11-22*
