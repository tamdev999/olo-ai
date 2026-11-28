# M3 final real-world acceptance — readiness (prepared, not executed)

Baseline `0ce2cccbbb` (after the live Mode B B1 run, `acc20261128t021814df39`, accepted). This batch prepares the remaining real-world gaps. **None of the live steps below has run**, and nothing here claims they passed. Each needs the approval named in §5.

## 1. Workstream A — genuine second identities

**What the evidence already holds** (checked against every recorded run):

| Scenario | Best result so far | What a seeded pass proves, and what it does not |
|---|---|---|
| G1 plain reply, quoting | `passed_simulated` | Olo against Google-stored synthetic MIME. Not a reply composed by Gmail web. |
| G2 Outlook HTML | `needs_identity` | Nothing: only Outlook.com produces it. |
| G3 HTML-only + ISO-8859-1 | `passed_simulated` (live 2026-11-27, with Gmail's real transcoding of the part observed and decoded) | **Sufficient API evidence for Olo's decoding.** Gmail's stored representation is the real one. Gmail web and Outlook.com both send UTF-8, so with these identities the charset half can only ever be simulated. G2 supplies the real-client HTML half. |
| G4 attachment + forwarded .eml | `passed_simulated` | Gmail web can produce it ("Forward as attachment"), so it is **genuinely testable** with the second Google account. |
| C4 invitation responses | `needs_identity` | Nothing: the invitation must originate from another account. |
| L1 invitation UID | `needs_identity` | Nothing: needs Google's own invitation email and iCalendar UID. |
| L3 cancel after linking | `needs_identity` | Nothing: needs the genuine L1 event. |

**Minimum identities: two.** Both are owned by the operator and **neither is connected to Olo**: no OAuth, no Cloud project, no new client. The operator acts in each account's own web UI. Olo reads only the dedicated business account through its existing read-only grant, in a fresh disposable home.

1. **One second Google (consumer Gmail) account** covers G1, G4, C4, L1 and L3. Its address goes in `OLO_ACCEPTANCE_CUSTOMER`.
2. **One Outlook.com account** covers G2. Its address goes in `OLO_ACCEPTANCE_OUTLOOK`.

**L1/L3 change.** Their action now has the customer invite the business, replacing "the business invites, the customer forwards the invitation back". Google's own invitation email (with its iCalendar UID) then reaches the business's Gmail directly, with no forwarding client in between. The check is unchanged.

**Harness added** (offline-tested):
- `google_acceptance/real_identity.py`: provenance for an action recorded `--by real-identity`. The scenario passes only if its data shows a genuine second party:
  - not the seeder's `<olo-acc.…@olo-seed.example.com>` Message-ID, nor its `oloacc…` event ID;
  - from the configured, non-reserved customer;
  - carrying the client's own mark: Gmail web `@mail.gmail.com`, Outlook.com `outlook.com`, Google invitation `@google.com`;
  - for invitations, an event organized by the customer (`organizer.self` false).

  Seeded data recorded as real is `failed`, never `passed`.
- G1, criterion version 2: the customer's reply must also keep only its authored text (no quote).
- `run.py --run-id R real-plan` prints the operator's exact steps, subjects and titles for a run.
- `OLO_ACCEPTANCE_OUTLOOK` is optional. The guard refuses it if it equals the business account.

## 2. Workstream B — two unattended hours (E2, criterion version 3)

**Audit against the installed path.** On this host the scheduler is `hermes cron tick` from the `olo_hs` crontab every minute. It fires the jobs installed by `hermes olo setup --schedule`:
- `olo-gmail-livetest` every 5 minutes;
- `olo-gcal-livetest` every 15 minutes;
- `olo-hindsight-accept` every minute.

Cron's durable execution ledger (`$HERMES_HOME/cron/executions.db`) records every attempt with its origin, as read from the Mode A home: `builtin` (a scheduled fire), `direct` (`hermes cron run`), or `missed` (a skipped slot, source `scheduler`). All 8 ingest runs of the bounded 35-minute window fall inside `builtin` executions. The same ledger holds the first rejected Mode A run's 21 failed fires, each reporting `budget_exhausted`.

**Defect found in E2 version 2 (harness): fixed.** It counted every ingest run. So a worker run by hand, or a `run.py sync` loop, satisfied it; a scheduled fire that never polled its source went unnoticed; and an exhausted budget looked like Mode A's disabled model. Version 3 (`scenarios.e2`) requires:

| Distinction | Evidence |
|---|---|
| Scheduler, not a person | Every installed job (gmail, gcal, Hindsight supervisor): `builtin` completed fires with no gap over 30 minutes; no failed, unknown, cancelled, missed or `direct` fire. |
| Real executions, not health queries | Health queries create neither executions nor ingest runs. Every ingest run must fall inside a scheduled fire. |
| Polling, not a silent source | Each scheduled worker fire produced an ingest run of its source. Every installed job must exist (`missing_jobs`). |
| Disabled model vs exhausted budget | A degraded run fails its fire (`Script exited with code 1 … budget_exhausted`), and an exhausted budget in commitment health fails. `extraction_disabled` is reported as Mode A's intent. |
| Progress, not survival | The mid-window canary (seeder `E2` phase 1, one received message) was ingested by a scheduled run. |
| Window | At least 2 hours since the action. Verdicts under versions 1 or 2 are never reused. |

**The earlier bounded 35-minute E2 does not count, and it fails version 3.**

**Prepared execution** (scratch scripts `e2L_prep.sh`, `e2L_start.sh` and `e2L_finish.sh`; not run):
1. **Fresh home** `/home/olo_hs/acceptE2`, prepared offline:
   - Olo's existing credential file copied locally (no consent, no grant change);
   - `setup.sh --provider=none` and the pg0 installation;
   - `hermes olo setup --schedule` for gmail and gcal, Mode A (`--extract-max-calls 0`, structured-only projection), installing the three jobs.
2. **Start:**
   - preflight (one Gmail profile read);
   - record and check E1;
   - install the temporary `olo_hs` crontab `hermes cron tick`;
   - record E2.

   From then on nothing manual touches Olo. The scheduler bootstraps Gmail, and the supervisor starts Hindsight.
3. **At about T+60 minutes:** the seeder writes the canary (1 write).
4. **At T+122 minutes:** `run.py check E1 E2`.
5. **Teardown:** remove the crontab and the three jobs, stop Hindsight and verify no `olo_hs` postgres remains, dry-run then run the cleanup (1 message trashed). Grants are kept.

**Bounds:**
- **Duration:** about 2 h 10 min.
- **Google reads (Olo, existing read-only grant):** 24 Gmail and 8 Calendar runs at about 2–10 requests each, plus the first Calendar sync of four calendars. Expected 120–250 in total; each run is bounded by the installed `--limit 5 --max-batches 20`.
- **Seeder:** 1 write and 1 cleanup write.
- **Cost:** model $0; Hindsight $0 (provider `none`).

## 3. Workstream C — one real Hermes model turn

**Configured provider: none.** No Hermes home on this machine has a model configured: WSL root, `olo_hs`, both acceptance homes, and the Windows `%LOCALAPPDATA%\hermes`. Hermes does ship a built-in `deepseek` provider with `deepseek-flash` in its catalog, the same model and key file Olo's extraction already uses. The test configures that provider **only inside its own disposable home**, so no new model and no change to any existing Hermes configuration.

**Harness** `tests/olo/live_agent/` (offline-tested with a scripted loopback model):

- **Business.** A disposable business built by the real worker: identity, scripted extraction, ledger, projection to a loopback Hindsight. It holds:
  - ORD-1: confirmed after a confirmed change, so its first date is superseded;
  - ORD-2: confirmed, with a pending customer request;
  - ORD-3: cancelled;
  - ORD-4: confirmed.

  Hindsight also holds three deliberately stale observations and one of another customer.
- **Turn.** The ordinary `AIAgent`, with its default tool surface, plugin discovery and tool-search bridge, and the `deepseek` provider. It runs through `MeteredUpstream`, a loopback request meter, not an agent loop. The meter:
  - forwards only chat completions and answers `/models` locally;
  - refuses past the call cap, or when a request's worst case would exceed the spend cap, before the provider sees it;
  - clamps output tokens;
  - charges reported usage, or the worst case when none is reported;
  - adds the key itself, so Hermes, the home and the evidence hold only a placeholder.
- **Judgement against SQLite (`olo.ledger`):**
  - the tool was called;
  - the tool's memory holds nothing of the other customer, and no stale observation is offered as support (offline: the stale ones reach the model only under `no_longer_true`; `excluded_other_scope: 1`; `authoritative: false`);
  - the answer's JSON summary matches the ledger for every order;
  - no sentence presents a superseded date or a pending request's date as confirmed.
- **Evidence:** tool invocations, the ledger truth, the response, the meter's accounting, and per-request size and stream flag. No key and no message bodies.

**Budget:**
- **Request size:** measured offline, Hermes sends 58–67 KB per request (17 tools plus its system prompt), about 15–22k input tokens.
- **Expected:** 2–3 calls at about $0.005–0.008 each, so about **$0.01–0.02**.
- **Hard caps (the CLI may only lower them):** **5 requests, $0.03, 800 output tokens per request**, no SDK or Hermes retries (`api_max_retries: 0`).
- **Other costs:** no Google; no Hindsight inference (loopback Hindsight).

## 4. Workstream D — operator and measurement integrity

- **Ambiguous commitments are never guessed.** Covered by `tests/olo/test_ledger_ambiguity.py`:
  - a date-only request or cancellation competes with both candidates and is applied to neither;
  - operator `distinct`, `link` and `dismiss` replay through the real worker.

  `tests/olo/test_operator.py` covers the CLI `review` → `resolve` flow, mandatory reasons, cross-customer link refusal and the append-only audit. All are in the gate.
- **Immutable per-run outcomes.** Verdicts are frozen per run, scenario, action and criterion version, and reports are per run (`results_<run>.json`). A criterion change (E2 v3, G1 v2) is judged afresh, never reused.
- **Outcome classes stay distinct** (new test): `not_run`, `needs_identity`, `needs_approval`, `passed_simulated`, `passed` and `failed`. A claimed real identity without genuine data is `failed`.
- **Contamination audit:**
  - Each live run uses a fresh disposable home, its own run ID and its own evidence directory. Every check is run-scoped (`acc <run>`), so earlier failed runs' verdicts, facts and reports cannot mix in.
  - The 30-day Calendar lookback does ingest earlier runs' deleted fixture events, as cancelled tombstones (61 in the B1 home). They appear only as `uncertain` memory and never as ledger truth. They cannot satisfy an E2 canary (that is run-tagged) or a real-identity scenario (a different customer, plus provenance).
  - Trashed mail is never backfilled: a fresh home only records Gmail's history cursor.
  - Historical Mode A and B1 evidence is untouched.
- **Lint inventory:** see §6.

## 5. Approval requests (separate per workstream)

- **A:** the operator provides one second Google account and one Outlook.com account, used only in their own web UIs and never connected to Olo. Then approval to:
  - send the messages and invitations listed by `run.py real-plan`, done by hand by the operator;
  - run Olo's syncs (reads through the existing grant) in a fresh disposable home;
  - have the operator delete those messages and events afterwards.
- **B:** approval to run the prepared two-hour E2 (§2): a temporary crontab tick, the scheduler's Google reads, 1 seeder write plus 1 cleanup, $0 inference.
- **C:** approval for one paid Hermes agent turn: `deepseek-flash` via Hermes's built-in provider, key file `/home/olo_hs/deepseek.env`, hard caps 5 requests, $0.03 and 800 output tokens. DeepSeek has no provider-side cap (prepaid balance, as reported for B1).

## 6. Lint and code-health inventory

The per-commit inventory and the regression comparison are recorded in the batch report. A clean clone of each commit is checked, so untracked scratch files are excluded.
