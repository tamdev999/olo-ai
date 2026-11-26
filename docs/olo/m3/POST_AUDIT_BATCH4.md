# Post-audit remediation, batch 4: Hermes context, Hindsight lifecycle, cross-source meetings

This batch gives the Hermes agent a trustworthy way to read Olo, makes each business's Hindsight recoverable, and closes the Calendar gaps left by batch 3 (`eb86ef42`). Business actions stay disabled; nothing here acts on a customer's behalf.

## 1. The Hermes tool: `olo_customer_context`

The Olo plugin (`olo/hermes_plugin`) now registers one read-only tool (toolset `olo`) through `ctx.register_tool`.

- **Availability:** it is available when the active profile has an olo.db.
- **What it returns:** `olo/agent_context.py::customer_brief`, built only from `olo.current_state.customer_context` / `current_truth`:

| Section | Source | Meaning |
|---|---|---|
| `current` | commitment ledger (olo.db) | Each commitment: status, confirmed date and start time, version, superseded dates, supporting sources, evidence no longer in the mailbox. A `pending_request` is the customer's proposal, always `confirmed: false`. |
| `uncertain_ledger` | `unresolved_matches` | This customer's events the ledger could not apply, with the reason and what they wait for. |
| `warnings` | the ledger | For example, two commitments of one kind on the same date: refer to them by key or id. |
| `memory` | Hindsight recall restricted to the customer's entity tag | `authoritative: false`, grouped `supporting` / `no_longer_true` (stale or superseded) / `unconfirmed` / `uncertain`. Results from another customer's scope are excluded and counted. |
| `guidance` | fixed text | Answer from `current` only; never repeat `no_longer_true` memories as current. |

- **Identity:** the customer is named by contact id, email or E.164 phone, never by name.
  - A disputed or unknown identifier returns no customer data.
- **Scope:** the brief reads the profile's own olo.db (one business), the bound business and bank, and that business's Hindsight through its tenant key.
- **Failure:** if Hindsight is unreachable, `memory.status` says `unavailable` and the ledger is still returned.
- **Read-only:**
  - the database is opened `mode=ro`;
  - the tool issues only GETs and recall POSTs to Hindsight;
  - no Hermes core file changes.
- **No raw recall:** the tool never returns unlabelled recall. Hermes ships no Hindsight memory provider, and setup.sh keeps Hindsight's MCP endpoint off, so this tool is the agent's only route to Olo memory.

**Tests** (`tests/olo/test_customer_context_tool.py`):
- the plugin is copied into a temporary HERMES_HOME and loaded by Hermes's own discovery;
- every call goes through `model_tools.handle_function_call`;
- the business is built by the real worker with projection to a loopback Hindsight that enforces the tenant key;
- scenarios: multiple customers, two commitments on one date, a pending request, a superseded date with a stale observation, a disputed identity, an unreachable Hindsight, and a byte-identical database after calls.

## 2. Hindsight lifecycle

### Supervisor (`olo/hindsight_service.py`, `hermes olo hindsight …`)

- **`configure`** records, in olo.db:
  - the venv;
  - an optional owner-only LLM key file;
  - an optional model cache.

  The port and tenant key stay where setup.sh wrote them.
- **`start` / `stop`:**
  - the server runs detached, with cwd set to the business home (it reads `./.env`);
  - the LLM key is passed through the environment only;
  - the log goes to `businesses/<id>/hindsight.log`.
