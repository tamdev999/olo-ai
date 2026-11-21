# M3.0 Granite Gate — WIP Diagnostic Checkpoint

**Branch:** `olo/m3-hindsight-memory`  
**Date:** 2026-11-21  
**Base SHA:** `492e78a488`  
**Author:** Sonnet (diagnostic session, NOT a passing implementation)  
**Status:** INCOMPLETE — Opus review required

---

## 1. What Changed Since `492e78a488`

### Source files changed (committed below)

| File | Change |
|------|--------|
| `tests/olo/m3/conftest.py` | Warmup retries 8→18, settle 2s→5s; comments clarify 90s max wait |
| `tests/olo/m3/test_m3_gate.py` | Multiple gate test fixes (see §2) |
| `olo/m3/deploy_granite.py` | NEW: reproducible Granite R2 bank deployment with retry logic |

### Venv patch — NOT in git

**File:** `/root/olo_m3_hindsight_venv/lib/python3.14/site-packages/hindsight_api/engine/providers/openai_compatible_llm.py`  
**What:** Added `http_client=httpx2.AsyncClient(limits=httpx2.Limits(max_keepalive_connections=0, max_connections=10))` to `AsyncOpenAI(**client_kwargs)` constructor.  
**Why:** Prevents DeepSeek TCP connections from staying in CLOSE_WAIT (remote sends FIN, local never closes).  
**Re-apply:** `python3 __apply_llm_fix.py` from the repo root (WSL).  
**Status:** Applied to live venv. Confirmed syntax-clean. NOT tested to actually fix the stall in isolation (the deeper event-loop cause was not independently reproduced).

---

## 2. Gate Test Changes in `test_m3_gate.py`

### V8 `test_live_recall_provenance`
**Problem:** Scratch bank created without `prepare_hindsight_bank` → Hindsight falls back to default extraction mode → inline DeepSeek called (3-4s per item × 20 items = 60-80s) → LLM concurrency slots saturated → server event loop stalls → all subsequent tests ECONNRESET.

**Fix:** Use short warmup item ("V8 bank warmup retain") to create bank, then call `prepare_hindsight_bank`, then retain actual items. Warmup item short content (<5 words) → DeepSeek returns trivially fast.

**Verification:** V8 NC passed in run 3. V8 live provenance still fails in run 3 (cascade from P9 budget guard, NOT V8 itself).

### V6 `test_rebuild_gives_identical_document_ids` and `test_v6_negative_control`
**Problem:** `prepare_hindsight_bank` called before bank exists → PATCH on non-existent bank causes transient ECONNRESET.

**Fix:** Same warmup-then-configure pattern as V8.

**Verification:** V6 moved from FAIL to ERROR (fixture failure) in run 3 — still a cascade from P9/V8.

### V5 `test_crash_during_retain_no_loss_or_duplicate`
**Problem:** pg0 (embedded PostgreSQL) survives `_kill_process_group` because PostgreSQL daemonizes and renames its process (not matchable by `pkill -f`). Second and third startup attempts find "Instance already running (pid: X)".

