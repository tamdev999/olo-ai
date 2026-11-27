# Google Mode A — live result and remediation

The first real Mode A acceptance ran on 2026-11-27 against the `livetest` account. It used the two existing Cloud projects: Olo's read-only client, and the seeder client for writes. Its verdict was **rejected**.

This document records that result and the fixes made for it. The run's evidence stays where it was written, in the acceptance home's evidence directory. Nothing here alters it.

**Status of every fix:** verified offline; a live recheck is pending. Correctness against Google itself is established only by the bounded rerun described in section 4.

## 1. The live result (baseline `077b6fc`)

| Result | Scenarios |
|---|---|
| passed (api) | G5, G6, G7, G8, G9, G10, C1, C7, C8, E1, E3, E4; D1 (see below) |
| passed_simulated | G1, G4 |
| failed | G3 (simulated), C2, C3, C5, C6, E2 |
| needs_identity | G2, C4, L1, L3 |
| needs_approval | L2 (mode B) |
| not_run | E5, E6 (not approved) |

**D1** passed when it was checked right after its rerun. The final combined check then reported it failed: unrelated later ingestion had raised the total signal count. No duplicate content was found. This is harness defect F2.

### What Google really did

These shapes come from the recorded API evidence:

- **Gmail transcodes text parts.** A part sent as ISO-8859-1 quoted-printable (`Caf=E9`) came back from `format=full` as the UTF-8 bytes `43 61 66 c3 a9`, still labelled `charset="iso-8859-1"`.
- **Calendar returns deleted events in full.** Incremental sync returned a deleted event as the full resource with `status: cancelled`, keeping its title and start. A cancelled recurring occurrence also kept its title, start, `recurringEventId`, `originalStartTime` and the series' `iCalUID`.
- **Every occurrence shares the series' `iCalUID`.**
- **Incremental Calendar sync reports state, not history.** It returns each changed event once, in its state at fetch time.

## 2. Defects and fixes

