# Google live acceptance: readiness

- **Status: ready for one controlled run, pending approval to connect the test account.**
- **Not run:** no Google account has been connected and no live Google call has been made.
- **Harness:** `scripts/olo/m3/google_acceptance` (tests: `tests/olo/test_google_acceptance_harness.py`).
- **Scenarios:** `GOOGLE_LIVE_ACCEPTANCE_PLAN.md`.

## 1. Audit of the plan and the existing harness

| Finding | Effect on the result | Resolution |
|---|---|---|
| `test_google_live.py` skipped every test when credentials were missing or broken | A failed setup reads as a clean run | With `OLO_GOOGLE_TEST_HOME` set, unloadable credentials now **fail** the run |
| No check that the connected account is the dedicated test account | A run could read a real mailbox | The preflight requires `OLO_ACCEPTANCE_ACCOUNT`, repeated in `OLO_ACCEPTANCE_CONFIRM`; the saved credentials **and** the live `users.getProfile` must match it; no other Google alias may be in the home; the home must not be the default `~/.hermes` |
| No scope or OAuth-mode check | Broader access than needed | Scopes must be **exactly** the three read-only scopes; the client must be in Testing mode |
| Evidence written into the credential home | Evidence and secrets mixed | Evidence goes to `OLO_ACCEPTANCE_EVIDENCE`, which is refused if inside the home (`OLO_GOOGLE_EVIDENCE` for the old harness) |
| No record of the real API response shapes | Fake-server assumptions stay unverified | `RecordingClient` records every endpoint's response shape (keys, types, list lengths, non-personal enums) into `api_shapes.json`; ids in paths become `{id}` |
| No request bound | Unbounded API usage | `RecordingClient` stops at `--max-requests` (default 400 per sync); worker runs use `--limit 5 --max-batches 20` |
| The plan's post-audit scenarios (rescue, mailbox state, MIME fidelity, Calendar to ledger, UID linking, supervisor, tool) existed only as prose | Nothing to run | Implemented as checks in `scenarios.py`, one per scenario ID, matching the plan's "Expected" column |
| Mode A could not exercise Calendar to ledger: commitments required a DeepSeek key, and an exhausted budget stopped the commitment stage before later calendar events | Calendar scenarios needed paid inference | `--extract-max-calls 0` is now a no-model mode (no key read; the model refuses). An exhausted budget stops only prose, and calendar events are still applied. **This was a real defect, now fixed with a regression test.** |
| Mode A would project email prose into a Hindsight with no LLM provider | Retains of prose fail | `--projection-max-prose-docs 0` projects structured documents only |
| G9 and C8 needed 120 to 300 items for pagination | Bulk creation in the test account | Setup and the harness take `--limit`/`--max-batches`; 12 items at `--limit 5` paginate |
| No explicit duplicate-delivery scenario | Duplicates untested | **D1** added: both sources re-synced twice back to back; no new or duplicate signals |
| Single-account tests cannot verify received mail, invitations from a customer or attendee responses | Identity and response scenarios unverifiable | Received mail is seeded through the separate seeder project (the 2026-11-17 design). Every scenario has an evidence class (section 3), and the four that need a genuine second party are reported `needs_identity`, never passed |
| G3 (non-UTF-8) cannot be produced by Gmail web | G3 would be untestable | The seeder imports raw ISO-8859-1 and HTML-only MIME (`passed_simulated`: Gmail's storage and API are real; the sending client is not) |
| The plan's C5 check found occurrences by title, but Google's cancelled occurrences carry no title | The cancelled occurrence would never be checked | The check follows the series ID (corrected before any live run) |
| The Hindsight supervisor identified its server by psutil's epoch `create_time()`, which on Linux adds the `/proc/stat` btime re-read on every call; WSL2 re-anchors btime | Another process took the live server for dead, `stop` left it on the port, and the next pass adopted it (the intermittent crash-loop test; E4/E5 on WSL) | Identity is now `worker_birth` (kernel start ticks since boot on Linux), as Hermes core does; regression test `test_a_boot_time_reanchor_does_not_orphan_the_server` |

## 2. Endpoints and OAuth scopes

All calls are HTTPS GETs on Olo's allow-list (`olo.google.http._ALLOWED_PATHS`), except the OAuth token exchange, refresh and revocation.

| API | Endpoint | Parameters Olo sends |
|---|---|---|
| OAuth | `https://accounts.google.com/o/oauth2/v2/auth` | consent, at connect only (PKCE, loopback redirect) |
| OAuth | `POST https://oauth2.googleapis.com/token` | code exchange and refresh |
| OAuth | `POST https://oauth2.googleapis.com/revoke` | at disconnect (teardown) |
| Gmail | `GET /gmail/v1/users/me/profile` | none (preflight and bootstrap) |
| Gmail | `GET /gmail/v1/users/me/history` | `startHistoryId`, `historyTypes`=messageAdded/messageDeleted/labelAdded/labelRemoved, `maxResults`, `pageToken` |
| Gmail | `GET /gmail/v1/users/me/messages` | backfill after an expired cursor: `q=after:`, `includeSpamTrash=false`, `maxResults`, `pageToken` |
| Gmail | `GET /gmail/v1/users/me/messages/{id}` | `format=metadata` (with `fields`, `metadataHeaders`) or `format=full` |
| Calendar | `GET /calendar/v3/users/me/calendarList` | full scan (`showDeleted`, `minAccessRole=reader`) or `syncToken`; `pageToken` |
| Calendar | `GET /calendar/v3/calendars/{calendarId}/events` | `singleEvents=false`, `showDeleted=true`, `maxResults`, `timeMin` (full) or `syncToken` (incremental), `pageToken` |

**Scopes, exactly these three** (the preflight refuses any other set):
- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/calendar.events.readonly`
- `https://www.googleapis.com/auth/calendar.calendarlist.readonly`

Olo refuses write scopes at connect (`FORBIDDEN_SCOPES`: gmail.modify/compose/insert/send/settings, mail.google.com, calendar, contacts, drive). No Olo call can change anything in Google. The acceptance data is written separately, through the seeder project's own client and token (section 4): Gmail `messages.import`/`insert`/`modify`/`trash`/`untrash`/`delete` and `drafts`, and Calendar `events` insert/patch/delete on the primary calendar with `sendUpdates=none`.

## 3. Coverage matrix and evidence classes

The run reuses the original two-project setup (2026-11-17), with no new project, client or account:

| Setup | Google Cloud project | OAuth client | Scopes | Used by |
|---|---|---|---|---|
| Olo (read-only) | `project-d6c356e7-dc2f-4daa-9f8` | Desktop `796790…` | the three read-only scopes (§2) | `hermes olo google connect`; every sync and check |
| Seeder (write) | `olo-seeder` | Desktop `396883…` | `gmail.insert`, `gmail.labels`, `mail.google.com`, `calendar` | `tests/olo/live_seed` only, with its own token file and account guard |

Both authorise the same dedicated test account (alias `livetest`).

**The "customer" is synthetic.** Fixtures come from `tests/olo/live_seed/acceptance_fixtures.py` through the seeder project: imported mail from a synthetic address on `olo-seed.example.com`, and the business's own Calendar and Gmail operations. No second Google account is used, as decided on 2026-11-17.

Each scenario has an **evidence class** (`scenarios.py`), which says what a pass proves:

| Class | Meaning | Outcome on a pass |
|---|---|---|
| `api` | Google's behaviour is verified for real (label history, deletion, pagination, the business's own Calendar writes). A seeded external party is only a stand-in that the tested assumption does not depend on. | `passed` |
| `simulated` | The assumption depends on how another party's mail client composes a message. The seeder synthesises it, so a pass proves Olo against Google-stored synthetic MIME, not against that client. | `passed_simulated` |
| `second_identity` | Only a genuine second party can produce the data | never evaluated: `needs_identity`, unless the action is recorded `--by real-identity` |

