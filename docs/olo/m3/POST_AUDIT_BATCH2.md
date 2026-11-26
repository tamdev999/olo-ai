# Post-audit remediation, batch 2: Gmail capture and email meaning

This batch fixes the hostile audit's Gmail capture and evidence-fidelity
defects (`M1_M3_HOSTILE_AUDIT_8a0feac0.md`, D5, D6 and D8). It covers Gmail
only: Calendar and the Hermes integration are later batches.

## 1. Root causes and fixes

| Finding | Root cause | Fix |
|---|---|---|
| Mail first filed in SPAM / TRASH / DRAFT never captured (D5) | `messagesAdded` skips those labels, and label-change entries only produced `email.labels_*` signals, so a later "Not spam", restore or draft-send never fetched the message | `GmailAdapter` treats a label move out of SPAM/TRASH/DRAFT, or into INBOX/SENT, as a rescue: when the message's current labels are no longer skipped, it fetches the message and its full content (`_message_signals`). Ingest de-duplicates by id, so a message captured earlier is harmless. CHAT stays excluded |
| Deletes and label moves stopped at SQLite (D6) | Nothing consumed `email.deleted` / `email.labels_*` | New `gmail_message_state` (`olo/message_state.py`): the current mailbox state per message (active / trash / spam / deleted), applied in the ingest transaction and in crash recovery, ordered by Gmail history id (see the policy below) |
| A metadata copy whose message was deleted before its content was fetched waited forever | The commitment stage waits for `email.content` while full content is enabled | It is recorded `skipped` / `content_unavailable` once the message is `deleted` |
| A legitimate line starting `From:` truncated the authored text (D8) | `authored_text` cut at any `From: ` line | A quoted header block now needs `From:` followed by `Sent:`/`Date:` and `To:`/`Subject:` lines. Quoted history also starts at an attribution line (including Gmail's two-line wrap and common translations) or a forward marker |
| HTML `<style>` / `<script>` leaked into the text; non-UTF-8 mail was garbled (D8) | Tags were stripped but their content kept, and every part was decoded as UTF-8 | The head, style, script, title and comments are dropped. Each part is decoded with its `Content-Type` charset. Gmail `gmail_quote`, Outlook `divRplyFwdMsg` / `appendonsend` and `cite` blocks become quoted lines |
| Attachments and attached messages counted as message text | Every `text/plain` part was joined | `multipart/alternative` takes the plain alternative (else HTML). Parts with a filename or `Content-Disposition: attachment`, and `message/rfc822` subtrees, are not body text |
| Extraction and projection built their text separately | The projection rendered and quote-stripped on its own | The projection uses the extractor's `prepare_signal(...).authored_text`. Both carry `authored_sha256`; the full original remains the checksummed blob (`content_sha256`) |

## 2. Deletion and label policy

1. **Evidence is immutable.** Signals and the full original stay in olo.db. Nothing is deleted or rewritten when mail is deleted, trashed or moved.
2. **Changes are recorded as state, not inferred.** `email.deleted` gives `deleted`; current labels give `trash` / `spam` / `active`.
3. **Order comes from the history id.** A state applies only if its history id is at least the recorded one, so duplicated, replayed or reordered history cannot roll a newer state back. Signals without a history id (backfill) only fill a missing state.
4. **No automatic cancellation.** The ledger changes only on extracted, authorised events; deleting a confirmation email does not undo the confirmation.
5. **Downstream sees the state:**
   - `customer_context` marks memories whose source email is not active (`source_state`, with a note "kept as history, not a retraction");
   - every ledger version carries the `evidence_state` of its source email;
   - `hermes olo status` shows mailbox state counts.
6. **Hindsight documents are not rewritten** for a deletion; that would re-run LLM extraction. Retrieval carries the state instead.

## 3. Migrations and compatibility

- **New tables:** `gmail_message_state`, additive and created by `migrate()`.
- **Existing messages:** they have no state row and are treated as `active` until a history event reports otherwise.
- **`EXTRACTION_VERSION` 2 → 3:** rendering is part of the extraction contract. Cached answers are not reused for re-rendered text. Settled signals are not re-extracted.
- **Email documents in Hindsight:** their text is now the prepared authored text (metadata-only mail renders as `Subject: …`), and their metadata adds `authored_sha256`. An existing bank therefore re-projects each email document once, replacing it in place under the same document id. No test business has a bank yet.
- **`email.content` payloads** now include `label_ids` (new signals only).
- **`olo/content.py`** (the M2b blob reader, used only by tests) keeps its own renderer; it is not on the pipeline path.

## 4. The recurring fake Google network failure

- **Symptom:** `SourceError: google network: URLError` from the loopback fake server, intermittently in full suite runs. It never failed in isolation (450 runs).
- **Root cause:** this machine's WSL2 distro starts with "Failed to configure network (networkingMode Nat), falling back to networkingMode VirtioProxy".
  - In that mode WSL installs the policy rule `1: from all ipproto tcp lookup 127`, ahead of the local table, with `127.0.0.1 via 169.254.73.152 dev loopback0`.
  - So every TCP connection to `127.0.0.1` inside WSL goes through the Windows host's relay instead of the kernel loopback.
  - A freshly bound ephemeral port is reachable only once the relay has picked it up. Fake servers on new ports therefore saw immediate `ECONNREFUSED` under load (about 50% across 24 processes; first request after start-up).
  - During this batch the relay stopped forwarding altogether: 0 of 200 raw socket connections to a listening `127.0.0.1` port succeeded, across three WSL boots.
- **Not the cause:** the fake server, the Google client and retries. Plain `socket.create_connection` to a listening port fails in the same way, and there are no firewall rules.
- **Fix:** environmental. Restore WSL NAT networking (`wsl --shutdown`; check the Windows Host Network Service), or configure a working networking mode. Retrying in tests would only hide it.
- **Production relevance:** on this machine, Olo in WSL reaches Hindsight on `127.0.0.1:18899` through the same relay; it works only while the relay forwards. Health and `status` now report that as `bank_unprepared` with its reason (batch 1).
- **Verification for this batch** ran on the Windows-native venv (`.venv`), whose loopback is unaffected; see the report.

## 5. Gmail coverage matrix

| Business event (Gmail) | Captured as | Downstream | Fake-server test | Real API |
|---|---|---|---|---|
| Message received in INBOX | `email.received` + `email.content` (full original blob) | identity → extraction → ledger → Hindsight | yes | M2b staged live run (not rerun since M3) |
| Message sent | `email.sent` + `email.content` | as above, business actor | yes | M2b live run |
| Message arrives in SPAM, then "Not spam" | captured at the move (`_message_signals`) | as above | **yes, through the cron shim** | **unverified** (assumes Gmail's `labelsRemoved SPAM` / `labelsAdded INBOX` history entries, per the API docs) |
| Restored from TRASH | captured at the move | as above | yes | **unverified** |
| Draft sent | captured when DRAFT is removed or SENT added | as above | yes (same id) | **unverified**: Gmail may give the sent message a new id, which is then captured as `messagesAdded` |
| Draft saved / edited | excluded (a draft is not a business statement) | — | yes | M2b (H1 regression) |
| Chat | excluded | — | yes | — |
| Archived, read, starred, important | `email.labels_*` only (no refetch) | mailbox state stays `active` | yes | M2b live run (stage 2) |
| Moved to TRASH / SPAM | `email.labels_added` | state `trash` / `spam`; customer context marks its memories | yes | partly (M2b stage 2) |
| Permanently deleted | `email.deleted` | state `deleted`; ledger unchanged; memories marked; a waiting metadata copy becomes `content_unavailable` | yes | M2b stage 2 (signal only) |
| History cursor expired | gap backfill (`messages.list`, `email.content` only) | normal processing | yes | **unverified** since M2b |
| Duplicate / replayed / reordered history | ingest de-duplicates by id; state ordered by history id | — | yes | — |
| HTML-only, multipart/alternative, non-UTF-8 charsets, attachments, forwarded attachments | full original kept; readable authored text | — | yes (Gmail API-shaped fixtures) | **unverified** against real mailboxes |
| Gmail and Outlook quoted replies (plain and HTML) | quoted part kept in the original, excluded from authored text | — | yes | **unverified** against real clients |
| Messages filtered out by a user rule (skip inbox, auto-archive) | captured on arrival (no INBOX needed) | — | partly | **unverified** |
| Messages in user labels only | captured (labels are not skipped) | — | partly | **unverified** |
| Thread fetch, attachments' bytes | not fetched (attachment references only) | — | — | — |

## 6. Remaining Gmail gaps

- **Real API untested since M2b.** None of the rescue, MIME or quote behaviour has been checked against a real mailbox; the live harness is deselected by default and needs a test account.
- **No retry or backoff on 429/5xx.** The whole run fails and is retried by the next tick, while the downstream stages still run (batch 1).
- **OAuth Testing mode** expires every 7 days, and the only notification is `status`.
- **Re-fetch on any move to INBOX/SENT.** Moving an archived message back costs two API calls; ingest discards the duplicates.
- **Rescue after cursor expiry.** A message rescued from spam during a history gap is captured by the gap backfill only if it is in INBOX.
- **Hindsight documents of deleted mail stay in memory.** The deletion is visible through `customer_context`, not to raw Hindsight readers; that is a reason Hermes must use `customer_context` (batch 1).
