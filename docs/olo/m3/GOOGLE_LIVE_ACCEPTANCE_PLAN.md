# Google live acceptance plan (prepared in post-audit batch 5; not executed)

This plan proves Olo against a **dedicated test Google account** whose mailbox and calendar hold only test data.

Nothing in this document has been run. Connecting the account (OAuth consent) and any step that calls a paid model need explicit approval first. The steps that need it are marked.

- **Existing harness:** `tests/olo/test_google_live.py`, staged with `OLO_LIVE_STAGE` 0–3 plus `failure`, covers the M2/M2b surface.
- **What this plan adds:** the post-audit behaviour (batches 2–5) and the installed end-to-end path.

## 0. Prerequisites

| # | Prerequisite | Who | Approval |
|---|---|---|---|
| P1 | A dedicated Google account, for example `olo.acceptance.<n>@gmail.com`, plus a second account as the "customer", with no other data | operator | — |
| P2 | An OAuth client in a Testing-mode Google Cloud project with the Gmail readonly, Calendar readonly and calendarList scopes; both accounts as test users | operator | — |
| P3 | A disposable HERMES_HOME and business (`hermes olo setup`, Hindsight via `setup.sh`) in WSL's default network namespace (verified despite the NAT fallback: readiness doc §8), as a non-root user; `run.py environment` passes | operator | — |
| P4 | `hermes olo google connect --account accept --client-secret-file …` (interactive consent) | operator | **yes: connects a Google account** |
| P5 | Optional: a DeepSeek key file with a hard budget, for commitment extraction from email prose | operator | **yes: paid inference** |
| P6 | An enrolled Olo operator (`hermes olo operator enroll`) for the review steps | operator | — |

Two run modes:
- **A. Without P5 (no paid inference).**
  - Covered: ingestion, identity, Calendar commitments (deterministic), mailbox state, projection of structured documents, reconciliation, the supervisor, and the tool.
  - Email commitments use the offline extraction path only: they stay `pending` with commitments disabled.
- **B. With P5.** Adds email commitment extraction. It is bounded by `--extract-max-calls`, and the cost is reported from the usage ledger.

## 1. What is already verified without a live account

| Area | Evidence (offline) |
|---|---|
| Gmail sync: history, label moves, rescue from SPAM/TRASH/DRAFT, deletion state, 404 and expired-history gaps, pagination | `test_gmail_adapter.py`, `test_gmail_full.py`, `test_gmail_fidelity.py`, `test_google_cron_e2e.py` (fake Gmail API) |
| MIME, HTML-only, charsets, quoting, attachments | `test_gmail_fidelity.py` (Gmail API-shaped payloads) |
| Calendar sync: calendarList, round-robin, 410 recovery, pagination | `test_gcal_adapter.py`, `test_gcal_full.py` |
| Calendar to ledger: reschedule, same-day time change, cancellation and deletion stubs, recurring exceptions, invitation responses, stale and duplicate revisions | `test_calendar_commitments.py`, `test_calendar_email_linking.py` |
| Email/Calendar UID linking, in either order | `test_calendar_email_linking.py` |
| Cron → SQLite → ledger → Hindsight through the installed shim | `test_s5_cron_e2e.py`, `test_installed_schedule.py` |
| Hindsight lifecycle against the **real** 1.2.1 server | `scripts/olo/m3/b5_live` (evidence in `docs/olo/m3/b5_live/evidence.json`) |
| `olo_customer_context` from a real Hermes agent turn (scripted model) | `test_customer_context_tool.py`, b5_live stage `agent_turn` |

The live run below exists to verify what the fakes assume about Google.

## 2. Live scenarios (need P4)

Each scenario is:
1. **Action:** done by hand in the Gmail or Calendar UI of the test account (or the customer account).
2. **Run:** `hermes cron run olo-gmail-accept` / `olo-gcal-accept`, or the scheduled tick.
3. **Check:** a check script reads olo.db, `hermes olo status`, the bank and the tool.

Record what Google actually returned (`phase_b_observations.json`, redacted: hashes, no bodies) wherever an assumption is involved.

### Gmail

| ID | Action | Expected in Olo | Verifies the assumption |
|---|---|---|---|
| G1 | Customer sends a plain-text order confirmation request; business replies | `email.received`/`email.content`, then `email.sent`; identity is the customer contact; authored text excludes the quoted part | Gmail threading and the quote format of Gmail web replies |
| G2 | Customer sends from an Outlook.com account (HTML, `divRplyFwdMsg` quote) | Authored text has no Outlook header block | Outlook HTML structure |
| G3 | An HTML-only newsletter-style message; a message in ISO-8859-1 or windows-1252 (send from a client that allows it) | Readable text; no style or script; correct accents | Gmail's `body.data` charset handling |
| G4 | A message with a PDF attachment and a forwarded `.eml` | Attachments not in the authored text; references kept | `message/rfc822` part shape |
| G5 | Move a customer message to Spam, then "Not spam" | The rescue fetches full content once; state goes `spam` → `active` | `labelsRemoved SPAM` / `labelsAdded INBOX` history entries |
| G6 | Trash a message, restore it; trash another and delete it forever | State `trash` → `active`; `deleted`; the ledger is unchanged; the memory is marked | `messageDeleted` history |
| G7 | Save a draft, edit it, then send it | Draft not captured; the sent message is captured once | Whether the sent draft keeps its id or gets a new one |
| G8 | Archive, star, mark read | Label signals only; no refetch | — |
| G9 | 120 messages imported in one go (a filter or a mail import) | Paginated history; every message once | `nextPageToken` on history |
| G10 | Expire the cursor (pause sync for over 7 days, or rewrite the stored `start_history_id` to an old value) | 404 then gap backfill; nothing duplicated | `historyId` expiry behaviour |