- **`supervise`** runs one pass, from cron every minute or with `--loop`. Each pass:
  - **probes** health and tenant enforcement:
    - an unauthenticated request must get 401;
    - the business key must be accepted;
  - **restarts** a dead server at once;
  - **restarts** a hung one after 3 failed passes (after a 3-minute start grace);
  - **gives up** after 5 restarts an hour (`crashloop`, needs an operator);
  - **never restarts** a misconfigured server (auth off, or another business's key on the port);
  - **respects** an operator's stop;
  - **adopts** a server it did not start.
- **Process identity** is the pid plus its creation time, never a command-line match.
- **Events** are recorded in `hindsight_service_events`.

### Reconciliation (`olo/hindsight_reconcile.py`, in the projection stage and `hermes olo hindsight reconcile`)

- **Bank lost** (bank preparation had to recreate it while olo.db holds confirmed documents): every document is unconfirmed and resubmitted under the usual caps and usage guard.
- **Full comparison** runs every `reconcile_every_hours` (24) and after every restart the supervisor records. It lists the bank and compares it with the outbox and with what olo.db wants:
  - **missing or drifted** documents: the server copy is absent, or its tags and metadata differ (for example, a superseded ledger version still tagged current). These are unconfirmed and resubmitted in place.
  - **present but unconfirmed** documents: a cancelled or failed operation wrote the document anyway. It is reported and still resubmitted in place, because Hindsight writes the document row before its extraction finishes, so presence does not prove processing.
  - **unexpected** documents: reported only.
- **Never writes or deletes:** reconciliation corrects only the outbox. Document ids are stable, so recovery never duplicates history.

### Incomplete versus permanent

`olo.health.processing` (shown by `hermes olo status`) splits unfinished work into three groups:

- **`incomplete`** settles by itself: pending identity or commitment, retrying extractions, unconfirmed documents, operations in flight.
- **`permanent`**: extractions out of attempts, and documents out of retain attempts. `hermes olo retry --extractions|--projection` re-queues them and records the re-queue; earlier failures are kept.
- **`awaiting_operator`**: unresolved ledger events, ambiguous identities, identifier conflicts (`hermes olo review` / `resolve`).

### Tests

- `tests/olo/test_hindsight_recovery.py`: bank loss, partial retain, restart with operations in flight, a cancelled retain that persisted (replaced in place, never duplicated), a stale (drifted) copy, permanent failure then re-queue, operator-only items, and bank isolation.
- `tests/olo/test_hindsight_service.py`:
  - the decision table against real child processes on every host;
  - on POSIX, the full loop against a stand-in `hindsight-api` over HTTP: kill and restart, auth off, wrong key.

## 3. Calendar and email

- **Same-day time changes:**
  - ledger versions carry `confirmed_start` (ISO 8601 with offset, on `confirmed_date`);
  - a new start time on the same day is a `confirm_change`;
  - attendee or title edits remain `no_change`;
  - `scheduled_start` appears in the ledger state, `customer_context`, the tool and the ledger documents.
  - Only timed versions add metadata, so untimed documents keep their fingerprints.
- **Linking email and Calendar:**
  - **The link:** a Gmail invitation carries the event's iCalendar UID in its `text/calendar` part (`olo.signal_content.calendar_uids`). This is the only automatic link.
  - **Aliases:** whichever source arrives first records the commitment. The UID (`ical:<uid>`) and the event key (`gcal:…`) become aliases (`commitment_aliases`), and the ledger matcher resolves a provider key through its alias. Arrival order does not matter, and a deletion stub (which has no UID) still finds the commitment.
- **No link without the UID:** a calendar event on a day that already holds a meeting recorded from email for that customer is unresolved; it is neither linked nor duplicated. Later revisions wait behind it.
  - An operator `link` records the alias, so the event's later revisions and its cancellation follow.
  - An operator `distinct` keeps them separate.
- **Out of order:** a late, older invitation is appended as history and never revives a cancelled meeting, because the current state is the latest confirmed version by time.
- **Refusals:** a UID naming another customer's meeting is refused and left unresolved.
- **Tests:** `tests/olo/test_calendar_email_linking.py`.

## 4. Migrations

All additive, applied by `migrate()`:
- **New column:** `commitment_versions.confirmed_start`.
- **New tables:**
  - `commitment_aliases`;
  - `hindsight_reconciliations`;
  - `hindsight_service`, `hindsight_service_state`, `hindsight_service_events`;
  - `hindsight_requeues`.
- **Upgrade cost:** the first projection run after upgrading reconciles the bank once (listing only).

## 5. Unresolved correctness risks

- **Hindsight document metadata.** Whether Hindsight 1.2.1 stores retain `metadata` as the listing's `document_metadata` was not confirmed from the code. Reconciliation always compares tags, and compares metadata only when the server reports it. A content change that alters neither tags nor metadata is caught by the outbox fingerprint, not by reconciliation.
- **Time zones.** Dates come from the event's own offset. The ledger does not normalise them to the business's time zone.
- **All-day events and times.** An all-day event has no start time, and all-day to timed conversions are a change.
- **No corroboration without a UID.** An email about a meeting without an invitation is never linked automatically. Same-day calendar events then always need an operator. This is deliberate, and is the main source of operator work.
- **Supervision is not continuous.** The supervisor runs a pass per minute. A crash between passes is noticed at the next one, and in-flight operations are re-driven by the projection stage.
- **Unverified against real systems:**
  - the real Hindsight server process, its start-up time and restart behaviour;
  - real Gmail invitation MIME (only the Calendar API v3 and RFC 5545 shapes were followed);
  - Hermes running a live agent turn with the tool, as opposed to `handle_function_call`.

## 6. Before unattended test-business operation

1. **WSL NAT networking:** repair it with administrator rights. Until then, Olo in WSL reaches Hindsight through the VirtioProxy relay (batch 3).
2. **A cron schedule** for `hermes olo hindsight supervise` alongside the Gmail and Calendar shims (`hermes olo setup` does not yet write it).
3. **A live smoke test** against a real Hindsight on a disposable business: supervisor start, restart and reconcile, and the tool over real recall. This needs approval for any LLM-backed retain.
4. **A live Gmail and Calendar check** with a test account: invitation UIDs, deletion stubs, the 410 resync.
5. **An enrolled operator** with a review routine for `hermes olo review` (unresolved, ambiguous and permanent items).
6. **OAuth production publishing** (Testing-mode tokens expire every 7 days).

## 7. Final verification

### Counterexamples run before the commit

Both defects were fixed, and each now has a regression test that failed before the fix:

- **A keyed email meeting** (`MTG-5`) on the same day as a calendar event became a second commitment. A keyed commit only date-checked unnamed commitments, and calendar commitments are named by their provider key. It is now unresolved for an operator; a keyed meeting on another day is still its own commitment.
- **A confirmed document olo.db no longer projects**, once absent from the bank, was marked unconfirmed and counted as unfinished work forever. Reconciliation and bank-loss recovery now only redo documents olo.db still wants; the others are reported as `retired`.

Also fixed:

- **Timestamps:** supervisor and re-queue timestamps use the projection's canonical UTC format, so they compare correctly as text.

Confirmed safe, and pinned by tests:

- a recalled memory naming another customer's ledger document is `uncertain` and never attached;
- a rebuilt bank holds byte-identical documents (content, metadata, provenance).

### Test results

Run in WSL inside a private network namespace, with `HERMES_TEST_FILE_RETRIES=0` so no file is retried:

| Run | Passed | Failed | Skipped |
|---|---|---|---|
| Full offline `tests/olo` (80 files) | 1,661 | 0 | 1 (pre-existing `test_m3_gate` skip; plus 2 expected failures) |
| Focused (10 files): cron and shim E2E, plugin with `olo_customer_context` through Hermes discovery and `handle_function_call`, Linux supervisor and restart, reconciliation and recovery, calendar/email | 100 | 0 | 0 |
| Integration tests excluded by default, offline-capable (`test_m3_config`, `test_m3_abort`, `test_contact_identity`) | 4 | 0 | 5 ("Hindsight server not running") |

- **Not run:** `test_m3_gate` integration (27, needs Hindsight and an LLM), `test_google_live` (27, a real Google account), `test_s4_addition` (1, paid DeepSeek).
- **Subprocess imports:** a script run by path resolves `hermes_cli` to `/root/olo-linux` without the repository on `PYTHONPATH`, and to this checkout with the `tests/olo` conftest's setting. `test_subprocesses_import_this_repository` passes.
- **Windows venv (focused, 10 files):** 137 passed, 3 skipped (the POSIX-only supervisor tests).
