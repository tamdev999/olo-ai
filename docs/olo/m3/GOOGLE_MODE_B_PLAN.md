# Google Mode B — paid extraction acceptance (prepared, not executed)

Mode B proves the production path that Mode A cannot:

**Gmail → `make_deepseek_model_fn` (DeepSeek) → ledger → Hindsight projection → Hermes `olo_customer_context`.**

Everything below is verified offline only. Running it needs approval P5, for paid inference. It does not need new OAuth, grant changes or accounts.

## 1. Which Mode B scenarios the dedicated account can test

| Scenario | Testable with `livetest` + seeder? | Why |
|---|---|---|
| **B1** (new) — request → confirmation → confirmed change → cancellation | **yes** (`api`) | The business's messages are its own writes. The customer's two messages are seeded stand-ins, but what's tested (extraction and ledger semantics) doesn't depend on how a mail client composed them. |
| L2 — same-day plain-email confirmation without UID | yes (`api`) | The business's own event and sent mail. The expected result is `ledger_unresolved`, an operator item. |
| G1 with extraction | only `passed_simulated` | The customer reply is seeded, and G1's check doesn't test extraction. |
| G2 | **no** | Needs a genuine Outlook second identity. |
| E3 "with a real model turn" | **not in this plan** | A Hermes agent turn uses the agent's own model provider, a separate paid path with its own cost. B1 instead records the production tool handler's output, which needs no model. |

## 2. B1: the sequence and what must hold

One thread about `ORD-<run>`. The dates come from the run: D1 = base + 18 days, D2 = base + 20 days. Each phase gets **one** Mode B sync, then `run.py snapshot B1`.

| Phase | Message | Expected ledger version | Point-in-time state (snapshot) |
|---|---|---|---|
| 1 | Business: "Your order … is confirmed for delivery on D1." | `commit` (business, confirmed, D1) | scheduled D1 |
| 2 | Customer: "Could we move … from D1 to D2?" | `request_change` (customer, **unconfirmed**, proposed D2) | **still scheduled D1**, pending D2 |
| 3 | Business: "Confirmed: … moved from D1 to D2." | `confirm_change` (business, confirmed, D2, prior D1) | scheduled D2. D1 is superseded. |
| 4 | Customer: "Please cancel … on D2." | `cancel` (customer, **unconfirmed**) | **still scheduled D2**, pending cancel |
| 5 | Business: "We have cancelled the … delivery." | `cancel` (business, confirmed) | cancelled |

The B1 check (`scenarios.b1`) passes only if all of the following hold:
- **History:** the ledger history is exactly that sequence, and every snapshot matches its row.
- **Memory:** every confirmed version's ledger document is `confirmed` in Hindsight.
- **Tool output:** the production Hermes tool handler returns a verified identity and the commitment as `cancelled`. Superseded versions appear only under `no_longer_true`.
- **Usage:** model use stayed within the run caps.

**Evidence** goes to `results_<run>.json`, `facts.json` and `verdicts.jsonl`:
- message and source-document fingerprints;
- every version (action, actor, confirmed, dates);
- the memory-document statuses;
- the snapshots;
- the model usage;
- the tool output, with the business address replaced by a placeholder.

No credentials or message bodies are recorded.

## 3. The production extractor, not a stand-in

`run.py sync --mode B` builds its stage with `olo.activation.activate`, the same function the installed worker uses. That gives:
- `make_deepseek_model_fn`;
- model `deepseek-flash`, at `https://api.deepseek.com/v1`;
- thinking disabled, temperature 0, at most 400 output tokens;
- `EXTRACTION_VERSION` 3, using the current prompt.

The command line has no way to redirect the base URL; only offline tests can. `run.py --mode B --key-file F preflight-b` builds that model function offline and asserts its `model_id`, without making a request.

## 4. Hard ceilings

