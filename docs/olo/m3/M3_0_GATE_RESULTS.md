# M3.0 GATE Results

Audit repair pass applied to `olo/m3-hindsight-memory`.  
Corrections: 10 targeted fixes (see commit `fix(olo/m3): M3.0 audit repair — 10 targeted corrections`).

## Gate Status

| Gate | Description | Status | Notes |
|------|-------------|--------|-------|
| V1 | recall@10 ≥ 0.90; p95 ≤ 500 ms | NOT RUN | Requires Hindsight server + DeepSeek key. Labelled question set now ≥40 (fix #3). |
| V2 | Cross-connector identity | NOT RUN | Requires Hindsight server. Test now uses document listing (not ranked recall) to avoid semantic bias (fix #5). |
| V3 | History preserved on re-retain | NOT RUN | Requires Hindsight server. Test now verifies entity-merge re-retain loses 0 docs (fix #6). |
| V4 | Hot without Hindsight | PASS | Olo-side only; no server needed. Unchanged. |
| V5 | Crash recovery | NOT RUN | Requires server + running binary (`HINDSIGHT_BIN`). |
| V6 | Replay / rebuild | NOT RUN | Requires Hindsight server. delete_bank 405 is now a strict failure, not a silent pass (fix #7). |
| V7 | Isolation (two businesses) | NOT RUN | Requires Hindsight binary. Test now uses `setup.sh`-generated per-business configs and document listing (fix #8). |
| V8 | Provenance 100% | PASS | Structural: sha256 chain verified against olo.db. |
| V9 | No-LLM guarantee | BLOCKED | `hindsight_api` not installed in olo venv. Strict-mode test added (fix #2). |
| V10 | Idle RAM ≤ 600 MB | NOT RUN | Requires Linux + running server. PID now detected via `ss` without fixture dependency (fix #9). |
| P1 | Delivery date consolidation + history | NOT RUN | Requires DeepSeek key. |
| P2 | Invoice dispute (3 sources) | NOT RUN | Requires DeepSeek key. |
| P3 | Months-old recall | NOT RUN | Equivalent to V1 with old-event questions. Labelled set now has ≥10 old questions per entity (fix #3). |
| P4 | Contradiction / observation history | NOT RUN | Requires DeepSeek key. Contradiction signals seeded in generator (day 90–91). |
| P9 | LLM gating / cost | NOT RUN | Requires Hindsight server. Structural test added to V9 class (fix #10). |

## Audit Corrections Applied

1. **pytest.skip → `_strict_skip`** in all integration classes (V1–V10, P1–P4).  
   `OLO_GATE_STRICT=1` now converts every infrastructure skip to a hard failure.

2. **Strict-mode test** (`TestV9.test_strict_mode_no_server_fails_not_skips`): verifies `_strict_skip` raises `pytest.Failed` when `OLO_GATE_STRICT=1`.

3. **Labelled questions ≥40**: `build_labelled_questions()` now generates 42 questions (4 recent + 10 old per entity × 3 entities). Old-event count is 30 ≥ 10.

4. **V1 time-window format**: already correct (`{start, end}` not `{from, to}`). No change needed.

5. **V2 document listing**: `test_all_five_connectors_per_entity` now uses `list_all_documents_with_tags()` (pagination + local tag filter) instead of ranked recall to eliminate semantic bias.

6. **V3 entity-merge test**: `test_re_retain_loses_no_documents` now verifies a merged-entity re-retain does not decrease the document count.

7. **V6 delete_bank strictness**: 405 is now a `_strict_skip` (not a silent pass). A poll-to-zero assertion verifies the scratch bank is empty before reload.

8. **V7 setup.sh-based isolation**: test now calls `setup.sh` for each business, parses the generated `.env`, and uses `list_all_documents_with_tags` for isolation check. PIDs of two server starts are compared to verify true process restart.

9. **V10 Popen-based PID detection**: test no longer depends on `hs_client` fixture; connects directly via `HindsightConfig.from_env()` and uses `_strict_skip` throughout.

10. **P9 budget test** (`TestV9.test_p9_structured_no_llm_calls`): retaining a `strategy=structured` item must not increment the server's LLM call counter.

## Running the Gates

### Offline (no server)
```bash
python -m pytest tests/olo/m3/ -v -m 'not integration' --tb=short
```

### Full suite (requires server)
```bash
export HINDSIGHT_BIN=/path/to/hindsight-api
export HINDSIGHT_API_LLM_API_KEY=sk-...
export OLO_GATE_STRICT=1
python -m pytest tests/olo/m3/ -v -m integration --tb=short
```

## Security Controls (unchanged)
- `HindsightConfig` rejects non-loopback hosts (raises `ValueError`).
- All HTTP uses `open_credentialed_url` (C2 §4.1) — no direct `urlopen`.
- No LLM imports in `olo/m3/` outside the client wrapper.