| ID | Requirement | Class | Test data |
|---|---|---|---|
| G1 | Sent and received mail; quote stripping | simulated | seeder: imported customer mail and a business reply quoting it |
| G2 | Outlook HTML reply | second_identity | needs a genuine Outlook client |
| G3 | HTML-only and ISO-8859-1 | simulated | seeder: raw MIME in both forms |
| G4 | Attachments and a forwarded message | simulated | seeder: multipart/mixed with `message/rfc822` |
| G5 | Spam rescue | api | seeder: imported into SPAM, then "not spam" |
| G6 | Trash, restore, delete forever | api | seeder: trash, untrash, delete |
| G7 | Draft saved, edited, sent | api | seeder: draft create and update, then send **to the account itself** |
| G8 | Archive, star, read | api | seeder: three label changes |
| G9 | History pagination | api | seeder: 12 messages, each followed by signal-less draft history, so Olo must follow `nextPageToken` (as the M2b H1 fixture did) |
| G10, C7 | Cursor expiry | api | `run.py expire-cursor` |
| C1 | Event created with an invitee | api | seeder: business-organised event, synthetic attendee, `sendUpdates=none` |
| C2 | Rescheduled, then same-day time change | api | seeder: two moves |
| C3 | Title and description edit | api | seeder |
| C4 | Invitation responses | second_identity | needs an invitation organised by another account |
| C5 | Recurring exceptions | api | seeder: weekly series; one occurrence moved, one cancelled |
| C6 | Deleted event | api | seeder |
| C8 | Calendar pagination | api | seeder: 12 events |
| L1 | Real invitation MIME linked by UID | second_identity | needs Google's own invitation email |
| L2 | Same-day confirmation without UID (mode B) | api | seeder: event and a business email |
| L3 | Cancellation after linking | second_identity | depends on L1 |
| D1 | Duplicate delivery | api | `run.py rerun` |
| E1–E6 | Installed path, recovery, revocation | api | operator actions; E6 revokes the grant at myaccount.google.com |

