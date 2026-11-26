# S5 pre-live fixes: model, credentials, acceptance checks, open risks

Recorded 2026-11-26 against `0ec6c3e0439a318b04c6ffcd4a8936bd4254c6f4`, after
the final pre-live code audit. No paid inference was made.

## 1. Model change: deepseek-chat → deepseek-flash, non-thinking

### Availability (non-generative check, 2026-11-26)

`GET https://api.deepseek.com/models` with the project key returned HTTP 200
with two models:

| id | name | max output | effort levels (default) |
|---|---|---|---|
| `deepseek-flash` | DeepSeek-V4.1-Flash | 393,216 | low, high, max (high) |
| `deepseek-v4-pro` | DeepSeek-V4-Pro | 393,216 | low, high, max (high) |

`deepseek-chat` is not listed. DeepSeek's change log (2026-04-24) announced
that `deepseek-chat` and `deepseek-reasoner` would be discontinued on
2026-07-24. The pricing page lists `deepseek-v4-flash` as a legacy name that
is "still accepted" for retired models.

**Not verified:** whether `deepseek-chat` still answers a chat request.
Checking that would take a generative call.

### Non-thinking mode

DeepSeek's thinking-mode guide makes thinking the default, at effort `high`.
`thinking: {"type": "disabled"}` turns it off, and it takes precedence over
`reasoning_effort`; with the OpenAI SDK it goes in `extra_body`. The pricing
page lists JSON output for `deepseek-flash`.

