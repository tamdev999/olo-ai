# M3.0 Gate Results — Final Correction Pass

**Branch:** `olo/m3-hindsight-memory`  
**Audited commit:** `d551b98a754c` (previous), this commit pending push  
**Hindsight fork:** `tamdev999/olo-persistentmemory` @ `0eb02d8c418e` (v1.2.1)  
**Run date:** 2026-11-19  
**Server:** port 18800, `postgresql://hindsight@/hindsight?host=/var/run/postgresql`, DeepSeek V3  
**Test corpus:** 385 synthetic signals (3 entities × 5 connectors × 12 months + P4 contradiction pairs)  
**Mode:** `OLO_GATE_STRICT=1`

---

## Implementation summary (11 corrections)

| # | Correction | Status | Location |
|---|-----------|--------|----------|
| 1 | Port isolation in setup.sh | ✓ | `olo/m3/hindsight/setup.sh` |
| 2 | Budget guard wired into feeder | ✓ | `olo/m3/feeder.py` + `olo/m3/hindsight/budget.py` |
| 3 | V1 answer leakage removed | ✓ | `olo/m3/synthetic/generator.py` + test |
| 4 | P4 5/5 contradictions | ✓ | generator + test |
| 5 | P1 delivery-date correctness | ✓ | test |
| 6 | V3 document history | ✓ (best-effort) | test |
| 7 | V4 NOT RUN (M3.3) | ✓ | test |
| 8 | P9 no-LLM verification | ✓ | test |
| 9 | Dependency/embedding pinning | ✓ | setup.sh |
| 10 | V10 accurate memory measurement | ✓ | test |
| 11 | Reproducible results | this doc | — |

---

## Gate matrix

| Gate | Status | Evidence |
|------|--------|----------|
| **V1 recall@10 ≥ 90%** | **FAIL 89.58%** | 43/48 questions hit; 5 misses are voice transcripts (pos 11-16) and confirmation emails. Query-derived temporal filter (not answer-derived). No leakage confirmed. |
| V1 NC (non-existent entity) | PASS | 0 results with fake entity tag |
| V1 NC (no-leakage) | PASS | `_extract_temporal_ym` returns None for queries without month text |
| V2 cross-connector ×2 | PASS | 5/5 connectors per entity; 0 results for nonexistent entity |
| V3 re-retain same ID | PASS | Document count preserved; best-effort history check |
| V3 NC | PASS | History endpoint accessible |
| **V4** | **NOT RUN (M3.3)** | Context Builder belongs to M3.3; not implemented |
| V5 crash recovery | **PASS (isolated)** / FAIL (sequential) | Passes alone (45-110s); fails in sequential suite due to RAM pressure from main server (1271 MB PSS competing with pg0 startup) |
| V6 rebuild ×2 | PASS | 20-item subset, identical IDs after bank drop+reload |
| **V7 pg0 isolation** | **PASS (isolated)** / FAIL (sequential) | Passes when run alone; fails sequentially due to same RAM pressure |
| V8 provenance | PASS | 37 structural tests including V8 |
| V9/P9 no LLM (structured) | PASS | 0 LLM calls for structured items |
| P9 NC (prose tracking) | **FAIL** | Correctly fails per correction #8: stats show 0 calls on fresh server. After an active DeepSeek workload, stats would be non-zero and this would pass. This IS the required behavior. |
| P9 budget wired | PASS | `batch_retain_to_hindsight` with `UsageGuard`, tested against scratch bank |
| **V10 Hindsight PSS ≤ 600 MB** | **XFAIL 1271 MB** | Planning target, not hard requirement. BBC capacity decision. |
| **V10 combined PSS ≤ 1 GB** | **XFAIL 1491 MB** | Hindsight 1271 MB + PostgreSQL 220 MB = 1491 MB. BBC capacity decision. |
| V10 NC | PASS | — |
| P1 delivery date | PASS | Finds rescheduled observation with April date > original |
| P1 NC | PASS | — |
| P2 invoice dispute | PASS | 3/3 sources (gmail, stripe, qbo) |
| **P4 5/5 contradictions** | **PASS (isolated)** / FAIL (sequential) | P4 uses May 2026 signals (d1=May 06, d2=May 10); passes when server not under RAM pressure |
| P4 NC | PASS | — |

---

## V1 recall@10: 89.58% (43/48) — no answer leakage

**Correction #3 applied:** `temporal_ym` is derived exclusively from explicit month/year text in the query (e.g., "April 2026"), never from expected-answer metadata.

**5 misses:**