Mode A needs no paid inference. Mode B (L2, and the extraction parts of G1 and E3) needs approval P5.

**Expected verdict with this design:** `accepted_within_scope` (exit 4), listing G2, C4, L1 and L3 as `needs_identity` and G1, G3 and G4 as `passed_simulated`. `accepted` (exit 0) requires a genuine second identity for those seven.

**Check correction before any live run:** C5's check found a series by the `[OLO-C5]` title. Google guarantees only `id`, `recurringEventId` and `originalStartTime` on a cancelled occurrence, so that occurrence carries no title. The check now also follows the series ID.

## 4. Fixture generator

```
python -m tests.olo.live_seed.acceptance_fixtures new-run        # a unique run ID (local)
python -m tests.olo.live_seed.acceptance_fixtures plan           # the class and seeding steps per scenario (local)
python -m tests.olo.live_seed.acceptance_fixtures seed --run-id R --scenario G5 --phase 1
python -m tests.olo.live_seed.acceptance_fixtures status --run-id R   # the local manifest
python -m tests.olo.live_seed.acceptance_fixtures cleanup --run-id R [--dry-run] [--permanent]
```

- **Isolation:**
  - each subject or title is `[OLO-<ID>] … - acc <run>`;
  - each message carries an `X-Olo-Acceptance-Run` header, and each event a private extended property with the run ID;
  - the customer address is refused unless it is on a reserved test domain.
- **Idempotent:**
  - Message-IDs and event IDs derive from (run, key), and each create looks up the existing item first;
  - each phase is applied once (the manifest records it) and only after the earlier phases.
- **Bounded:**
  - at most `--max-writes` writes (default 80) and 200 reads per invocation;
  - Calendar writes always use `sendUpdates=none`;
  - the only sent message is G7's, to the account itself.
- **Controlled cleanup:** only the manifest's items whose run marker matches. Messages are trashed (or deleted with `--permanent`), and events deleted. `--dry-run` changes nothing.
- **Separate credentials:**
  - the seeder runs as root with `OLO_SEEDER_CLIENT_SECRET_FILE` (the `396883…` client), `OLO_SEEDER_TOKEN_FILE` and `OLO_SEEDER_ALLOWED_EMAIL`;
  - Olo's token stays in the acceptance home of `olo_hs`;
  - the generator also refuses if its account differs from `OLO_ACCEPTANCE_ACCOUNT`.
- **Proven offline** (`tests/olo/live_seed/test_acceptance_fixtures.py`): every fixture goes through the fake Google server and Olo's real adapters to the scenario's check. All `api` fixtures reach `passed`, the `simulated` ones `passed_simulated`, and seeded second-identity actions stay `needs_identity`.

## 5. Activation procedure (after approval)