| Bound | Value | Enforced by |
|---|---|---|
| Model requests, whole run | **8** (`--max-model-calls`, at most 20). Expected: 5, or 6 with L2. | `mode_b.allowance`: each sync's Budget gets only what's left, and the sync is refused at the cap. |
| Model spend, whole run | **$0.05** (`--max-usd`, at most $0.50), counted at the output rate as an upper bound | the same |
| HTTP requests per model call | **1** | `DEEPSEEK_MAX_RETRIES = 0`; the SDK's 2 automatic retries are off |
| A failed call | counted, and charged its estimate | `Budget.charge_failed` |
| Request timeout | 60 s | `DEEPSEEK_TIMEOUT_S` |
| Per call | at most 3,000 tokens estimated before sending; at most 400 output | `Budget.call_refusal`, `max_tokens` |
| Retries of one email | at most 5 attempts, each counted against the cap | `max_attempts` |
| Hindsight inference | **none** | Prose isn't retained (`projection_max_prose_docs = 0`, in both modes); the business must be `HINDSIGHT_API_LLM_PROVIDER=none` with no LLM key |
| Prose backlog | **0** | A Mode B sync refuses while prose from another run awaits extraction |
| Google reads per sync | 400 | the recorder |

**Maximum estimated cost:**
- **Expected:** 5–6 calls of about 900 tokens each (estimate, including the 400 output cap) come to about $0.0003–0.0006 per call, so **under $0.004 in total**.
- **Hard worst case under the caps:** 8 calls × $0.00126 (the 3,000-token per-call limit, priced at deepseek-flash list prices of $0.30/M input and $1.20/M output) = **$0.0101**. The $0.05 cap is a second stop.
- **Hindsight:** $0.
- **Live spend check:** these figures use list prices. The run records Olo's own usage ledger; DeepSeek's dashboard is the independent check.

## 5. Execution plan (after approval; each step is local unless marked)

1. **A fresh disposable home** `/home/olo_hs/acceptB/home`. The Mode A home holds 37 identity-resolved prose signals awaiting extraction, which would spend the cap. The offline `preflight-b` run against it on 2026-11-27 refused it on exactly that ground. Its other checks passed: production model, 0 retries, an owner-only key and provider `none`.
   - `setup.sh accept --provider=none`, with the pg0 installation copied, as before.
   - Olo's existing credential file is copied from the Mode A home. That is a local copy: no consent and no grant change.
2. **Key file:** `/home/olo_hs/deepseek.env` (owner olo_hs, mode 600, defines `HINDSIGHT_API_LLM_API_KEY`). The operator confirms it is the key to use and that its account has a provider-side spend limit.
3. **Offline preflight:** `run.py --mode B --key-file … preflight-b`. It must pass: no Google request and no model call.
4. **Bootstrap (Google, reads only):** `run.py preflight` (one profile read), Hindsight start, then one Mode A sync to set the Gmail cursor.
5. **New run ID**, then `export OLO_ACCEPTANCE_RUN`.
6. **For each of B1's phases 1–5** (Google seeder write; DeepSeek, **paid**):
   - seed the phase: 1 write;
   - run `run.py --mode B --key-file … sync`: 1 model call expected;
   - run `run.py snapshot B1`.
7. **Optional, if approved: L2.** Seed it (2 writes), then one Mode B sync, for 1 model call. The expected result is an operator item.
8. **Check:** `run.py --mode B check B1 [L2]`, which writes `results_<run>.json`.
9. **Cleanup** (Google, seeder): a dry run, then this run's marked items only (5–6 messages, 0–1 events).
10. **Teardown:**
    - `hermes olo hindsight stop`, then verify that no `olo_hs` postgres is left;
    - no cron jobs are installed for Mode B;
    - grants are kept, and nothing is revoked.

**Totals:** about 7–8 Olo syncs, roughly 10 Google reads each; 5–7 seeder writes, plus the same number for cleanup; 5–6 DeepSeek requests.

## 6. Approval boundary

Approved now: nothing that spends money or contacts Google.

The separate approvals needed:
- **P5:** paid DeepSeek inference within the caps in section 4. The operator names the key file and confirms the provider-side limit.
- **Live writes:** seeding B1 (and L2, if wanted) and the run-tagged cleanup.
- **Reads:** the Google reads of the bootstrap and the Mode B syncs.

Not part of this plan: Mode B for G1 or G2; E3 with a real agent turn; E5 and E6; prose projection with Hindsight inference; new accounts or OAuth.