### Calendar

| ID | Action | Expected | Verifies |
|---|---|---|---|
| C1 | Business creates an event and invites the customer | Meeting commitment (`commit`), with its start time | `organizer.self` and the attendee shape |
| C2 | Move it to another day; then to another time on the same day | `confirm_change` (new date); `confirm_change` (same date, new `confirmed_start`) | `updated` ordering; offsets |
| C3 | Change only the title or description; add a second invitee from the business's own domain | `no_change` | — |
| C4 | Customer invites the business; business does not respond, then accepts, then declines | `no_commitment` → `commit` → `cancel` | `attendees[self].responseStatus` |
| C5 | Weekly recurring meeting; move one occurrence; cancel another | Series `recurring_series`; moved occurrence is its own commitment; cancelled occurrence is `no_commitment` | `recurringEventId` / `originalStartTime` shapes |
| C6 | Delete an event | The deletion stub inherits the customer; `cancel` | The minimal stub shape in incremental sync |
| C7 | Expire the sync token (stored-token rewrite, as in the M2b failure stage) | 410 then full resync; nothing duplicated | 410 behaviour |
| C8 | 300 events (an import) | Paginated; round-robin across calendars | — |

### Email and Calendar

| ID | Action | Expected | Verifies |
|---|---|---|---|
| L1 | Business creates the event (C1). The customer's account receives the invitation email; forward it to the business mailbox, or have the customer send a counter-invitation | One commitment; the invitation email links by iCalUID (`linked by calendar invitation UID`) | **Real Gmail invitation MIME**: a `text/calendar` part with `UID` equal to the event's `iCalUID` |
| L2 | The business confirms the same meeting by plain email (no invite) on the same day | `ledger_unresolved` (no UID); `hermes olo review` lists it; an operator links it | Operator workflow |
| L3 | Cancel the event after L1 | The commitment is cancelled; the late invitation does not revive it | — |

### End to end, restart and recovery

| ID | Action | Expected |
|---|---|---|
| E1 | `hermes olo setup --source gmail … --enable-projection [--enable-commitments] --schedule`, then the same for gcal | Three cron jobs (Gmail, Calendar, the Hindsight supervisor), all with the pinned interpreter |
| E2 | Let the scheduler run for 2 hours | `hermes olo status`: ingest OK, no stalled stage, the supervisor healthy, ledger documents confirmed in the bank |
| E3 | Run `olo_customer_context` for the customer from a Hermes session (mode B: a real model turn) | Current commitments from the ledger; superseded and stale memories under `no_longer_true` |
| E4 | `kill -9` the Hindsight server | The supervisor restarts it within a minute; the next projection run reconciles (`restart` trigger) |
| E5 | Reboot the machine (or `wsl --shutdown`) | After boot, cron resumes; the supervisor starts Hindsight; sync continues from its cursors |
| E6 | Revoke the OAuth grant in the Google account | Ingest fails as `auth revoked or expired`; `status` asks for a reconnect; downstream still runs |

## 3. Exit criteria

- Every G, C and L scenario matches its "Expected" column. Each assumption is recorded with the observed API shape.
- E1–E6 pass, with `hermes olo status` showing no unexplained degraded or stalled condition.
- No body text in signal payloads; no credentials in logs or evidence.
- In mode B only, the usage ledger shows spend within the approved cap.

## 4. Order of execution

1. P1–P3 and P6, then **request approval for P4**.
2. Mode A: G1–G10, C1–C8, L1–L3, E1, E2, E4–E6.
3. **Request approval for P5** (budget), then mode B: G1 and G2 with commitment extraction, and E3 with a real model.

## 5. Revision (readiness batch)

The plan is executable through `scripts/olo/m3/google_acceptance`. The procedure, endpoints, scopes, coverage matrix and blockers are in `GOOGLE_ACCEPTANCE_READINESS.md`.

- **Tagging:** test data carries `[OLO-<ID>]` in the subject or title (G6 uses `G6a` for restore and `G6b` for delete). Each check reads only its own tagged data.
- **Mode A setup:** `--extract-max-calls 0` (calendar commitments, no model) and `--projection-max-prose-docs 0` (structured documents only), so mode A needs no paid inference anywhere, including in Hindsight.
- **Pagination:** G9 and C8 use 12 items with the jobs at `--limit 5` instead of hundreds.
- **New scenario D1** (duplicate delivery): both sources re-synced twice back to back; no new or duplicate signals.
- **Identities:** a second (customer) identity is required, Outlook.com is needed only for G2, and G3 needs an SMTP-capable sender.
- **Recording:** every scenario ends `passed`, `failed`, `error`, `not_run` or `needs_approval`. The verdict is `accepted` only when all pass.

## 6. Revision: seeder-generated test data

The run keeps the 2026-11-17 architecture: one dedicated test account (`livetest`) is read through Olo's read-only project and written only through the separate `olo-seeder` project. There is no second Google account, and no manual sending.

- **Fixtures:** `tests/olo/live_seed/acceptance_fixtures.py` seeds every scenario that the API can genuinely exercise, one run ID per run.
- **Evidence classes:** each scenario is `api`, `simulated` or `second_identity` (readiness doc §3).
  - G1, G3 and G4 pass as `passed_simulated`.
  - G2, C4, L1 and L3 are reported `needs_identity` and are never passed.
  - The run's verdict is `accepted_within_scope` unless a genuine second identity performs those scenarios.
- **Corrected check:** C5's cancelled occurrence is found through its series. Google's cancelled occurrences carry no title.