```bash
# 0. WSL's default network namespace (not the private namespace of the offline
#    suite: Google must be reachable), as a non-root user (pg0 refuses root),
#    with this checkout first on the path (the venv's editable install may be
#    an older tree). Every command below runs in this shell.
sudo -iu olo_hs
export REPO="/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai" PYTHONPATH="$REPO"
alias hermes=/root/olo_linux_venv/bin/hermes python=/root/olo_linux_venv/bin/python
cd "$REPO"
export OLO_ACCEPTANCE_HOME=/home/olo_hs/accept/home        # disposable, not ~/.hermes
export OLO_ACCEPTANCE_EVIDENCE=/home/olo_hs/accept/evidence # outside the home
export OLO_ACCEPTANCE_ACCOUNT=<the livetest account>        # the existing dedicated test account
export OLO_ACCEPTANCE_CONFIRM=<the livetest account>        # repeated on purpose
export OLO_ACCEPTANCE_CUSTOMER=customer@olo-seed.example.com # synthetic (seeder)
export OLO_ACCEPTANCE_ALIAS=livetest
export HERMES_HOME=$OLO_ACCEPTANCE_HOME
mkdir -p "$OLO_ACCEPTANCE_HOME" "$OLO_ACCEPTANCE_EVIDENCE"
python scripts/olo/m3/google_acceptance/run.py environment   # no Google call; exit 3 on a refusal

# 1. Disposable Hindsight business (provider none in mode A).
HERMES_HOME=$OLO_ACCEPTANCE_HOME bash olo/m3/hindsight/setup.sh accept --provider=none

# 2. *** APPROVAL REQUIRED (P4) *** connect the dedicated account through the existing
#    read-only client (a mode-600 copy of client_secret_796790....json owned by olo_hs).
hermes olo google connect --account livetest --client-secret-file /home/olo_hs/secure/olo_client_secret.json

# 3. Preflight: refuses anything but the dedicated, read-only, Testing-mode setup.
python scripts/olo/m3/google_acceptance/run.py preflight

# 4. Installed path: three jobs, structured-only projection, no model (mode A).
for src in gmail gcal; do
  hermes olo setup --source $src --option account=$OLO_ACCEPTANCE_ALIAS --enable-commitments \
    --enable-projection --business-id accept --bank-id olo-accept --extract-max-calls 0 \
    --projection-max-docs 50 --projection-max-prose-docs 0 --projection-budget-usd 0.5 \
    --own-email "$OLO_ACCEPTANCE_ACCOUNT" --limit 5 --max-batches 20 \
    --hindsight-hf-home /var/cache/huggingface --schedule --force
done
python scripts/olo/m3/google_acceptance/run.py action E1 --note "jobs installed"

# 5. Bootstrap, then per scenario: seed each phase (in a root shell, with the seeder's own
#    credentials), sync after each, record, check. `acceptance_fixtures plan` prints the steps.
python scripts/olo/m3/google_acceptance/run.py sync
#   root shell, once: *** APPROVAL REQUIRED (P4s) *** re-authorise the seeder (existing 396883... client)
#     export OLO_SEEDER_CLIENT_SECRET_FILE=/root/olo_seeder_client_secret.json   # mode-600 copy
#     export OLO_SEEDER_ALLOWED_EMAIL=<the livetest account> OLO_ACCEPTANCE_ACCOUNT=<the same>
#     export OLO_ACCEPTANCE_CUSTOMER=customer@olo-seed.example.com
#     export OLO_ACCEPTANCE_EVIDENCE=/home/olo_hs/accept/evidence
#     python -m tests.olo.live_seed.seeder auth
#     RUN=$(python -m tests.olo.live_seed.acceptance_fixtures new-run)
#   olo_hs shell: export OLO_ACCEPTANCE_RUN=$RUN   # scopes every action, fact and check to this run
#   root:   python -m tests.olo.live_seed.acceptance_fixtures seed --run-id $RUN --scenario G5 --phase 1
python scripts/olo/m3/google_acceptance/run.py sync
#   root:   python -m tests.olo.live_seed.acceptance_fixtures seed --run-id $RUN --scenario G5 --phase 2
python scripts/olo/m3/google_acceptance/run.py sync
python scripts/olo/m3/google_acceptance/run.py action G5 --by seeder --note "run $RUN"
python scripts/olo/m3/google_acceptance/run.py check G5

# 6. Local-action scenarios.
python scripts/olo/m3/google_acceptance/run.py expire-cursor gmail && \
  python scripts/olo/m3/google_acceptance/run.py sync
python scripts/olo/m3/google_acceptance/run.py expire-cursor gcal && \
  python scripts/olo/m3/google_acceptance/run.py sync
python scripts/olo/m3/google_acceptance/run.py rerun                 # D1

# 7. Final report (every scenario), then E6 last, then cleanup and teardown.
#    Verdicts already recorded for this run are reported as checked (verdicts.jsonl);
#    `check --recheck <ID>` evaluates one again, keeping both in the log.
python scripts/olo/m3/google_acceptance/run.py check
#   root:   python -m tests.olo.live_seed.acceptance_fixtures cleanup --run-id $RUN --dry-run   # then without
python scripts/olo/m3/google_acceptance/run.py teardown              # prints the steps
```

