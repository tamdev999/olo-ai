# Post-audit remediation, batch 5: the installed system, live

This batch completes the unattended installation, proves the Hindsight lifecycle and the Hermes integration against the **real pinned Hindsight 1.2.1 server**, and prepares Google live acceptance (`GOOGLE_LIVE_ACCEPTANCE_PLAN.md`).

No paid call was made. No Google account was connected. No benchmark or holdout was run.

## 1. Unattended installation (`hermes olo setup … --schedule`)

- **Jobs:**
  - With `--schedule`, setup creates or updates the Hermes cron jobs itself. Jobs are idempotent by name; a re-run reports `unchanged`.
  - With `--enable-projection` it also:
    - configures the business's Hindsight service (`--hindsight-venv`, `--hindsight-llm-key-file`, `--hindsight-hf-home`);
    - installs the supervisor shim `olo_hindsight_<business>.py`;
    - schedules it as `olo-hindsight-<business>`, every minute.
- **Defaults:** without `--schedule`, setup prints the `hermes cron create` commands, as before. Without `--enable-projection`, it installs no supervisor shim and no job (disabled by default).
- **Interpreter:** jobs pin the interpreter that ran setup (cron's `interpreter`).
- **Shims:**
  - **Pinned to this checkout:** every shim now puts the checkout that installed it first on `sys.path`, and refuses to run if `olo` or `hermes_constants` resolves elsewhere.
  - **The old risk:** the previous shim tried a "direct import" first, so any `olo` earlier on `sys.path` won, such as an editable install of another copy or a stale `PYTHONPATH`. A test with a decoy package proves the new behaviour; the old shim ran the decoy.
  - **Removed:** the source-reading test of the old shim's text.
- **The supervisor shim:**
  - It runs one pass (`olo.hindsight_service.supervise_main`) and is silent while healthy.
  - It exits 1 on `crashloop` or `misconfigured`, so the cron run is recorded as failed.
- **Status:** `hermes olo status` prints the supervisor's state, pid, failures and last event. `olo.health.assess` reports:
  - a crash loop or misconfiguration (`degraded`);
  - an unhealthy, starting or restarting server (`degraded`);
  - an operator stop (`attention`);
  - a supervisor that has not run for 5 minutes (`stalled`: "is its cron job scheduled?").
- **Tests:** `tests/olo/test_installed_schedule.py`, through real `hermes` subprocesses, the real cron store and `hermes cron run`.
  - On every host: jobs are idempotent with a pinned interpreter, plain setup schedules nothing, and a shim never runs another checkout.
  - On POSIX, against a stand-in `hindsight-api`: the job starts the server, a forced kill is restarted by the next run, and a crash loop fails the job and shows as `[DEGRADED] hindsight: Hindsight crashloop`.

## 2. Real Hindsight 1.2.1 and Hermes, live (`scripts/olo/m3/b5_live`)

### How it ran

`run_live.sh`, as root in WSL:
1. **Business setup:** creates a disposable business with the **real setup.sh**:
   - provider `none` (no LLM configured);
   - tenant authentication on;
   - its own pg0 instance and port.
2. **Isolation:** runs Olo as `olo_hs` inside a **private network namespace**.
   - The server and Olo share a kernel loopback.
   - Nothing can reach the internet: no API call, no model download (embeddings come from the local cache).
3. **Data:** only Calendar data is projected. Calendar events and ledger states are structured documents with no LLM extraction, and the commitment stage's model refuses every call.
4. **Teardown:** removes the home.

Evidence: `docs/olo/m3/b5_live/evidence.json` (no keys).

### Stages (all passed, 14 of 14)

| Stage | Verified against | Result |
|---|---|---|
| configure | setup.sh registry and `.env` | port, bank and tenant key read as setup.sh wrote them |
| start_and_ready | real `hindsight-api` 1.2.1 started by `olo.hindsight_service` | healthy in 6–9 s across runs |
| tenant_auth | real server | probe healthy (anonymous request 401, business key accepted); a wrong key gets 401 |
| first_projection | real retain and listing | 2 customers, 4 documents confirmed and listed |
| metadata_consistency | real `get_document` and listing | **the listing and `get_document` both return `document_metadata` equal to what Olo sent**; tags equal; text stored verbatim. This resolves the open batch 4 risk. |
| document_replacement | real retain (replace) | after a reschedule, ledger v1 is re-retained in place: tag `ledger:superseded`, text `SUPERSEDED: …`, and each id is held once |
| forced_restart | `kill -9` of the real server, then Olo's supervisor | `restarting`, then healthy; documents persisted |
| reconcile_after_restart | real listing | reconciliation triggered by the restart event: 6 of 6 confirmed, nothing missing |
| partial_write_recovery | real `delete_document`, reconcile, retain | the deleted document was found missing and resubmitted |
| drift_recovery | real retain of a stale copy, reconcile | a stale `ledger:current` copy was detected as drift and replaced (`ledger:superseded`) |
| bank_loss_recovery | real `delete_bank`, projection | the bank was recreated and all 6 documents re-projected from olo.db |
| tool_over_real_recall | `olo_customer_context` via plugin discovery and `handle_function_call`, real recall with local embeddings | current date 2027-03-12 at 15:00 from SQLite; the superseded ledger version and the old calendar revision are under `no_longer_true`; no other customer's data |
| agent_turn | real `AIAgent` loop and real recall; the **model is scripted** (loopback, no inference) | the tool was reached through Hermes's tool-search bridge (`tool_call`), and the brief went back to the model |
| installed_cron_supervisor | `hermes olo setup --schedule`, `hermes cron run`, the installed shim, the real server | the killed server was restarted by the cron job (new pid); a second run succeeded; `status` shows `Hindsight: healthy … last_event=restarted` |

### Still simulated in this run

- **The model:** the agent turn's model replies are scripted. A real model turn needs paid inference (approval P5 in the plan).
- **The data source:** Gmail and Calendar data were Calendar API-shaped resources fed to the worker, not fetched from Google. No account is connected.
- **LLM extraction inside Hindsight:** not exercised. Provider `none` means no prose extraction and no consolidation, and any prose retain would need a paid provider.
- **The operating system:** WSL with the VirtioProxy fallback, so the run used a private namespace rather than the default network. A native Linux host or repaired WSL NAT still has to be shown.

## 3. Google live acceptance

`docs/olo/m3/GOOGLE_LIVE_ACCEPTANCE_PLAN.md` lists every scenario:
- Gmail G1–G10, Calendar C1–C8, linking L1–L3, end to end E1–E6;
- what each scenario verifies about Google's real API shapes;
- what is already covered offline;
- the two approvals needed: connecting the test account (P4) and a bounded model budget (P5).

## 4. Migrations and behaviour changes

- **Shims and the template:** existing shims keep working, but a re-run of setup replaces them with the pinned form. `olo/cron_scripts/olo_monitor.py.tmpl` is removed; the shim text now comes from `olo/hermes_plugin/schedule.py`.
- **No new tables.** The supervisor's tables and its configuration already existed (batch 4).

## 5. Environmental blockers and next prerequisites

1. **WSL NAT networking.** It fails at every boot (VirtioProxy fallback). Repairing it needs administrator rights, or use a native Linux host. Until then, unattended operation in WSL depends on the Windows relay for loopback.
2. **A dedicated test Google account and approval to connect it** (plan P1–P4).
3. **A bounded model budget and approval** (plan P5), for the prose extraction and real-model steps.
4. **Hermes's cron scheduler running unattended** (gateway or `hermes cron` ticking) on the host. In this batch, jobs were run with `hermes cron run`.
5. **An enrolled operator** and a review routine.
6. **OAuth production publishing**: Testing-mode tokens expire every 7 days.

## 6. Verification

Run in WSL inside a private network namespace, with `HERMES_TEST_FILE_RETRIES=0` so no file is retried:

| Run | Passed | Failed | Skipped |
|---|---|---|---|
| Full offline `tests/olo` (81 files) | 1,666 | 0 | 1 (pre-existing `test_m3_gate` skip) |
| Focused (11 files): cron and shim E2E, installed schedule, plugin and tool with a real agent turn, supervisor, reconciliation, calendar/email | 105 | 0 | 0 |
| Integration tests excluded by default, offline-capable | 4 | 0 | 5 ("Hindsight server not running") |
| Live: `scripts/olo/m3/b5_live` against the real Hindsight 1.2.1 | 14 of 14 stages | 0 | 0 |

- **Not run:** `test_m3_gate` integration (27, Hindsight plus an LLM), `test_google_live` (27, a real Google account), `test_s4_addition` (1, paid DeepSeek).
- **Subprocess imports:** a script run by path resolves `olo` and `hermes_cli` to this checkout under the Olo test environment, and the installed shims enforce it themselves.
- **Windows venv (6 batch 5 files):** 47 passed, 5 skipped (POSIX-only process tests and an existing skip).
