# M3.0 Gate Results — Live Run

**Branch:** `olo/m3-hindsight-memory`  
**Hindsight version:** v1.2.1 (`tamdev999/olo-persistentmemory` @ `0eb02d8c418eac6b489bc63883c993b2257dfac4`)  
**Run date:** 2026-11-19  
**Server:** port 18800, `postgresql://hindsight@/hindsight?host=/var/run/postgresql`, DeepSeek V3  
**Test corpus:** 343 synthetic signals (3 entities × 5 connectors × 12 months)  
**Mode:** `OLO_GATE_STRICT=1` — skips become failures

---

## Gate Summary

| Gate | Description | Status | Result |
|------|-------------|--------|--------|
| V1 | recall@10 ≥ 0.90; p95 ≤ 500 ms | **FAIL** | 16.67% (2/12 questions hit) |
| V1-NC | Impossible entity tag returns 0 results | PASS | 0 results for non-existent entity tag |
| V2 | Cross-connector identity (5 connectors) | PASS | All 5 sources found per entity |
| V2-NC | Non-existent entity tag returns 0 results | PASS | 0 results confirmed |
| V3 | History preserved on re-retain | PASS | Re-retain with new tag: 0 doc loss |
| V3-NC | Delete-and-query returns nothing | PASS | Deleted doc no longer retrievable |
| V4 | Hot without Hindsight | PASS | Structural (Olo-side only) |
| V5 | Crash recovery (kill -9) | PASS | 343/343 docs after crash+restart |
| V6 | Replay / rebuild identical IDs | PASS | Same 20-item doc-ID set after bank drop+reload |
| V6-NC | Removing one item detected | PASS | Diff set detected correctly |
| V7 | Isolation (two businesses) | PASS | Business B has 0 of Business A's documents |
| V8 | Provenance 100% | PASS | Structural: sha256 chain verified (110/110) |
| V9 / P9 | No-LLM guarantee (structured items) | PASS | 0 LLM calls for structured items |
| V10 | Idle RAM ≤ 600 MB | **FAIL** | 1408 MB (ONNX multilingual-e5-small loaded) |
| V10-NC | 650 MB > 600 MB check works | PASS | NC assertion correct |
| P1 | Delivery date consolidation + history | PASS | Reschedule observation found; history entries confirmed |
| P1-NC | Non-reschedule query has no reschedule obs | PASS | NC confirmed |
| P2 | Invoice dispute (3 sources) | PASS | gmail_owner + stripe_fixture + qbo_fixture in results |
| P4 | Contradiction / observation history | PASS | 5 contradiction observations, old values in history |
| P4-NC | History has ≥ 2 entries for updated obs | PASS | NC confirmed |

**Summary: 18/20 PASS, 2/20 FAIL (V1, V10)**

---

## V1 Failure Analysis

**Gate:** V1 recall@10 ≥ 0.90  
**Result:** 16.67% (2/12 labelled questions hit top 10)

**Root cause:** Hindsight's RRF-based semantic ranking surfaces calendar events before the specific reschedule email/transcript for the query "When did Alice Chen ask to reschedule a meeting?". There are 287 results for the entity; the expected signals are present but ranked at position > 10. The temporal_window parameter does not filter the result set (confirmed by probe: 287 results with and without window). Calendar events semantically compete for the same query intent.

**Evidence:** Direct `get_document()` confirmed both expected signals exist in the bank:
- `sig:sig_d992bc383e81974213d7d74a` (voice transcript) — present
- `sig:sig_08e9ea787f62d5e743bf7288` (reschedule email) — present
- Both appear in the full 287-result set; neither in top 10

**Disposition:** Genuine system FAIL. Hindsight v1.2.1 does not achieve 90% recall@10 on this labelled question set without temporal filtering.

---

## V10 Failure Analysis

**Gate:** Idle RAM ≤ 600 MB  
**Result:** 1408 MB

**Root cause:** `intfloat/multilingual-e5-small` ONNX model is loaded at startup and kept resident (~900 MB). The 600 MB limit was designed for a lighter embedding setup. After processing 343 documents and running V5/V6/V7 test loads, peak RSS reached 1436 MB.

**Disposition:** Genuine system FAIL. The ONNX model requirement and the 600 MB limit are incompatible with the current Option B configuration. Would need a lighter embedding model or ONNX quantization to meet the threshold.

---

## DeepSeek Usage (from /metrics endpoint)

| Scope | Calls | Input tokens | Output tokens |
|-------|-------|-------------|---------------|
| retain_extract_facts | 966 | ~3,152,437 | ~148,428 |
| consolidation | 668 | ~3,313,412 | ~141,767 |
| consolidation_dedup | 142 | ~60,071 | ~7,560 |
| verification | 1 | ~8 | ~1 |
| **Total** | **1,777** | **~6,525,928** | **~297,756** |

**Estimated cost:** 6.5M × $0.14/M + 298K × $0.28/M ≈ **$0.91 + $0.08 = ~$1.00**  
**Budget limit:** $8.00 hard stop  
**Status: WITHIN BUDGET ($1.00 / $8.00)**

Token data is from OTEL prometheus metrics buckets — values are approximate (bucket-aggregated).

---

## Bank Statistics (post-load)

```
total_documents: 356
total_nodes: 1013
total_observations: 605
pending_consolidation: 0
```

Initial evidence-first load: 343 signals, 199 blobs. Additional test documents from V3/V6/V7 gate tests account for the 356 total.

---

## Fork Verification

```
tamdev999/olo-persistentmemory
HEAD = refs/tags/v1.2.1 = 0eb02d8c418eac6b489bc63883c993b2257dfac4
```

Verified via `git ls-remote` before load run. hindsight-api-slim==1.2.1 confirmed in `/root/olo_m3_hindsight_venv`.

---

## Environment Notes

- **Platform:** WSL2 on Windows 11, root user
- **Python:** 3.14.9 (Hindsight venv), 3.14.9 (olo_linux_venv for pytest)
- **PostgreSQL:** 14 + pgvector 0.7.4 on Unix socket `/var/run/postgresql`
- **pg0 (embedded PostgreSQL):** not used for main server or V7 (initdb fails as root)
  - V5 and V7 use dedicated system PostgreSQL databases (`hindsight_v5test`, `hindsight_v7a`, `hindsight_v7b`)
  - `pg_hba.conf` updated: `local all hindsight trust` to allow root-process connections
- **ONNX model:** `intfloat/multilingual-e5-small` auto-downloaded from HuggingFace on first run

---

## Structural Tests (not_integration)

Structural suite (V4, V8, config validity): **522+ PASS, 0 FAIL** (see separate run).

---

## Test Command

```bash
export HINDSIGHT_HOST=127.0.0.1
export HINDSIGHT_PORT=18800
export HINDSIGHT_BANK_ID=main-v1
export HINDSIGHT_BIN=/root/olo_m3_hindsight_venv/bin/hindsight-api
export OLO_GATE_STRICT=1
export OLO_DB_PATH=/tmp/gate_olo.db
export HINDSIGHT_API_LLM_API_KEY=<deepseek_key>
export PYTHONPATH=/mnt/c/oloai
/root/olo_linux_venv/bin/python -m pytest tests/olo/m3/test_m3_gate.py -m integration -v
# 18 passed, 2 failed in 211.90s
```