| Question | Expected signal | Position |
|---------|----------------|----------|
| `phone call delivery date change Acme Corp April 2026` | `transcript_reschedule_ACME` (voice_fixture) | 13 |
| `original meeting date confirmation email Acme Corp April 2026` | `email_confirm_orig_ACME` | 17 |
| `phone call delivery date change Beta Tech April 2026` | `transcript_reschedule_BETA` (voice_fixture) | 11 |
| `original meeting date confirmation email Beta Tech April 2026` | `email_confirm_orig_BETA` | 12 |
| `phone call delivery date change Gamma Retail April 2026` | `transcript_reschedule_GAMMA` (voice_fixture) | 16 |

**Root cause:** RRF ranks DeepSeek observations and calendar events above voice transcripts and confirmation emails for April queries. The expected signals ARE in the results (positions 11-17) but not in the top 10.

**BBC action needed:** The Opus audit recommends investigating cross-encoder reranking if strict entity+temporal filtering still fails. BGE-small cross-encoder would need testing (adds RAM). Alternatively, increasing top-k to 15 would score these (all 5 are in positions 11-17) but changes the benchmark definition.

---

## P4 5/5 contradictions

P4 uses explicit contradiction pairs seeded in May 2026 (days 122-148, to avoid April competition with V1 scenario signals):
- **D1 meeting:** `(start + timedelta(days=126)).strftime("%B %d")` = "May 06, 2026"
- **D2 meeting:** `(start + timedelta(days=130)).strftime("%B %d")` = "May 10, 2026"
- **D1 delivery:** "May 20, 2026"; **D2 delivery:** "May 28, 2026"

5 explicit pairs: 3 meeting corrections (one per entity) + 2 delivery corrections (Acme, Beta). After DeepSeek consolidation, current observation has D2; D1 in history.

**Result:** PASS in isolation (DeepSeek correctly consolidates). FAILS sequentially when server crashed from V1 load.

---

## V10 Memory measurements (PSS from /proc/smaps_rollup)

| Metric | Value |
|--------|-------|
| Hindsight idle PSS | 1271 MB |
| Hindsight peak RSS (from /proc/status) | 1438 MB |
| PostgreSQL PSS | ~220 MB |
| Combined steady-state PSS | 1491 MB |
| BBC 1 GB target | EXCEEDS by 491 MB |

Model: `intfloat/multilingual-e5-small` fp32 ONNX (90 MB file, ~300 MB resident).

BGE-small and e5-small int8 comparison not completed (additional RAM concern; would require server restart with each model and full re-seed of 385 documents).

**Server stability observation:** The server experiences connection resets when rapid-fire ONNX embedding requests arrive on a fresh start. With `cpu_mem_arena=false`, the server recovers after ~5s. This suggests the ONNX initialization races with the first recall request.

---

## DeepSeek usage (from /metrics)

After full 385-signal evidence-first load:

| Scope | Calls | Input tokens | Output tokens |
|-------|-------|-------------|---------------|
| retain_extract_facts | ~1000 | ~3.5M | ~155K |
| consolidation | ~700 | ~3.3M | ~148K |
| consolidation_dedup | ~145 | ~62K | ~8K |
| **Total** | **~1845** | **~6.9M** | **~311K** |

**Estimated cost:** ~$0.97 + $0.09 = **~$1.06**  
**Budget:** $8.00 hard stop (enforced by `UsageGuard` in feeder)

---

## Port allocation (correction #1)

`setup.sh` now uses flock-protected port registry with collision detection:
- Explicit `HINDSIGHT_PORT` → checked against registry; fails if another business owns it
- Auto-allocation → scans registry + system ports, uses next free port above 18800
- Registry at `${HERMES_HOME}/hindsight_port_registry.json`

---

## Dependency pinning (correction #9)

- `HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_REVISION` removed (not a valid v1.2.1 var)
- `HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_PATH` pins to local snapshot: `...614241f622f53c4eeff9890bdc4f31cfecc418b3/onnx/model.onnx`
- Installed from fork git URL, `direct_url.json` verified in setup.sh
- `requirements-hindsight.lock` regenerated with actual pip freeze

---

## Structural tests

**37 passed, 2 skipped, 0 failed** (`tests/olo/m3/test_m3_budget.py` + `test_m3_gate.py -m 'not integration'`)

Budget guard: 17 unit tests all pass.

---

## Outstanding blockers

**P0 — BBC decisions required:**
1. **V1 recall@10 = 89.58%** (1 hit short of 90%). Root cause: voice transcripts and confirmation emails ranked below positions 10 by RRF. Cross-encoder reranking would likely fix this but adds RAM.
2. **V10 combined PSS = 1491 MB** (exceeds 1 GB target). Requires either lighter model or additional RAM budget.

**P1 — Environment:**
3. **Server stability:** Main server crashes under rapid ONNX load in sequential test run. Affects V5, V7, P4 when run sequentially. Each passes in isolation.
4. **P9 prose NC:** Correctly fails (correction #8) when stats show 0 calls on fresh server. This is the required behavior — it means a live DeepSeek workload is needed to validate this NC.
