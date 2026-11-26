# Post-audit remediation, batch 3: Calendar, ledger ambiguity, Linux verification

This batch has three parts:
- it restores Linux (WSL) verification;
- it gives Google Calendar events a production path into the commitment ledger;
- it settles keyless commitments that share a date without guessing.

It builds on batch 2 (`eef5afbc`). The Hermes agent integration is not part of it.

## 1. WSL verification baseline

### Root cause (confirmed)

- **Fallback mode.** Every WSL boot on this machine logs "Failed to configure network (networkingMode Nat), falling back to networkingMode VirtioProxy".
- **Loopback detour.** VirtioProxy installs the policy route `1: from all ipproto tcp lookup 127` (`127.0.0.1 via 169.254.73.152 dev loopback0`). Every TCP connection to 127.0.0.1 therefore goes through the Windows host relay, not the kernel loopback.
- **Effect.** The relay refuses ports it has not yet picked up.
  - On freshly bound ports, the default namespace accepted 5 of 200 connections (`ECONNREFUSED`).
  - A private network namespace accepted 200 of 200 (`unshare -n`, then `ip link set lo up`): same machine, same moment, same probe.
- **What it is not.** The cause is the route, not the fake servers, urllib or a firewall. Disabling the Hyper-V firewall integration was tested: `firewall=false` in `.wslconfig` was added at the user's request, still fell back, and was reverted (the file was restored byte for byte).
- **Still unknown.** Why the Windows Host Network Service fails to create the NAT network. `hnsdiag` and the WSL logs need administrator rights. The Windows Defender Firewall service (`mpssvc`) is disabled on this machine; that is one candidate, but it is unconfirmed.

### Verification approach

- The WSL suite runs inside a private network namespace (`unshare -n`). There the fake Google, DeepSeek and Hindsight servers use the kernel loopback.
- No production networking assumption changes.
  - Olo still reaches Hindsight on `127.0.0.1`.
  - The workaround is scoped to the test process.
  - No retries were added.
- Production on this machine still depends on the relay. `status` reports an unreachable Hindsight as `bank_unprepared` (batch 1). The lasting fix is administrative: repair WSL NAT networking.

### Stale installation

`/root/olo_linux_venv`'s editable install maps `hermes_agent` to `/root/olo-linux`, a stale non-git copy.

- A script run by path (a cron shim, the OAuth helper scripts) gets only its own directory on `sys.path`, so its imports fell through to that copy.
- Batch 2 pinned `PYTHONPATH` only in the cron tests.
- Now an autouse fixture in `tests/olo/conftest.py` prepends this checkout to `PYTHONPATH` for every Olo test, and so for every child process.
- `test_subprocesses_import_this_repository` runs a script by path and asserts that both `olo` and `hermes_cli` resolve inside the repository.

### `test_m3_config` (setup.sh `HINDSIGHT_API_TENANT_*`)

The test was stale. setup.sh was correct.

- Hindsight 1.2.1 reads the tenant extension through its extension loader: `server.py` calls `load_extension("TENANT", TenantExtension)`.
- The loader reads `HINDSIGHT_API_TENANT_EXTENSION` and passes every other `HINDSIGHT_API_TENANT_*` variable to the extension as lower-cased config (`_collect_config`).
- `ApiKeyTenantExtension` requires `config["api_key"]`.
- None of these names is an `ENV_*` constant in `config.py`, the only source the test consulted.

The test now proves the two variables against the pinned server instead of allow-listing them. It runs the pinned Hindsight venv's own loader with setup.sh's extension, and requires:
- version 1.2.1;
- the configured class loads;
- the configured key is accepted;
- another business's key is rejected (C4, as proven live).

It runs offline. It is skipped where the pinned venv is absent, as the config fetch was before. Any other unknown variable still fails.

## 2. Calendar → ledger (`olo/calendar_commitments.py`)

### Path

1. `GcalAdapter` (unchanged sync: composite cursor, 410 self-heal, round-robin) yields `calendar.event.changed` / `.cancelled`, with the full event resource as a blob.
2. Ingest stores it in SQLite.
3. The identity stage resolves the single customer; several customers is `ambiguous`.
4. The commitment stage applies the event deterministically, with no model call and no budget.
5. The ledger records it.
6. The projection sends the signal document (with `commitment_id`) and the ledger documents to Hindsight.

### Event identity and authority

- **Identity.**
  - **Key.** `gcal:<calendar_id>:<event_id>` is a provider key. The ledger matches it by exact key only, and only calendar source kinds may carry one, so a model can never mint one.
  - **Source document.** Each revision is its own source document, `<key>@<updated>`, so replays and duplicate deliveries are UNCHANGED.
- **Authority.**
  - A revision binds the business when the business organises the event or has accepted the invitation.
  - An unaccepted or tentative invitation is no commitment.
  - Declining an accepted invitation cancels it.

### How each change is handled