Mode B adds `--mode B --key-file <owner-only key file>` to `sync`, `snapshot` and `check`, and needs approval P5. Its run caps, structured-only projection, backlog guard, offline `preflight-b` and the B1 sequence are in `GOOGLE_MODE_B_PLAN.md`.

The remaining real-world gaps (genuine second identities, the two-hour E2 under criterion version 3, one real Hermes agent turn) are prepared in `M3_FINAL_ACCEPTANCE_READINESS.md`; none of them has run.

## 6. How results are recorded

- **One outcome per scenario, never skipped:**
  - `passed`;
  - `passed_simulated`, for a `simulated` scenario on seeded data;
  - `failed`, with the observed values in `details`;
  - `error`, when the check itself raised;
  - `not_run`, when its action is not recorded;
  - `needs_approval`, for mode B;
  - `needs_identity`, for a `second_identity` scenario not recorded `--by real-identity`.
- **Who produced the data** is recorded with `run.py action <ID> --by operator|seeder|real-identity`, and appears in each outcome with its evidence class.
- **Run scoping.** With `--run-id` (or `$OLO_ACCEPTANCE_RUN`) actions and facts are kept per run and a check sees only data tagged `acc <run id>`, so a diagnostic run in the same home cannot change another run's verdict.
- **Point-in-time verdicts.** Each verdict is appended to `verdicts.jsonl` with when it was checked and which action it judged. A later `check` reuses it until that scenario's action is recorded again or it is re-checked with `--recheck`, so later activity (more scenarios, the E2 window, cleanup) never rewrites it. D1 judges the signal-id window recorded around its own rerun.
- **Verdict:**
  - `rejected` (exit 1) on any failure or error;
  - `incomplete` (exit 2) while anything is not run or awaits approval;
  - `accepted_within_scope` (exit 4) when the rest passed but some scenarios passed only on seeded data or need a second identity (they are listed and never counted as passed);
  - `accepted` (exit 0) only when every scenario passed for real.
- **The checks are fixed before the run** (`scenarios.py` at the target SHA) and encode the plan's "Expected" column. A mismatch is recorded as `failed`. Neither the plan nor the checks are edited to pass; a disagreement is analysed after the run, against the recorded API shapes.
- **Evidence** (in `$OLO_ACCEPTANCE_EVIDENCE`, no credentials or message content):
  - `preflight.json`, `actions.json`, `facts.json`;
  - `api_shapes.json`;
  - `requests.jsonl` (every request: endpoint template, parameter names, budget);
  - `syncs.jsonl`;
  - `results_<run id>.json` / `.md` per run (`results.json` / `.md` for an unscoped check), `verdicts.jsonl` (every verdict, append-only);
  - `fixtures.jsonl` (each seeder write: run, scenario, phase, operation, Google ID).
- **olo.db** keeps the source evidence (signals, blobs) in the acceptance home; it is archived or deleted at teardown.

## 7. Teardown and restoration

Run `run.py teardown` to print the steps:
1. Remove the run's fixtures (`acceptance_fixtures cleanup --run-id $RUN`, after a `--dry-run`).
2. Revoke the token (`hermes olo google disconnect --account livetest`).
3. Remove the three cron jobs.
4. Stop Hindsight.
5. Archive the evidence directory.
6. Delete the acceptance home (credentials, olo.db, Hindsight data).
7. Keep both Cloud projects and clients for the next run (they are reused, not recreated).

