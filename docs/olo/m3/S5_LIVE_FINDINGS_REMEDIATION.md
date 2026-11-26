# S5 live findings: root causes and remediation

The first live S5 acceptance passed at `5bf8753b`. Its evidence is preserved
in `s5_live_5bf8753b/`. It exposed memory-correctness defects that its own
checks did not test for. This document records each finding's root cause,
the fix, and what remains unverified. No paid call was made for this work.

## 1. Current operational truth

**Finding.** After the business confirmed the move from 2026-06-10 to
2026-06-15, Hindsight's main observation still read "The ORD-501 delivery is
confirmed for 2026-06-10. (… rescheduled … to 2026-06-15.)". The ledger
documents v1 (06-10) and v3 (06-15) were recalled side by side.

**Root cause.**
- **Observations.** Hindsight's consolidation LLM writes and updates
  observations. Updating the stale one is the model's choice, and the live
  run shows it appended the change instead of replacing the lead claim.
  Prompt wording cannot make this reliable.
- **Ledger documents.** Olo rendered one document per confirmed version,
  with nothing marking which version was current (render version 1).
- **Retrieval.** No retrieval path distinguished the ledger's current state
  from history. The driver's recall was a raw, unscoped Hindsight recall.

**Fix (deterministic, ledger-derived; no new state store).**
- **Ledger documents** (`olo/projection.py`, render version 2): the
  ledger's current version (`get_current_state`, the latest confirmed
  version by UTC instant) is marked `CURRENT:` in its first line, tagged
  `ledger:current` and given metadata `ledger_role=current`. Every other
  confirmed version is marked `SUPERSEDED:`, names the current state, and is
  tagged `ledger:superseded`. A new version changes the superseded
  document's fingerprint, so the projection replaces it in place under the
  same `document_id`. History stays auditable, and pending proposals still
  get no state document.
- **Customer-facing retrieval** (`olo/current_state.py`):
  - `customer_state` reads the customer's commitments from olo.db: current
    status, date and version, earlier confirmed or cancelled dates, and any
    pending customer proposal (never confirmed).
  - `recall_for_customer` recalls with the customer's entity tag only
    (`tags_match=all_strict`) and returns the ledger state first.
  - `label_memories` labels each memory against the ledger, never by its
    wording:
    - `current_ledger` / `superseded_ledger` by version number;
    - `historical` when the memory mentions a superseded or cancelled date;
    - `unconfirmed_request` when it mentions a pending proposal's date;
    - another customer's memory is excluded.
  - The live stale observation is labelled `historical`, with the note
    "2026-06-10 is no longer in force … current is scheduled, 2026-06-15
    (ledger v3)".
  - Before confirmation, any memory stating 2026-06-15 is labelled
    `unconfirmed_request`, however it is worded.

**Covered by tests** (`tests/olo/test_s5_current_state.py`):
- before and after confirmation;
- the live stale wording;
- the wrong wording "confirmed for June 15" before confirmation;
- out-of-order arrival, where the current version is the latest instant and
  not the highest version number;
- business cancellation;
- replay producing identical documents;
- tag scoping;
- exclusion of another customer.

**Limits.**
- Raw Hindsight observations can still be stale. The view labels them;
  nothing rewrites them.
- Dates are matched lexically: ISO dates and month-name forms. A relative
  date ("next Tuesday") would not be matched.
- `historical` means "mentions a date no longer in force", even when the
  memory also states the current date. The note says which date is current.
- No production customer-facing caller exists yet. `recall_for_customer` is
  the path such a caller must use.

## 2A. Quoted evidence

**Finding.** Hindsight extracted "Order ORD-501 delivery was confirmed for
2026-06-10 by the owner" from `sig:c:m2`, Alice's reply.

**Root cause.**
- The projection retained the full rendered email (`load_content(...).rendered`),
  which includes the quoted line "> Your delivery is confirmed for
  2026-06-10."
- The commitment extractor already used `authored_text`; the projection did
  not.

**Fix.**
- **Projected text:** the projection now retains the author's own text only
  (`authored_text`: `>` lines and everything from a reply or forward header
  are dropped).
- **Metadata:** each email prose document carries `text_scope=authored`, plus
  `quoted_history=omitted` when anything was dropped.
- **Original evidence:** the full original stays immutable in olo.db, and
  `content_sha256` names its blob. A test reads it back unchanged.

## 2B. Unknown strategy

**Finding.** Five warnings read "Unknown retain strategy 'prose', using
resolved config as-is".

**Root cause.**
- `olo.m3.feeder.PROSE_STRATEGY = "prose"` was used deliberately as "falls
  back to bank default". `prepare_bank` registered only `structured-no-llm`.
- Hindsight 1.2.1's `apply_strategy` (`config_resolver.py`) logs a warning
  for an unregistered name and returns the resolved config unchanged.

