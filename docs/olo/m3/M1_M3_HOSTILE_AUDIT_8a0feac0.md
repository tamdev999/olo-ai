# Olo M1–M3 hostile functionality audit — `8a0feac0874aa428f44be4d9bc978fe83fc1c768`

This was a read-only audit: no code or test changes, no paid calls, no
benchmark or holdout runs. It is preserved verbatim in substance as the
baseline for the post-audit remediation batches. The counterexample probes
were run offline in temporary directories; they are summarised here, and the
remediation tests reproduce them.

## 1. Executive verdict

The pieces are better than the evidence for them, and the system is not yet
integrated.

- **Solid:** durable ingestion (leases, atomic checkpoint plus signals plus
  blobs, crash recovery) and the ledger's fail-safe core.
- **Not a working system:** M3 exists only as a manually activated library.
  - Nothing installed runs it.
  - Nothing reports when it stops.
  - Nothing in Hermes consumes it.
- **Customer-facing truth is not safe:**
  - the raw memory goes stale (shown live, twice);
  - the new ledger-backed view mislabels correct dates when a customer has
    more than one commitment;
  - no enforced path keeps Hermes code from reading raw Hindsight.
- **The evidence base is mostly synthetic or mocked.** Every headline
  benchmark figure is a development-set number, and the repository's own
  tests flag answer leakage in one of them.

**Verdict:** a base for further development once the integration,
observability and retrieval contract are fixed. Not a base for unattended
operation, benchmarking claims or customers.

## 2. Actual execution paths

| Path | What really ran at `8a0feac0` |
|---|---|
| Documented install | `hermes olo setup` writes a shim with only `--source/--option`, then `hermes cron` → `olo.runner.main` → fetch → `ingest_batch` → identity → **stop**. The S5 stages could not be enabled |
| Manual S5 | `python -m olo.runner … --enable-commitments --enable-projection …`, used only by the acceptance harness |
| Benchmark | synthetic generator → `populate_olo_db` → `build_hindsight_items(production=False)` (the gate uses a separate `to_hindsight_items`) |
| Customer truth | `olo.current_state.recall_for_customer`, called only by the acceptance driver |
| Hermes | the Olo plugin is CLI-only; no tools or context |

Nothing was installed on the machine: the WSL and Windows Hermes homes had
no cron jobs, Olo data, scripts or scheduler.

## 3. Test credibility

- **Real tests silently deselected:** `pyproject.toml` has `addopts = "-m 'not integration and not live'"`. It deselects 27 integration tests in `test_m3_gate.py` and all of `test_google_live.py`; none show as skips.
- **Known invalidity carried as xfail:** `test_m3_gate.py` lists as xfail "30/48 questions have candidate pool ≤ 10" and "21 questions share > 40% content words … (answer leakage)".
- **Unrealistic fake Google:** every `format=full` message in `tests/olo/fake_google.py` is one ASCII `text/plain` part.
- **Plumbing-only fakes:** the S4 extraction tests use a model routed by text fragment, and the S5 tests use an in-memory fake Hindsight.
- **Shared assumption in the harness:** the S5 check `current_view` shares the labeller's date-only reasoning.

## 4. Benchmark validity

- **M3.0 V1 recall@10 (89.58%): discard.** It has trivial candidate pools and answer leakage.
- **Dev recall@10 (about 90.5%, 201 questions): qualify.** It is development-set retrieval only:
  - the same questions were reused across configurations;
  - per-question evaluator rules were written after the results;
  - a hit means any labelled document in the top k, and observations can never count;
  - identity is perfect (CRM fixture), the corpus is templated synthetic text, and items are built with `production=False`.
- **S4 extraction (precision 0.95 / recall 1.00): qualify.** The labels were changed after the v1 run, it covers the same 30 cases, and it was measured on the retired `deepseek-chat`.
- **Holdout v3.1:** `FROZEN_PENDING_REVIEW`; never run.

## 5–6. Google capture and cron

- Mail that first arrives in SPAM, DRAFT or CHAT is never captured; when rescued it produces only label-change signals (probe P1).
- Deletes and label changes are never consumed downstream (P7).
- Calendar events are outside the ledger, and a 410 restarts from `lookback_days`.
- Testing-mode OAuth refresh tokens expire after 7 days.
- 429/5xx responses are not retried, and a failed fetch skipped the downstream stages.
- The S5 stage counters were never persisted or printed, so the run exited 0 ("silent success"), and `status` showed ingestion only.

## 7. Memory reliability

- **Stale observation** in both live runs: "Order ORD-501's delivery is confirmed for 2026-06-10. (…rescheduled…to 2026-06-15.)".
- **Date-only labels across commitments (P5):** ORD-777's valid 06-10 was labelled `historical`, and ORD-501's correct 06-15 was labelled `unconfirmed_request` because of ORD-888's pending request.
- **Bypass paths into raw memory:**
  - the plugin catalog's `hindsight` provider (recall, reflect and retain plus automatic per-turn capture);
  - `hindsight-curator` and `hermes-memory-ui`;
  - Hindsight's own reflect and recall endpoints;
  - Olo's unscoped client recall.
- **Preparation losses:**
  - `authored_text` cut at any `From: ` line (P3);
  - HTML `<style>` leaked into the text (P4);
  - charset loss.
- **Silent holds:** permanently failed extractions, ambiguous identities and unresolved matches were held without being reported, and the operator path is DenyAll.
- **Spend loss:** the usage guard lost spend to Hindsight's 1-day trace retention.

## 8. Rolodex and ledger

- **Holds:** authority gates, idempotency, current state by UTC instant, Cc ignored (that hypothesis was refuted).
- **Same-date keyless orders (P6):** two keyless orders on the same date leave the second unresolved, and a later request attaches to the first.
- **Contact creation:** any single unknown sender becomes a contact.
- **No operator workflow.**

## 11. Defect register (as found)

- **P0:**
  - D1: the installed path never ran M3, and Hermes has no interface;
  - D2: silent degradation;
  - D3: no enforced authoritative retrieval.
- **P1:**
  - D4: cross-commitment labels;
  - D5: spam-rescued mail;
  - D6: deletes and labels not propagated;
  - D7: calendar outside the ledger;
  - D8: authored-text, HTML and charset losses;
  - D9: same-date keyless commitments, no recovery path;
  - D10: failed extractions held silently;
  - D11: usage-retention bypass;
  - D12: no retry, and a failed fetch blocked downstream.
- **P2:**
  - D13: auto-created contacts;
  - D14: request and confirmation merged into one observation;
  - D15: pg0 `/tmp/.pgpass` left behind;
  - D16: scope label in `status`.

## 13. Readiness at `8a0feac0`

| Use | Verdict |
|---|---|
| Further development | GO, with integration and the retrieval contract first |
| Benchmarking claims | NO-GO |
| Unattended test business | NO-GO |
| Real customers | NO-GO |

## Addendum (found during remediation batch 1)

The WSL test venv's editable install (`/root/olo_linux_venv`) maps Hermes to a
separate, non-git checkout, `/root/olo-linux`, dated 16–19 November, which
predates S5. `hermes` subprocesses resolved `olo` from that copy. Every
subprocess-based cron E2E test run in WSL (`test_cron_e2e.py`,
`test_google_cron_e2e.py`) therefore exercised the stale copy of the Olo code,
not the repository under test. Batch 1 pins the repository first on
`PYTHONPATH` for those subprocesses.