**Fix:** Use `pg0 stop --name <instance>` before each retry (uses pg0's own state file, not process name matching).

**Verification:** Still fails in all 3 runs. The fix was added in run 3 but `HOME` env var for `pg0 stop` may be wrong — pg0 stores state relative to `HOME`. Needs Opus investigation.

### V7 `test_separate_pg0_instances_no_cross_results`
Same pg0 lifecycle issue as V5. Same fix applied. Not verified.

### P9 `test_p9_budget_guard_wired_to_feeder`
**Problem:** `batch_retain_to_hindsight` retains items with `strategy="structured"` on an unconfigured scratch bank → falls back to prose extraction → inline DeepSeek → server stall → `budget_guard.check()` calls stats endpoint mid-retain → ECONNRESET → `StatsUnavailableError` → `FeedError` → test FAIL in strict mode.

**Fix applied in run 2:** Changed `bank_id="main-v1"` to `bank_id=os.environ.get("HINDSIGHT_BANK_ID", "main-v1-granite")` for `UsageGuard`.

**Remaining problem:** The `batch_retain_to_hindsight` call itself still triggers DeepSeek (scratch bank not configured). The budget guard's mid-retain `check()` calls stats during the stall. 

**What still needs to be done:** Create and configure the P9 budget scratch bank BEFORE calling `batch_retain_to_hindsight`. Use short warmup item + `prepare_hindsight_bank` pattern. This was NOT done in runs 1-3.

---

## 3. Remaining Failing Tests (Run 3 — 25:45 total)

```
= 7 failed, 5 passed, 1 skipped, 27 deselected, 1 xfailed, 11 errors =
```

| Test | Status | Root Cause |
|------|--------|------------|
| P9 `test_p9_budget_guard_wired_to_feeder` | FAIL | Scratch bank triggers DeepSeek → stats endpoint stall → FeedError |
| V8 `test_live_recall_provenance` | FAIL | Cascade from P9 budget guard stall |
| V1 `test_recall_at_10` | FAIL | Cascade from P9 stall (recall ECONNRESET during 48-query loop) |
| V1 `test_v1_no_answer_leakage_nc` | FAIL | Cascade |
| V1 `test_recall_at_10_negative_control` | FAIL | Cascade |
| V5 `test_crash_during_retain_no_loss_or_duplicate` | FAIL | pg0 "Instance already running (pid: X)" — `pg0 stop` fix not yet verified |
| V7 `test_separate_pg0_instances_no_cross_results` | FAIL | Same pg0 issue |
| V2 `test_all_five_connectors_per_entity` | ERROR | Fixture ECONNRESET (cascade) |
| V2 `test_cross_connector_negative_control` | ERROR | Fixture ECONNRESET (cascade) |
| V3 `test_re_retain_same_document_id_preserves_history` | ERROR | Fixture ECONNRESET (cascade) |
| V3 `test_v3_negative_control` | ERROR | Fixture ECONNRESET (cascade) |
| V6 `test_rebuild_gives_identical_document_ids` | ERROR | Fixture ECONNRESET (cascade) |
| V6 `test_v6_negative_control` | ERROR | Fixture ECONNRESET (cascade) |
| P1 `test_new_date_in_observation...` | ERROR | Fixture ECONNRESET (cascade) |
| P1 `test_p1_negative_control` | ERROR | Fixture ECONNRESET (cascade) |
| P2 `test_all_three_sources_in_recall` | ERROR | Fixture ECONNRESET (cascade) |
| P4 `test_five_contradictions...` | ERROR | Fixture ECONNRESET (cascade) |
| P4 `test_p4_negative_control` | ERROR | Fixture ECONNRESET (cascade) |

**Tests that passed in run 3:**
- P9 `test_p9_structured_no_llm_calls` PASS
- P9 `test_p9_prose_calls_detected` PASS
- P9 `test_p9_prose_llm_calls_nc` PASS
- V8 `test_v8_live_negative_control` PASS
- V10 `test_v10_negative_control` PASS
- V10 `test_idle_rss_under_600mb` XFAIL (1,787 MB > 600 MB threshold)

---

## 4. Root Cause Analysis — Server Stall

### Hypothesis: DeepSeek CLOSE_WAIT connections
The previous session identified 3 TCP sockets in CLOSE_WAIT to `api.deepseek.com:443`. Hypothesis: httpx2 keepalive pool tries to reuse dead sockets → event loop stall.

**Fix applied:** `max_keepalive_connections=0` in `AsyncOpenAI` constructor.

**Evidence FOR:** No persistent CLOSE_WAIT sockets observed after applying fix.

**Evidence AGAINST:** Server still stalls (brief 1-5s ECONNRESET on probes) even after fix. The brief stalls correlate with DeepSeek consolidation activity (inline extraction for unconfigured banks), NOT CLOSE_WAIT.

### Confirmed root cause: Unconfigured scratch bank inline DeepSeek
When a Hindsight bank doesn't have `structured-no-llm` strategy registered (via `prepare_hindsight_bank`), items with `strategy="structured-no-llm"` fall back to the bank default (DeepSeek concise extraction). Each item takes 3-4s inline. With 20 items sequentially, this saturates LLM concurrency for 60-80s.

During this window, any HTTP request to the server that triggers an asyncio event while DeepSeek is active → ECONNRESET.

**Evidence:** Server log lines `Extract facts: 1 facts, 1 chunks from 1 contents in 3.300s` for items that should use chunks mode. Cross-referenced with `Unknown retain strategy 'structured-no-llm', using resolved config as-is` warning in server log.

### Still unexplained
- Why does the asyncio event loop briefly RST connections during inline DeepSeek processing? The LLM concurrency semaphore is async (doesn't block the loop). Suspects: uvloop internal connection handling, SSL transport state.
- Why does the loop_watchdog (stall_threshold=1.0s, poll=0.25s) NOT fire during stalls that cause ECONNRESET? The watchdog fires `call_soon_threadsafe` pings. If pings ARE being processed, the loop is not stalled per asyncio metrics — but HTTP is still being reset.

---

## 5. Bank State at Checkpoint

| Bank | Observations | Notes |
|------|-------------|-------|
| `main-v1-granite` | 963 | Granite R2 embeddings, DeepSeek consolidated, FULLY QUIESCENT |
| `main-v1` | unknown | May or may not exist (not checked since cleanup) |

---

## 6. Venv Patch — Reproduction Command

```bash
# From repo root in WSL, with venv active:
source /root/olo_linux_venv/bin/activate
cd /mnt/c/oloai
python3 __apply_llm_fix.py
# Verify:
python3 __verify_fix.py
```

---

## 7. Gate Run Commands (for Opus)

```bash
# From WSL
cd /mnt/c/oloai
source /root/olo_linux_venv/bin/activate
source /root/olo_m3_secrets/deepseek.env

# 1. Start fresh server
bash /tmp/hs_granite_final.sh > /tmp/hs_server_diag.log 2>&1 &
bash __wait_server.sh

# 2. Clean up lingering scratch banks
python3 __cleanup_scratch_banks.py

# 3. Run gate (excluding V10 benchmark)
export PYTHONPATH=/mnt/c/oloai
export HINDSIGHT_HOST=127.0.0.1
export HINDSIGHT_PORT=18800
export HINDSIGHT_BANK_ID=main-v1-granite
export HINDSIGHT_BIN=/root/olo_m3_hindsight_venv/bin/hindsight-api
export OLO_DB_PATH=/root/gate_olo.db
export OLO_GATE_STRICT=1
python3 -m pytest tests/olo/m3/test_m3_gate.py \
    -m integration \
    -k 'not test_embedding_model_comparison' \
    -v --tb=short \
    --junit-xml=/tmp/gate_granite.xml 2>&1 | tee /tmp/gate_granite.log
```

---

## 8. What Opus Needs to Do

1. **Fix P9 budget guard**: Add warmup item + `prepare_hindsight_bank` before `batch_retain_to_hindsight`. Pattern already established by `test_p9_structured_no_llm_calls`.

2. **Fix V5 pg0 lifecycle**: Verify `pg0 stop --name olo-v5gate` with correct `HOME=/home/olo_hs/v5_gate_home` actually terminates the stale pg0. Check pg0 state file location vs. the `HOME` env passed to the stop command.

3. **Confirm V8 fix is complete**: V8 live provenance should pass once P9 cascade is resolved.

4. **Run clean gate** with all fixes and confirm V1, V2, V3, P1, P2, P4 pass once cascade is eliminated.

5. **Optionally investigate** the asyncio RST mechanism: why does Hindsight send RST during inline DeepSeek when the LLM semaphore is async? This is a Hindsight venv issue, not olo-ai.

---

## 9. Files NOT Committed (stays local)

| Path | Reason |
|------|--------|
| `.pgpass` | Database credentials |
| `client_secret_*.json` | OAuth secrets (parent dir, outside repo) |
| `/tmp/hs_server_diag.log` | Server log — large, WSL-local |
| `/root/olo_m3_hindsight_venv/...` | Venv patch — not in git |