| Where | Before | Now |
|---|---|---|
| Extractor (`olo/extractor.py`) | `deepseek-chat`, no thinking control | `DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"`; every request sends `extra_body={"thinking": {"type": "disabled"}}`; `model_id` adds `:thinking=disabled` |
| Hindsight (`olo/m3/hindsight/setup.sh`, provider deepseek) | no model (Hindsight's default `deepseek-v4-flash`, a retired legacy name, thinking on by default); `start.sh` pinned `deepseek-chat` | `HINDSIGHT_API_LLM_MODEL=deepseek-flash`, `HINDSIGHT_API_LLM_EXTRA_BODY='{"thinking":{"type":"disabled"}}'`. Retain, consolidation and reflect inherit both settings, as Hindsight's own config loader confirmed from a generated `.env` |
| Acceptance driver | — | before any paid step, it checks the server log line, the `.env` request body, that there are no per-operation overrides, and the extractor `model_id`; it stops on any recorded reasoning token |

Hindsight 1.2.1 sends no `reasoning_effort` for `deepseek-flash`: its
`_supports_reasoning_model` treats only `v4-pro`, `reasoner`, `r1` and
`thinking` names as reasoning models. It also applies no thinking default for
the deepseek provider (only for minimax). Without the request body, Hindsight
would run `deepseek-flash` in thinking mode.

**Cache.** The extraction cache key contains `model_id`, so answers cached
under `deepseek-chat` are never reused for `deepseek-flash`.
`EXTRACTION_VERSION` stays `2`, because the prompt and verification rules are
unchanged.

**Historical results.** The following are unchanged and remain records of
`deepseek-chat`:
- the S4 live accuracy files (`s4_live_accuracy_*.json`);
- the label change log;
- the gate evidence and its fingerprints.

`LIVE_MODEL` in `tests/olo/test_s4_addition.py` now follows the extractor
default. A rerun on `deepseek-flash` would be a new measurement, not a
reproduction.

## 2. Prices

`olo/m3/hindsight/budget.py` is now the only price definition, and the
extractor imports it. The prices are deepseek-flash peak list prices from the
DeepSeek pricing page, checked 2026-11-26:

- input (cache miss): $0.30 per million tokens, previously $0.14;
- output: $1.20 per million tokens, previously $0.28.

Off-peak prices are half, and the peak prices apply throughout. Reasoning
tokens count as output:
- the extractor adds `completion_tokens_details.reasoning_tokens` to
  `completion_tokens`, an upper bound whichever way the API counts them;
- the usage guard adds Hindsight's `tokens.thoughts`.

All controls are client-side, and work the server has accepted can finish
after a stop. No hard monetary cap is claimed.

## 3. Credentials in start.sh

Before this change, the key was passed as `env -i … HINDSIGHT_API_LLM_API_KEY=<key>`
on the `sudo` command line. It was visible in `ps` for the server's lifetime;
the audit demonstrated this with a dummy value.

Now shell builtins (`read`, `printf`) copy the key into a one-time file:
mode 600, owner `olo_hs`, inside the 700 disposable home. The server's shell
sources that file and deletes it before `exec`, and `start.sh` stops if the
file was not consumed. Authentication and tenant settings are unchanged.

A dummy-secret probe ran the real `start.sh` with a dummy key file and a
dummy key. DeepSeek's startup check refused that key with HTTP 401. Results:

| Check | Result |
|---|---|
| `ps -eo args` during start-up (21 samples) and after it | dummy key in 0 samples / 0 processes |
| one-time key file after start | gone |
| server log, business `.env`, any file under the disposable home | 0 occurrences |
| server process arguments | `hindsight-api --host 127.0.0.1 --port 18899` only |
| `/proc/<server>/environ` | mode 400, owner `olo_hs`, holds the key variable (where Hindsight reads it) |
| redacted log copy written by `down.sh` | 0 occurrences |

## 4. Acceptance harness

The judgements are in `scripts/olo/m3/s5_acceptance/checks.py`, tested in
`tests/olo/test_s5_acceptance_checks.py`. The runbook lists every check.

- **Before confirmation.** After the customer's request (run 1) and before
  the business confirms, the driver now records ledger state, documents,
  observations and recall (`3_state_before_confirmation`). It fails when any
  of the following holds:
  - the customer's proposal is confirmed in the ledger;
  - a ledger document exists for the unconfirmed version, or states the
    proposed date;
  - an observation or recall states the proposed date without request
    wording;
  - recall no longer returns the confirmed 2026-06-10.

  The proposed dates come from the ledger's unconfirmed versions, plus the
  scenario's known 2026-06-15.
- **Settling.** Not settling within 36 runs, unfinished operations, pending
  or unknown prose consolidation, stop flags, a spend stop, signals held back
  by a failed extraction and missing observations all count as
  `incomplete`. When run 1 or run 2 does not settle, the remaining paid
  steps are skipped and recorded as incomplete.
- **Verdict.** The evidence ends with `verdict = {ledger, hindsight,
  incomplete, notes, pass}`. The driver exits 0 only on a pass.
- **Kept as before.** The source messages (M1–M4), the isolation and
  authentication preflight, and the spend limits are unchanged. The
  replacement and cancellation probes now have explicit checks.
- **One invocation.** WSL shut the distro down after a 90 s gap between
  `wsl` commands, although a background process was running (the boot id
  changed). `run_once.sh` now runs start, driver and down in one session.
  The driver itself requires `Connection verified: deepseek/deepseek-flash`
  and no LLM `ERROR` line in the server log. `start.sh` fails when the server
  never becomes healthy; before this change it returned 0.

### Offline verification (no paid calls)

| Run | Setup | Result |
|---|---|---|
| Negative preflight | real `start.sh`, dummy key; DeepSeek refused it at the startup check (401) | driver exit 2 at `0_preflight`: `connection_verified` false, the LLM error recorded; no further step |
| Dry run | `start.sh` variant pointing Hindsight at a local fake LLM; scripted extractor model carrying the real `model_id` | preflight, bank, activation, run 1 and settle passed. Every one of the 4 LLM requests carried `model=deepseek-flash` and `thinking={"type":"disabled"}`, with no `reasoning_effort`. Before confirmation, no ledger or Hindsight finding. The fake LLM extracts no facts, so there was no observation: reported as **incomplete**, and run 2 and the probes were skipped (exit 1). Spend was counted ($0.0018 estimated, fake usage) |

One earlier dry-run attempt failed at Hindsight start-up, with "embedded
PostgreSQL … Instance already running" right after a previous `down.sh`. It
did not reproduce on back-to-back start/down cycles. `start.sh` now reports
the case instead of returning 0.

`down.sh` leaves pg0's `/tmp/.ses` and `/tmp/.tmp*` directories, which are
owned by `olo_hs`; some hold a `.pgpass` for the embedded local database.
They contain no DeepSeek key.

## 5. Confirmed production risks: open, not resolved

These do not affect the disposable acceptance run, which uses a new bank, a
short run and one customer. They must be fixed before real customer data.

1. **Usage-guard retention accounting.**
   - **Cause:** Hindsight 1.2.1 keeps LLM trace rows for 1 day by default,
     swept hourly (`DEFAULT_LLM_TRACE_RETENTION_DAYS = 1`). Its stats
     endpoint sums the last 7 days. The projection stage's window baseline
     is persisted for 24 hours (`olo/projection.py` `_window_baseline`), so
     rows counted in the baseline are deleted while the window is open, and
     the measured delta shrinks. `setup.sh` does not set
     `HINDSIGHT_API_LLM_TRACE_RETENTION_DAYS`.
   - **Offline probe:** at a steady rate, $2.01 of real spend inside a window
     showed as $0.00 to the guard.
   - **Status:** not fixed.
2. **Permanently failed extractions are held back silently.**
   - **Cause:** `olo/projection.py` holds every resolved prose signal whose
     `commitment_status` is `pending` or `failed`
     (`_COMMITMENT_UNSETTLED`), without checking the attempt count.
   - **Effect:** a signal that has used all its extraction attempts is never
     projected, and no run counter reports it.
   - **Offline probe:** after 5 model errors the signal stayed `failed`, with
     no document projected and no count reported.
   - **Status:** not fixed. The acceptance harness now reports it as
     incomplete.
3. **Stale ledger-state documents.** Each confirmed version stays in
   Hindsight as `ledger:<cid>:v<n>`. Superseded versions (for example v1
   after v3) carried no "current" marker, so recall could return a
   superseded date beside the current one. **Addressed after the first live
   run:** versions are now marked current or superseded from the ledger, and
   `olo.current_state` labels recall against the ledger. See
   `S5_LIVE_FINDINGS_REMEDIATION.md`. Verified offline and on a disposable
   server, not yet in a paid run.

## 6. Remaining before the paid run

- **Extraction behaviour.** It is unmeasured on `deepseek-flash`: S4 accuracy
  was recorded on `deepseek-chat`. The acceptance run is the first
  observation, and a benchmark rerun needs separate approval.
- **Live request shape.** No paid call has yet tested non-thinking mode with
  JSON output on `deepseek-flash`:
  - **Hindsight:** its startup connection check runs at `start.sh`, before
    the driver.
  - **Extractor:** a rejected request shape would make run 1 incomplete. It
    is bounded by the 8-call budget.
