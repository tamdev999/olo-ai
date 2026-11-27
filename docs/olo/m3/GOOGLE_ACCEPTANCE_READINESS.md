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
| Single-account tests cannot verify received mail, invitations from a customer or attendee responses | Identity and response scenarios unverifiable | A second identity is required (section 4) |
| G3 (non-UTF-8) cannot be produced by Gmail web | G3 would be untestable | G3 needs an SMTP client (Thunderbird, or `python smtplib` with an app password of the customer account) |
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

Olo refuses write scopes at connect (`FORBIDDEN_SCOPES`: gmail.modify/compose/insert/send/settings, mail.google.com, calendar, contacts, drive). No call can change anything in Google. Every change in the acceptance data is made by hand in the accounts' own UIs.

## 3. Coverage matrix

| Requirement | Scenarios (IDs) | Mode | Identities |
|---|---|---|---|
| Sent and received mail | G1 | A | business + customer |
| Spam rescue | G5 | A | business + customer |
| Deletion, trash and restore | G6 | A | business + customer |
| Label changes (archive, star, read) | G8 | A | business |
| Drafts | G7 | A | business |
| Multipart MIME and attachments | G4 | A | business + customer |
| HTML-only, charsets | G3 | A | customer (SMTP client) |
| Quoted replies (Gmail, Outlook) | G1, G2 | A | + Outlook.com identity for G2 |
| Calendar creation | C1 | A | business + customer |
| Rescheduling, same-day time change | C2 | A | business |
| Attendee and title edits (no new commitment) | C3 | A | business |
| Invitation responses | C4 | A | customer invites the business |
| Recurring exceptions | C5 | A | business + customer |
| Cancellation (deletion stub) | C6 | A | business + customer |
| Email/Calendar UID linking | L1 (UID equality in A; single linked commitment in B), L2 (B), L3 | A / B | business + customer |
| Cursor expiration | G10, C7 | A | local cursor rewrite |
| Pagination | G9, C8 | A | business (+ customer for G9) |
| Duplicates and replays | D1 | A | local |
| Installed cron → SQLite → ledger → Hindsight | E1, E2 | A | local |
| Hermes context | E3 (the tool's brief in A; a real model turn needs B) | A / B | local |
| Restart and recovery | E4 (Hindsight killed), E5 (reboot) | A | local |
| Revoked consent | E6 (run last) | A | business |

Mode A needs no paid inference. Mode B (L2, and the commitment-extraction parts of G1, G2 and E3) needs approval P5 and a key file.

## 4. Identities

| Identity | Needed for |
|---|---|
| **Business test account** (connected to Olo; holds only synthetic data) | Every scenario |
| **Customer test account** (a second Gmail; never connected to Olo) | G1, G3, G4, G5, G6, G9, C1, C4, C5, C6, L1–L3: mail *from* a customer, invitations *from* a customer, attendee responses |
| Outlook.com test identity | G2 only. Without it, G2 is recorded `not_run` and the run cannot be `accepted` |
| An SMTP-capable sender (the customer account with an app password, or Thunderbird) | G3: Gmail web cannot send ISO-8859-1 or HTML-only mail |

Only synthetic contacts, messages and events are used. Subjects and titles carry `[OLO-<ID>]`, and checks only look at tagged data.

## 5. Activation procedure (after approval)

```bash
# 0. A host with working localhost (native Linux, or WSL with NAT repaired).
export OLO_ACCEPTANCE_HOME=/srv/olo-accept/home            # disposable, not ~/.hermes
export OLO_ACCEPTANCE_EVIDENCE=/srv/olo-accept/evidence    # outside the home
export OLO_ACCEPTANCE_ACCOUNT=olo.acceptance.biz@gmail.com # dedicated test account
export OLO_ACCEPTANCE_CONFIRM=olo.acceptance.biz@gmail.com # repeated on purpose
export OLO_ACCEPTANCE_CUSTOMER=olo.acceptance.cust@gmail.com
export HERMES_HOME=$OLO_ACCEPTANCE_HOME
mkdir -p "$OLO_ACCEPTANCE_HOME" "$OLO_ACCEPTANCE_EVIDENCE"

# 1. Disposable Hindsight business (provider none in mode A).
HERMES_HOME=$OLO_ACCEPTANCE_HOME bash olo/m3/hindsight/setup.sh accept --provider=none

# 2. *** APPROVAL REQUIRED (P4) *** connect the dedicated account (interactive consent).
hermes olo google connect --account accept --client-secret-file /secure/client_secret.json

# 3. Preflight: refuses anything but the dedicated, read-only, Testing-mode setup.
python scripts/olo/m3/google_acceptance/run.py preflight

# 4. Installed path: three jobs, structured-only projection, no model (mode A).
for src in gmail gcal; do
  hermes olo setup --source $src --option account=accept --enable-commitments \
    --enable-projection --business-id accept --bank-id olo-accept --extract-max-calls 0 \
    --projection-max-docs 50 --projection-max-prose-docs 0 --projection-budget-usd 0.5 \
    --own-email "$OLO_ACCEPTANCE_ACCOUNT" --limit 5 --max-batches 20 \
    --hindsight-hf-home /var/cache/huggingface --schedule --force
done
python scripts/olo/m3/google_acceptance/run.py action E1 --note "jobs installed"

# 5. Bootstrap, then per scenario: do the manual step, sync, record, check.
python scripts/olo/m3/google_acceptance/run.py sync
#    … perform G5 by hand in the Gmail UI …
python scripts/olo/m3/google_acceptance/run.py sync
python scripts/olo/m3/google_acceptance/run.py action G5 --note "spam 10:02, not spam 10:05"
python scripts/olo/m3/google_acceptance/run.py check G5

# 6. Local-action scenarios.
python scripts/olo/m3/google_acceptance/run.py expire-cursor gmail && \
  python scripts/olo/m3/google_acceptance/run.py sync
python scripts/olo/m3/google_acceptance/run.py expire-cursor gcal && \
  python scripts/olo/m3/google_acceptance/run.py sync
python scripts/olo/m3/google_acceptance/run.py rerun                 # D1

# 7. Final report (every scenario), then E6 last, then teardown.
python scripts/olo/m3/google_acceptance/run.py check
python scripts/olo/m3/google_acceptance/run.py teardown              # prints the steps
```

Mode B adds `--mode B --key-file /secure/deepseek.env` to `sync` and `check`, and needs approval P5.

## 6. How results are recorded

- **One outcome per scenario, never skipped:**
  - `passed`;
  - `failed`, with the observed values in `details`;
  - `error`, when the check itself raised;
  - `not_run`, when its manual action is not recorded;
  - `needs_approval`, for mode B.
- **Verdict:**
  - `accepted` (exit 0) only when every scenario passed;
  - `rejected` (exit 1) on any failure or error;
  - `incomplete` (exit 2) otherwise.
- **The checks are fixed before the run** (`scenarios.py` at the target SHA) and encode the plan's "Expected" column. A mismatch is recorded as `failed`. Neither the plan nor the checks are edited to pass; a disagreement is analysed after the run, against the recorded API shapes.
- **Evidence** (in `$OLO_ACCEPTANCE_EVIDENCE`, no credentials or message content):
  - `preflight.json`, `actions.json`, `facts.json`;
  - `api_shapes.json`;
  - `requests.jsonl` (every request: endpoint template, parameter names, budget);
  - `syncs.jsonl`;
  - `results.json`, `results.md`.
- **olo.db** keeps the source evidence (signals, blobs) in the acceptance home; it is archived or deleted at teardown.

## 7. Teardown and restoration

Run `run.py teardown` to print the steps:
1. Revoke the token (`hermes olo google disconnect --account accept`).
2. Remove the three cron jobs.
3. Stop Hindsight.
4. Archive the evidence directory.
5. Delete the acceptance home (credentials, olo.db, Hindsight data).
6. Optionally remove the OAuth test users and client.
7. Optionally empty the test accounts.

## 8. Environmental blockers

1. **WSL NAT networking fails on this machine** (the VirtioProxy fallback).
   - Scheduled jobs and Hindsight on `127.0.0.1` depend on the Windows relay, which refuses connections intermittently.
   - Run the acceptance on a native Linux host, or repair WSL NAT with administrator rights first. A private network namespace cannot be used here, because Google must be reachable.
2. **Accounts:** the test accounts (business, customer, optionally Outlook.com) and the Testing-mode OAuth client with both as test users.
3. **Approvals:** connecting the account (P4); mode B budget (P5).
4. **The Hermes cron scheduler running** for E2 and E5 (gateway, or `hermes cron tick` from the system cron).
5. **The WSL service on this machine hung** during the readiness verification: neither `wsl --shutdown` nor `wsl --shutdown --force` returned. Recovering it needs a restart of `WslService` (administrator) or a Windows reboot.
