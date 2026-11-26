# S5 live acceptance runbook

**Paid: run only with explicit approval, once.** Disposable business and bank
only; no real mailbox, production bank, benchmark or holdout.

## Commands (PowerShell, Windows host)

```powershell
wsl -d Ubuntu-22.04 -u root -- bash "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/m3/s5_acceptance/start.sh"
```

```powershell
wsl -d Ubuntu-22.04 -u root -- env -u DEEPSEEK_API_KEY /root/olo_linux_venv/bin/python "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/m3/s5_acceptance/driver.py" /root/s5_acceptance_evidence.json
```

```powershell
wsl -d Ubuntu-22.04 -u root -- bash "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/m3/s5_acceptance/down.sh" /root/s5_acceptance_server.redacted.log
```

### What each command does

- **`start.sh`:** runs the real setup.sh (provider deepseek, tenant
  authentication on) in a disposable home. It then starts Hindsight on port
  18899 with the key from `/root/olo_m3_secrets/deepseek.env`, kept in the
  server environment only, and the model pinned to `deepseek-chat`.
  Hindsight's own default for the deepseek provider is `deepseek-v4-flash`.
- **`driver.py`:** runs the scenario once and writes the evidence JSON.
- **`down.sh`:** stops the server and deletes every disposable file.

## Spend bounds

These are client-side controls. Hindsight has no server-side monetary cap:
work it has already accepted runs until it completes or is cancelled.

- **Extraction:** `Budget(max_calls=8, max_total_tokens=16000)`.
- **Projection:** at most 20 documents and 8 prose documents per run, with `UsageGuard` set to $0.15.
- **Usage guard coverage:** the guard reads the bank's LLM trace, which records retain extraction and consolidation alike (verified with a local fake LLM).
- **Driver stop:** the driver checks before every paid step and stops once the combined upper estimate reaches $0.20.
- **Pricing basis:** the estimate uses DeepSeek V3 list prices, $0.14 input and $0.28 output per million tokens.

## Expected checks (evidence step → expectation)

| Step | Expect |
|---|---|
| `0_preflight` | `llm_trace` true; version 1.2.1; auth `no key` and `wrong key` → `refused: 401`; `banks_before` empty. Otherwise the driver stops (exit 2) |
| `1_prepare_bank` | `created`; `enable_auto_consolidation` false; `structured-no-llm` = chunks, observations off. Otherwise it stops (exit 3) |
| `2_activate` | budget 8 / 16000; limits 20 / 8; `UsageGuard`; `model_id` deepseek-chat |
| `3_run1_commit_request_meeting` | 3 inserted, 3 identities resolved. Ledger: delivery ORD-501 scheduled 2026-06-10 with a pending, unconfirmed customer `request_change` to 2026-06-15; the meeting is a separate commitment |
| `3_settle` | retains complete (one strategy per operation); then a prose-scoped consolidation completes and prose pending reaches 0 |
| `4_run2_business_confirms` / `4_state` | delivery v3 `confirm_change` by the business, 2026-06-15, with v1 kept. Ledger documents for v1 and v3; none for the pending v2 |
| `4_state.observations` | every observation tagged with Alice's `ent:` (and month) only. Ledger memories unconsolidated |
| `5_recall` | delivery answers 2026-06-15, not the customer's proposal as confirmed; meeting 2026-06-05 |
| `6_run3_restart_replay` | 4 duplicates; versions, operations and documents unchanged |
| `7_probe_*` / `8_probe_*` | same document_id: v2 text replaces v1; history shows facts removed. Record whether observations still state the v1 date (a divergence to report, not a reason to retry) |
| `9_cancellation` | status `cancelled` |
| `10_final` / `spend_checks` | `total_usd_upper` < 0.20 |

A failed step is recorded and the run continues where safe. Failures are kept;
nothing is retried selectively.