**Fix.**
- `prepare_bank` registers `prose` with Hindsight's resolved defaults,
  stated explicitly: `retain_extraction_mode=concise`,
  `enable_observations=true`. Behaviour is unchanged, but it is now
  verifiable.
- It verifies every Olo strategy after the PATCH. A bank where either
  strategy does not take effect is unprepared and receives nothing.
- A document whose strategy is not registered is never submitted (counted
  as `unregistered_strategy`).
- **Not changed:** the model, the scopes, and the benchmark feeder (gate
  banks still use the documented fallback, and their evidence is unchanged).

## 2C. Cancellation

**Finding.** The cancelled probe operation's document existed with one
memory unit.

**Root cause.**
- **Server side, expected:** Hindsight 1.2.1 `cancel_operation`
  (`memory_engine.py`) is cooperative. It flips the row to `cancelled`, and
  the worker stops at its next checkpoint, after the batch or document it is
  on. Nothing is rolled back.
- **Olo side, a defect:** a cancellation was recorded as `cancelled`, with
  its documents `failed` and nothing checked. The driver judged it by status
  alone.

**Fix.**
- `reconcile_documents` reads back what the server holds for an operation's
  documents.
- Every cancellation Olo records (a timeout, a usage-guard stop, or one made
  on the server) now notes in the operation detail: "cooperative cancel, not
  a rollback: persisted on server {doc: units}; absent …; unknown …". It also
  counts `cancelled_persisted`.
- Documents stay eligible for resubmission, which replaces them in place.

## 2D. Usage discrepancy (50 trace entries vs 18 HTTP lines)

**Root cause.** These are different metrics.

- **Trace:** Hindsight's trace (`llm_trace.py`) records every model call:
  LLM, embedding, rerank and decision. The stats endpoint counts all of them
  unless filtered by `call_type`.
- **Startup check:** the server's startup connection check is an HTTP
  request that is not attributed to the bank.
- **Server log:** one `HTTP Request: POST …/chat/completions` line per HTTP
  request.

**Verified on a disposable server with a local fake LLM:**

| Metric | Count |
|---|---|
| HTTP requests the fake LLM received (ground truth) | 21 (7 retain extractions, 8 consolidations, 6 other) |
| server-log chat-completion lines | 21 |
| bank trace `call_type=llm` | 20 (the 21st is the unattributed startup check) |
| bank trace `call_type=embedding` | 35, all with 0 tokens (local ONNX) |
| bank trace total | 55 = 20 + 35 |

**Live run, inferred, not verified (the bank was torn down, and the split
was not recorded then):** 18 log lines = 17 bank LLM calls + 1 startup
check, and the remaining 33 of the 50 trace entries are non-LLM calls.
Embedding rows carry no tokens, so the usage guard's token totals are LLM
tokens only.

**Fix.** The driver now records the trace split by call type, plus the log
line count kept separately. It requires the call-type sums to equal the
totals and no failed calls.

## 3. Acceptance harness

`scripts/olo/m3/s5_acceptance/checks.py` and the driver now also require:

- **Ledger-state documents:** exactly one, the ledger's current version, is
  marked current; every other confirmed version is marked superseded
  (`ledger_markers`).
- **Customer view:** ledger state as expected; every memory stating a
  superseded or proposed date carries a non-current label; only the
  current-version ledger document is labelled current; no other customer's
  memory (`current_view`). This runs before and after confirmation.
- **Prose:** authored text only, with no quoted history (`authored_prose`).
- **Strategies:** both registered as required (`bank_strategies`, which
  stops the run before any paid step); no "Unknown retain strategy" line and
  no server `ERROR` line in the log (`server_log`).
- **Cancellation:** judged by the persisted document, with Olo's
  reconciliation matching it (`cancellation`).
- **Usage:** reconciled by call type (`usage`).
- **Raw observations** that still state a superseded date are recorded as
  notes (`stale_raw_observations`).

**Re-judged:** `tests/olo/test_s5_acceptance_checks.py` runs these checks
over the preserved first-run evidence. Its recorded verdict was PASS; the
new checks flag:
- the unmarked v1/v3;
- the missing current-state view;
- the quoted history in `sig:c:m2`;
- the five strategy fallbacks;
- the unreconciled cancellation;
- the unreconciled usage.

## Status

| Finding | Status |
|---|---|
| Current vs historical (ledger documents, retrieval) | Fixed. Verified offline and on a disposable server (fake LLM) |
| Stale raw observations | Not fixed in Hindsight; labelled by the customer view. A paid run is needed to see real model output against the labels |
| Quoted evidence | Fixed. Verified on a disposable server (the retained `sig:c:m2` is authored text only) |
| Unknown strategy | Fixed. Verified on a disposable server: 0 warnings, both strategies registered |
| Cancellation | Expected server behaviour. Olo now reconciles and reports partial effects; verified on a disposable server |
| Usage discrepancy | Explained and verified on a disposable server. The live-run split is inferred |