## 8. Network status and remaining blockers

### WSL networking: NAT fails, application connectivity works

These are two separate facts, measured on 2026-11-27 (WSL 2.7.10, Windows 26100.1457). The networking configuration is kept as it is: no Windows service, firewall, `.wslconfig`, networking-mode or routing change.

- **NAT initialisation fails, by design of this host.**
  - WSL logs `Failed to configure network (networkingMode Nat), falling back to networkingMode VirtioProxy` (VirtioProxy is the former name of Consomme). It has done so since the distribution was created, on 2026-11-15.
  - WSL's NAT setup adds a host firewall rule ("WSLCore SharedAccess Allow Rule"). WSL's ICS and HNS components import `FirewallAPI.dll`. The Windows Defender Firewall service is deliberately disabled on this host.
  - This is not a blocker: the fallback mode carries every connection the run needs.
- **Application connectivity in WSL's default namespace is verified:**

  | Path | Result |
  |---|---|
  | External DNS and HTTPS | work |
  | Windows browser → Linux `127.0.0.1` listener (the OAuth loopback redirect) | 5/5, through WSL's relay |
  | Real Hindsight 1.2.1 + pg0 + Olo supervisor + installed cron, `scripts/olo/m3/b5_live` with `NETNS=default` | **14/14 stages** (`b5_live/evidence_wsl_default_namespace.json`) |
  | Linux → `127.0.0.1` listener 0.25 s or more after `listen()` | 100% (5/5 at each of 0.25, 1, 3 and 8 s; 10/10 repeated) |
  | Linux → `127.0.0.1` listener at the instant of `listen()` | **refused** (5/5) |

  The Hindsight stages covered start, tenant authentication, projection, document replacement, `kill -9` and supervised restart, reconciliation, partial-write, drift and bank-loss recovery, recall, a scripted agent turn, and the installed cron supervisor.
- **The listener-registration delay.** In the fallback mode, Linux TCP to `127.0.0.1` is routed to the Windows host (`ip rule … lookup 127`), which relays it back once WSL has registered the new port, in about a quarter of a second.
  - **Residual risk:** a client that connects within that window after a server starts gets one refusal. Olo's paths tolerate it:
    - the Hindsight client retries refused idempotent requests (about 0.5 s, then 1 s);
    - projection starts with a bank-config GET and, if Hindsight is unreachable, records `bank_unprepared`, submits nothing, and retries next run;
    - the supervisor probes once a minute, counts a refused probe in the start grace period as `starting`, and restarts only after three failed passes;
    - the OAuth listener exists long before the browser redirect.
  - **Offline tests** (fake servers connected the instant they listen) still run in a private network namespace (`unshare -n` + `ip link set lo up`), where loopback is the kernel's own. That workaround does not apply to the live run, which needs Google.
- **`run.py environment`** checks this host before the run: not root; default route present (not the private namespace); `olo` and `hermes_cli` from this checkout; a `127.0.0.1` listener reachable after the registration wait.

### Remaining blockers

1. **Existing Google setup to confirm** in Cloud Console (no changes):
   - both projects and their Desktop clients (`796790…`, `396883…`) still exist;
   - both consent screens are in Testing mode;
   - the `livetest` account is a test user of both;
   - the Gmail and Calendar APIs are enabled in both.

   The client files: the read-only one is in `Music\OLO MVP\` and `/root/olo_client_secret.json`; the seeder's is in `Music\OLO MVP\`. Copy them mode 600 to where each process runs.
2. **Approvals:**
   - P4: re-consent of `livetest` through Olo's read-only client;
   - P4s: re-consent of the seeder (write scopes);
   - P5: mode B budget.

   Both earlier grants were Testing-mode tokens from 2026-11-17/18, past their 7-day limit.
3. **The Hermes cron scheduler running** for E2 and E5. For example, a crontab entry for `olo_hs`: `* * * * * HERMES_HOME=<home> PYTHONPATH=<repo> /root/olo_linux_venv/bin/hermes cron tick`.
4. **If WSL stops responding during the run:** record the time and the failing step as `error`, and stop. On 2026-11-26 the WSL service hung (neither `wsl --shutdown` nor `--shutdown --force` returned). A Windows restart recovered it; that restart ended in bugcheck 0x9F, whose driver is unidentified.