| # | Defect (live evidence) | Fix | Regression tests (red on `077b6fc`) |
|---|---|---|---|
| A | **Calendar synced one of four calendars per run.** Every change appeared four runs late, and quick edits collapsed (C2, C3, C6). | Each run syncs a full *cycle* over every calendar (cursor `cycle`; `GcalAdapter._fetch_next`). Details follow the table. | `test_gcal_cycle.py`; seeded C1–C8 on a four-calendar account (`live_seed/test_acceptance_fixtures.py`) |
| B | **A shared `iCalUID` merged recurring occurrences.** Cancelling the 18 Dec occurrence cancelled the 11 Dec one's commitment (C5). | The UID links only a single, non-recurring event (`Revision.link_uid`). Occurrence identity remains the Google occurrence ID. The series link is the alias `series:<series key>@<original start>` (basis `series_occurrence`), which no matcher consults. | `test_calendar_commitments.py::TestRecurringOccurrenceIdentity`; seeded C5 |
| C | **Mode A reported degraded health.** It recorded `budget_exhausted` on every run, reaching "85 consecutive" (E2). | A no-model activation sets `CommitmentStageConfig.extraction_enabled=False`. Prose then waits as `extraction_disabled`, which is not a degradation, and calendar commitments are applied as usual. An exhausted *positive* budget is still degraded. | `test_mode_a_health.py`; `test_s5_cron_e2e.py::test_unattended_no_model_runs_stay_healthy` |
| D | **UTF-8 under an ISO-8859-1 label was decoded as Latin-1** (G3: "CafÃ©"). | One decoder, `olo.content.decode_text_part`, used by ingestion text and authored text (extractor and Hindsight). Its rules follow the table. | `test_gmail_fidelity.py::TestGmailTranscodedCharset`; seeded G3 with the fake transcoding as Gmail does |
| E1 | **`hermes olo hindsight stop` left the embedded pg0 running.** A graceful shutdown stops its own pg0. After the E4 kill -9, the restarted server *reused* the still-running database, and its shutdown left it running. | Stopping also fast-shuts the business's own postmasters: SIGINT first, then SIGQUIT. See below the table. | `test_hindsight_service.py::test_stop_ends_the_server_and_its_embedded_database` (Linux, real signals). The real Hindsight and pg0 check in the acceptance home is in section 3. |
| E2 | **The connect and status messages printed `['gmail.metadata', 'calendar']`** for the three read-only v2 scopes. | `GoogleAccountCredentials.scope_labels()` reports exactly the granted scopes. | `test_cli_google.py::test_status_reports_exactly_the_granted_read_only_scopes` |
| F1 | **`expire-cursor gmail` raised `TypeError`** on the bare history-id cursor. | `run.stale_cursor` | `test_expire_cursor_handles_a_bare_gmail_history_id` |
| F2 | **Checks were neither run-scoped nor point-in-time.** A diagnostic run mixed into C2/C3, and later ingestion flipped D1. | `--run-id` / `$OLO_ACCEPTANCE_RUN` scope actions, facts and checks. Verdicts are appended to `verdicts.jsonl` and reused until the scenario's action is recorded again, or until `--recheck`. D1 judges the signal-id window around its own rerun. | `test_a_check_sees_only_its_own_run`, `test_a_verdict_is_not_rewritten_by_later_activity`, `test_a_new_action_is_judged_afresh`, `test_d1_counts_duplicates_inside_its_window_only` |
| F4 | **Every `check` rewrote the one shared `results.json` / `results.md`** (found on the live re-acceptance; the first run's final report survives as `results_final_precleanup.*`). | A run-scoped check writes `results_<run id>.json` / `.md`. | `test_a_run_scoped_check_never_overwrites_another_runs_report` |
| F3 | **The cleanup dry run said "would be trashed" for items already in trash.** | `message_meta` returns the marker and labels in a single read. Items already trashed report `already in trash`. | `test_cleanup_dry_run_reports_items_already_in_trash` |

### A: the sync cycle

- **Visit order.** Calendars added mid-cycle join at the end, and removed ones are dropped. A calendar that pages `_MAX_CONSECUTIVE_CAL_PAGES` times in a row moves to the back, so one backlog cannot starve the others.
- **Resuming.** If a run's batch budget ends the run early, the cycle stays in the committed cursor and the next run resumes it.
- **Reporting.** The run is then `partial`, and ingest health is `degraded` with `coverage: {complete: false, pending, calendars}` (`FetchBatch.coverage`).
- **Legacy cursors.** A legacy `next_cal` index is dropped, and the next run starts a full cycle.

### D: decoding rules

1. **Declared UTF-8, or no label:** decode as UTF-8, replacing invalid bytes.
2. **Valid UTF-8 containing a multi-byte sequence:** this wins over any other label.
3. **Otherwise:** use the declared charset, strictly. If that fails, fall back to UTF-8 with replacement.

Rule 2's one boundary: genuine Latin-1 text that literally reads "Ã©" is read as "é". This is pinned in `test_decoding_rules`. No text is normalised.

### E1: proving database ownership

Ownership is proved by the data directory, never by a command line:

- the `postmaster.pid` under `<business home>/.pg0/instances/*/data` names the process;
- that process must run as this user;
- and its working directory must be that data directory.

A stale pid file naming any other process matches nothing. The supervisor's restart of an unhealthy server still keeps the database.

### Final state, not every edit

Incremental Calendar sync gives an event's state at fetch time. The fake now does the same: `FakeCalendarState.consume_incr` returns each changed event once, in its latest state. An event edited twice between runs is seen once: the ledger converges on its final state, and the intermediate revision is not reconstructed (`test_edits_between_runs_converge_on_the_final_state`).

With a full cycle on every run, each change is seen on the next scheduled run of the Calendar job. That is at most one Calendar job interval, 15 minutes as installed. The old worst case was one interval per calendar.

### Not migrated

Databases written before this fix may hold `ical:<series uid>` aliases recorded from a recurring occurrence, and occurrence keys merged into another occurrence. The live acceptance database holds both. No migration rewrites or splits them: the calendar path no longer consults a recurring UID, and existing commitments are left as recorded. To repair one deliberately, use the operator tools: `hermes olo review` and `resolve`.

## 3. Verification

- **Full offline `tests/olo` gate** (WSL, private network namespace, retries off): 1768 passed, 1 skipped, 0 failed. It includes the core-diff guard and the Linux lifecycle tests.
- **Focused run** (cron/shim E2E, supervisor, calendar, Mode A, harness): 218 passed.
- **Offline-capable integration tests:** 4 passed, 5 skipped.
- **Real Hindsight 1.2.1 and pg0** in the acceptance home, as olo_hs, run locally only:
  - two start/stop cycles each left 0 olo_hs postgres processes;
  - after a kill -9, the supervisor restarted the server, which reused the same postmaster; `stop` then recorded `embedded database stopped (pid …)` and left 0 processes;
  - postgres processes of other users were untouched.
- **Lint:** no new ruff or code-health findings in touched units, and no function's complexity rose past the target.
- **Red proof:** the new and changed tests, run against `077b6fc` with only the test files overlaid, failed 48 tests across all 10 files. Every defect above is among those failures.

## 4. Proposed bounded live Mode A rerun

This needs its own approval. It does not touch real data, and creates no account, client, grant or revocation.

1. **Reuse the setup.** Keep the existing acceptance home, tokens and both clients. Re-consent is needed only if a token expired (Testing tokens last 7 days from 2026-11-27).
2. **Use a new run ID.** Set `OLO_ACCEPTANCE_RUN`. Run the Gmail bootstrap first, as before.
3. **Re-seed only the failed or affected scenarios:** G3, C2, C3, C5, C6. Re-run C1 and C8 too, as the cycle regression guard.
   - **Sync once per phase.** The fix makes one sync per phase sufficient. Running four would hide the very defect.
   - **Limits:** at most about 25 seeder writes and 40 reads, and 400 requests per sync.
4. **Run E2 again:** two hours unattended through the temporary olo_hs crontab entry, which is removed afterwards. Passing means the commitment stage stays `ok`, with no degraded condition.
5. **Run E4 again:** kill -9 the server, which should restart it. Then `hermes olo hindsight stop` must leave no olo_hs postgres process, checked with `pgrep -u olo_hs`.
6. **Run the final check:** one `check` scoped to the new run. Earlier verdicts are reported as checked.
7. **Clean up:** a dry run, then the run-tagged cleanup. Then remove the cron jobs and stop Hindsight, without revoking either grant.
8. **Expected verdict:** `accepted_within_scope` at best. G1, G3 and G4 are simulated; G2, C4, L1 and L3 need a second identity; L2 needs mode B; and E5 and E6 stay `not_run` unless separately approved.

## 5. Live targeted re-acceptance (2026-11-27, run `acc20261127t2225247f1c`, code `233c647`)

The rerun used the same account, clients, grants and acceptance home, in Mode A, with one Olo sync after each seeder phase. Every verdict below comes from this run's evidence only, in `results_acc20261127t2225247f1c.*` and `verdicts.jsonl`. The first run's report is untouched (`results_final_precleanup.*`).

| Scenario | Result | Ledger / identity evidence |
|---|---|---|
| G3 | passed_simulated | Google again returned `43 61 66 c3 a9…` under `charset="iso-8859-1"`; the authored text reads "Café déjà vu", with no mojibake |
| C1 | passed | `commit 2026-11-30`, with its start time |
| C2 | passed | `commit 12-01` → `confirm_change 12-02 (prior 12-01)` → `confirm_change 12-02 17:00 (same day)` |
| C3 | passed | `applied` then `no_change`; one version |
| C5 | passed | Series (seen twice): `recurring_series`. The moved 11 Dec occurrence: `applied`, its own commitment, still scheduled, with one version. The cancelled 18 Dec occurrence: a full resource with title and start, `no_commitment`. All three revisions share one iCalUID, and no `ical:` alias was recorded. |
| C6 | passed | `commit 12-05` → `cancel`; the deleted event came back as a full resource |
| C8 | passed | 12 events over 3 pages |
| E4 | passed | kill -9, then the supervisor restarted the server in about 4 s, reusing the same postmaster. The final `stop` recorded `embedded database stopped (pid 683)` and left 0 olo_hs postgres or Hindsight processes; the other users' 5 postgres processes were untouched. |
| E2 (bounded) | passed | 35 minutes: 6 Gmail and 2 Calendar scheduled runs, all succeeded. The commitment stage stayed `ok` (0 consecutive), counting `extraction_disabled`. Calendar coverage was 4 of 4, and nothing was degraded or stalled. **The plan's two-hour E2 window was not re-run.** The harness's E2 check judges conditions at check time and does not measure duration, so an accepted two-hour E2 still needs a fresh two-hour window. |

**Requests and writes:**
- **Olo:** 13 harness syncs made 84 read requests, at most 10 in one sync against the 400 budget. Separately, the preflight made 1 profile read, and the scheduled E2 runs aren't counted by the recorder.
- **Seeder:** 25 writes and 32 reads for fixtures, plus the cleanup: 16 event deletes and 2 trashes.

**Found during the rerun and fixed:** F4, the shared `results.json`.
