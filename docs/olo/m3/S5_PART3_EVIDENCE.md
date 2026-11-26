# S5 part 3 — live evidence and recorded issues

Recorded 2026-11-25, WSL Ubuntu 22.04, `hindsight-api-slim` 1.2.1
(`/root/olo_m3_hindsight_venv`). No paid model calls were made.

## 1. Document replacement on a disposable Hindsight bank

**Setup.** A throwaway server separate from the gate server (port 18800,
which was not running and was not touched):

- own temporary `HOME` with a copied pg0 installation and a new pg0 instance
  (`olo-s5p3-probe`), port 18899, local ONNX embeddings, RRF reranker;
- `HINDSIGHT_API_LLM_API_KEY` deliberately invalid and
  `HINDSIGHT_API_LLM_BASE_URL=http://127.0.0.1:9/v1` (a closed local port), so
  no LLM request could leave the machine;
- auto-consolidation off; bank `s5p3-replacement-probe` prepared with the
  `structured-no-llm` strategy (chunks extraction, observations disabled).

The server's start-up LLM connection check failed against the closed port
(logged as a connection error) and the server continued. The server was
stopped and its home deleted afterwards.

**Probe.** Retain document `sig:probe-1` (structured) with content A
("Order ZEBRA111 … 2024-04-10", metadata `rev=1`, tag `rev:1`), then retain the
same `document_id` with content B ("Order YAK222 … 2024-04-15", `rev=2`,
`rev:2`).

| Check | After A | After B (same document_id) |
|---|---|---|
| Documents in bank | 1 | 1 |
| `original_text` | A | **B** |
| `created_at` / `updated_at` | t1 / t1 | **t1 / t2** (same document, updated) |
| `document_metadata` / tags | rev 1 | **rev 2** (old values gone) |
| `memory_unit_count` | 1 | 1 |
| Document history | retain: facts_added 1 | retain: **facts_added 1, facts_removed 1** |
| Recall "ZEBRA111 delivery" | returns A | **returns only B** (old fact gone) |
| LLM usage stats | — | 3 traced requests, **0 input / 0 output tokens** |

**Conclusion (structured documents).** Retaining an existing `document_id`
replaces the document in place: content, tags and metadata are replaced and
the old fact is removed. No reconciliation is needed for re-projected
structured documents.

**Not verified.** Prose documents (LLM fact extraction) and observations
(consolidation) were not exercised: both require DeepSeek calls. Whether a
replaced prose document's *observations* are removed or updated is open and
needs an approved, budget-capped run on a disposable bank.

## 2. Schema upgrade of a real pre-S5 database (copy only)

A copy of `/root/gate_olo.db` (24 Nov, schema v4, pre-S2) in a private
directory, migrated three times with the current `olo.store.migrate`:

- v4 → v5; `signal_resolution` created with `commitment_outcome` and
  `commitment_attempts`; the four S5 tables created;
- `signals` 388 and `content_blobs` 240 rows before and after;
- migrations 2 and 3 changed nothing (idempotent);
- the original file's sha256 was unchanged; the copy was deleted.

The pre-S5 **v5** shape (signal_resolution without the S5 columns) is covered
offline by `tests/olo/test_schema_upgrade.py`.

## 3. Recorded intermittent test failures (cause not established)

These failures appeared during S5 verification. They are **not** shown to be
S5 regressions (S5 does not change Google adapters or cron code), but their
cause has **not** been established.

| Test | Observation |
|---|---|
| `tests/olo/test_gcal_full.py::test_tc3_added_signal_for_new_secondary_calendar` | `SourceError: google network: URLError` from the local fake Google server; failed on first attempt and passed on retry under `scripts/run_tests.sh` (flagged FLAKY); reproduced once in a sequential pytest run. |
| `tests/olo/test_google_cron_e2e.py` (assertion at line 132: `hermes cron run` exit code) | Failed once in a single-process full pytest run; passed 3/3 when run alone and passed under `scripts/run_tests.sh`. |
| `tests/olo/test_gmail_adapter.py::test_crash_mid_run_resumes_without_loss` | 2026-11-26, final S5 preflight: `SourceError: google network: URLError` from the local fake Google server in a full `scripts/run_tests.sh tests/olo` run (failed on attempt and file retry); the file passed 3/3 when run alone with retries disabled. Same symptom as `test_gcal_full` above; S5 does not touch the Gmail adapter. |