| Calendar change | Ledger |
|---|---|
| Event created / confirmed | `commit` (meeting) |
| Date or time moved | `confirm_change` with the old date as `prior_confirmed_date` (same-date time moves: `no_change`) |
| Attendee response, title, notes, location | `no_change`: never a new commitment |
| A second customer added | the identity becomes `ambiguous`; the ledger is untouched |
| Customer replaced | `ledger_unresolved` ("customer changed"), never a second commitment; later revisions wait behind it |
| Cancelled (full resource or the deleted-event stub) | `cancel` with the cancelled date; history kept |
| Recurring series master | `recurring_series` (no single date) |
| Moved / cancelled occurrence (exception) | its own commitment under the occurrence id, with the original date as `prior_confirmed_date`; a cancelled occurrence of an untracked series is `no_commitment` |
| Older revision delivered after a newer one | `stale_revision`: never rolls back |
| Same revision delivered again | ingest de-duplicates it; the ledger is UNCHANGED |
| Expired sync token (410) | full resync of that calendar; unchanged events de-duplicate and changes apply |
| Two unrelated events on the same day | two commitments (different keys) |

### Deleted-event stubs

Incremental sync returns a deleted event as a stub (`id`, `status`, `updated`) with no attendees, so the identity stage found no customer. The cancellation therefore never reached the ledger. This test failure was found in this batch.

The stub now inherits the customer that the event's earlier revisions were verified for (or its series'). The event id is the provider's own record. Two different customers make it `ambiguous`.

### Migration

- Calendar signals ingested before this batch were tracked as `skipped` with no outcome. `reopen_calendar_signals` re-queues them once, when the commitment stage runs.
- New payloads also carry `original_start`, `sequence` and `ical_uid`. Older signals are read from their stored event document.

## 3. Ledger ambiguity and operator resolution

### The defect

Commitment A is in thread t1 on 2024-04-10. Commitment B is in thread t2 on the same date with no order number; it is left unresolved (correctly: a date cannot tell a restatement from a second commitment). A later customer request in thread t2 then matched A by date. A probe run against `eef5afbc` confirms it: `updated` (applied to A).

### The fix (`olo/ledger.py`)

- **Pending identities.** An open unresolved `commit` is a pending identity that every later event is checked against (`_blocking_commits`):
  - an event anchored to it (same verified Gmail thread id, or same key) belongs to it and waits;
  - an event matched only by date competes with it, so it is never applied to one of two equally plausible commitments;
  - a key or thread match to a recorded commitment stays decisive.
- **Nothing is dropped.** Every unresolved event keeps its row (`unresolved_matches`), with `reason` and `blocked_by`. Its signal is `commitment_status='unresolved'`. `status` shows `unresolved_ledger={"open", "waiting_behind_another"}` and the operator count.

### Operator resolution (`olo/operator.py`, `hermes olo operator|review|resolve`)

- **DenyAll is unchanged.** The `OperatorToken` stubs (`create_operator_token`, `resolve_unresolved_match`, `resolve_identifier_conflict`) still always raise.
- **The new mechanism.**
  - **Credentials.** Enrolment stores a salted PBKDF2-SHA256 passphrase hash per operator. The first operator of a business bootstraps; later enrolments and all revocations need an existing operator's grant.
  - **Grants.** `authorize` exchanges the passphrase for a 15-minute grant (random secret, stored hashed). Every decision re-verifies the grant against olo.db, so forged, expired, revoked or wrong-business grants are refused.
  - **Terminal only.** The CLI reads passphrases from an interactive terminal only (`getpass`), never from argv, the environment or a pipe.
- **Decisions.**
  - **`link`.** Apply the event to a commitment of the same customer and type; the ledger rules still apply.
  - **`distinct`.** Record a `commit` as a new commitment.
  - **`dismiss`.** Settle the event with no ledger change.
  - **Identities and identifiers.**
    - An ambiguous signal can be assigned only to a contact it names.
    - A conflicted identifier can be assigned only to one of its claimants. Losers' identifiers are revoked, never deleted.
  - **Replay.** After a decision, the events waiting behind it replay through the normal matcher. Whatever is still ambiguous stays unresolved.
- **Audit.** Every decision, refusal and failed authorization is a row in `operator_decisions`. SQLite triggers reject UPDATE and DELETE on it, and each row is written in the same transaction as its effect.
- **Boundary.** This protects against automated and model paths, and against mistakes. It does not protect against someone holding the business's OS account, who can edit olo.db directly.

### Migrations

All are additive and applied by `migrate()`:
- `unresolved_matches` gains `reason`, `blocked_by` and `decision_id`;
- new tables `operator_credentials`, `operator_grants` and `operator_decisions`.

## 4. Still unverified against real Google APIs

- **Calendar resources.** No live mailbox or calendar was used.
  - Untested: the shapes of real Calendar resources for moved or cancelled occurrences, deleted-event stubs, `responseStatus` changes and 410 recovery.
  - The fixtures follow the Calendar API v3 reference.
- **Calendar edge cases.**
  - Events whose new date falls outside `lookback_days` after a 410 resync are not re-delivered, so their commitment keeps its last state.
  - Events purged by Google during a long gap are never reported as cancelled.
- **Cross-source links.** A calendar event and an email about the same meeting are separate commitments until an operator links them. There is no automatic email-to-calendar linking: Gmail invitation `iCalUID`s are not used yet.
- **Gmail.** All the gaps in `POST_AUDIT_BATCH2.md` §6 remain.
