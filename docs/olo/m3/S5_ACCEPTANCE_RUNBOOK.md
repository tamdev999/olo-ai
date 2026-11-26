# S5 live acceptance runbook

**Paid: run only with explicit approval, once.** Disposable business and bank
only; no real mailbox, production bank, benchmark or holdout.

## Command (PowerShell, Windows host)

```powershell
wsl -d Ubuntu-22.04 -u root -- bash "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/m3/s5_acceptance/run_once.sh" /root/s5_acceptance_evidence.json /root/s5_acceptance_server.redacted.log
```

`run_once.sh` runs everything in one WSL session:
- it runs `start.sh`;
- it runs `driver.py` only if `start.sh` succeeded;
- it always runs `down.sh` afterwards.

It exits with `start.sh`'s status if that failed, otherwise with the
driver's.

Do not run the three scripts as separate `wsl` commands. WSL shut the distro
down, and the server with it, after a 90 s gap between commands, although a
background process was running (observed 2026-11-26). The driver therefore
checks the server log itself, instead of relying on someone reading
`start.sh`'s output.

If `run_once.sh` is interrupted, clean up with:

```powershell
wsl -d Ubuntu-22.04 -u root -- bash "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/m3/s5_acceptance/down.sh"
```

### What each script does

- **`start.sh`:** runs the real setup.sh (provider deepseek, tenant
  authentication on) in a disposable home. It prints the `.env` model lines,
  which must be `deepseek-flash` and `{"thinking":{"type":"disabled"}}`. It
  then starts Hindsight on port 18899 with the key from
  `/root/olo_m3_secrets/deepseek.env`:
  - shell builtins copy the key into a one-time file (mode 600, owner
    `olo_hs`) in the private disposable home;
  - the server's shell sources that file and deletes it before `exec`;
  - the key reaches only the server's environment
    (`/proc/<pid>/environ`, mode 400, owner `olo_hs`). It is not in any
    process's arguments, the log or the `.env`;
  - `start.sh` stops if the one-time file was not consumed, or if the server
    does not become healthy.
- **`driver.py`:** runs the scenario once and writes the evidence JSON. Exit
  status:
  - `0`: every check passed;
  - `1`: any ledger, Hindsight or incomplete-processing failure;
  - `2`, `3` or `4`: stopped before any paid step (isolation or model,
    bank, activation).
- **`down.sh`:** stops the server and deletes every disposable file.

## Model

The model is `deepseek-flash` in non-thinking mode, for the extractor and for
every Hindsight operation (see `S5_PRELIVE_FIXES.md` §1).

- **Extractor:** `model_id` is
  `deepseek:deepseek-flash@https://api.deepseek.com/v1:thinking=disabled:temperature=0:max_tokens=400`.
  Every request sends `thinking: {"type": "disabled"}`.
- **Hindsight:** setup.sh writes `HINDSIGHT_API_LLM_MODEL=deepseek-flash` and
  `HINDSIGHT_API_LLM_EXTRA_BODY={"thinking":{"type":"disabled"}}`. Retain,
  consolidation and reflect inherit both settings.
- **Driver checks:**
  - before any paid step, it confirms the server log line, the startup
    connection check, the `.env` request body, no per-operation override and
    the extractor `model_id`;
  - once Hindsight records any reasoning token, it stops.

## Spend bounds

These are client-side controls. Hindsight has no server-side monetary cap:
work it has already accepted runs until it completes or is cancelled. A stop
therefore bounds what is started; it is not a guaranteed maximum.

- **Prices:** deepseek-flash peak list prices, $0.30 input (cache miss) and
  $1.20 output per million tokens. They are defined once in
  `olo/m3/hindsight/budget.py`. Off-peak is half.
- **Reasoning tokens:** they count as output, both in the extractor reply and
  in Hindsight's `tokens.thoughts`.
- **Extraction:** `Budget(max_calls=8, max_total_tokens=16000)`.
- **Projection:** at most 20 documents and 8 prose documents per run, with
  `UsageGuard` set to $0.15.
- **Driver stop:** the driver checks before every paid step. It stops once
  the combined upper estimate reaches $0.20, or once any reasoning token is
  recorded.

## Checks

Every step's evidence carries `findings` in three categories, and the
evidence ends with a `verdict`:

- `ledger`: the commitment state in olo.db is wrong;
- `hindsight`: documents, observations or recall are wrong;
- `incomplete`: processing did not finish, so the other two categories
  cannot be judged.

The checks live in `scripts/olo/m3/s5_acceptance/checks.py` and are tested
offline in `tests/olo/test_s5_acceptance_checks.py`.

Processing must actually settle. Each of these counts as **incomplete**,
never as a pass:

- not settling within 36 worker runs about 10 s apart;
- a spend stop;
- a submitted operation;
- a busy or stop flag on the last run;
- a projection document that is not confirmed;
- pending prose consolidation, or an unknown pending count;
- a signal held back by a failed extraction;
- no observation for the customer;
- an operation that did not complete;
- a step that raised.

When processing does not settle, the remaining paid steps are skipped, and
each skipped step is recorded as incomplete. A retain that failed once and
then completed when resubmitted is recorded under `notes`.

| Step | Checks |
|---|---|
| `0_preflight` | `llm_trace` true; auth `no key` and `wrong key` → `refused: 401`; `banks_before` empty; server model `deepseek-flash`, request body thinking disabled, no per-operation override; `Connection verified: deepseek/deepseek-flash` logged and no LLM `ERROR` line. Otherwise exit 2 |
| `1_prepare_bank` | `created`; `enable_auto_consolidation` false; `structured-no-llm` strategy. Otherwise exit 3 |
| `2_activate` | budget 8 / 16000; limits 20 / 8; `UsageGuard`; extractor `model_id` deepseek-flash with thinking disabled. Otherwise exit 4 |
| `3_run1_commit_request_meeting` | 3 inserted, 3 identities resolved |
| `3_settle` | settled (see above) |
| `3_state_before_confirmation` | **Ledger:** delivery ORD-501 scheduled 2026-06-10 (v1 business `commit`) with a pending, unconfirmed customer `request_change` to 2026-06-15 (v2); the meeting is a separate commitment on 2026-06-05. **Hindsight:** ledger documents only for confirmed versions, and none states 2026-06-15; every document and observation is Alice's only; at least one observation exists; no observation or delivery recall states 2026-06-15 without request wording; delivery recall states 2026-06-10 |
| `4_run2_business_confirms` / `4_settle` | 1 inserted; settled |
| `4_state` | **Ledger:** delivery v3 business `confirm_change` to 2026-06-15; v1 kept; v2 kept unconfirmed; meeting unchanged. **Hindsight:** ledger documents for v1 and v3, none for v2; customer scope as above |
| `5_recall` | delivery recall states 2026-06-15; meeting recall states 2026-06-05 |
| `6_run3_restart_replay` / `6_state` | 0 inserted, 4 duplicates; ledger versions, operations and documents unchanged |
| `7_probe_*` / `8_probe_*` | retain and consolidation completed, nothing pending. After v2 under the same document id, the text, observations and recall state only 2026-07-09. History shows facts removed |
| `9_cancellation` | status `cancelled` (otherwise incomplete: the cancel lost its race) |
| `10_final` | `total_usd_upper` < 0.20; no Hindsight reasoning tokens |

The proposal-as-fact check is lexical and deliberately strict: a date stated
with no request wording fails. Resolve a suspected false alarm by reading the
recorded text, not by rerunning.

Failures are kept; nothing is retried selectively.
