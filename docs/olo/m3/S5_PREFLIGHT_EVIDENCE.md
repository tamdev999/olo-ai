# S5 live preflight corrections: evidence

Recorded 2026-11-26, WSL Ubuntu 22.04, `hindsight-api-slim` 1.2.1 (fork
`0eb02d8c418e`). Disposable businesses only. Every server ran with LLM
provider `none`, so no model request could be made. Both banks reported 0
input and 0 output tokens throughout. The gate server (18800) and all
production data were not touched. Everything disposable was deleted
afterwards.

## Setup under test

`olo/m3/hindsight/setup.sh` was run unmodified, as `olo_hs`, for two businesses:
- `biza` on port 18897;
- `bizb` on port 18898;
- `bizlegacy` (no server), used only to check the key migration.

The venv was the existing pinned install, symlinked in, and the provenance
check passed.

The servers were started the way setup.sh documents: the working directory is
the business home, and Hindsight loads `./.env`. Both server logs show:
`Loaded tenant extension: ApiKeyTenantExtension` (config keys `['api_key']`).

| setup.sh check | Result |
|---|---|
| `.env` contains `HINDSIGHT_API_TENANT_EXTENSION=…:ApiKeyTenantExtension` + `HINDSIGHT_API_TENANT_API_KEY` | yes |
| `.env` file mode | 600 |
| Re-run keeps the business key | yes |
| Two businesses get different keys | yes |
| A key written under the old name `HINDSIGHT_API_TENANT_EXTENSION_API_KEY` is carried over | yes; the old name is no longer written |

## 1. Authentication (`GET /v1/default/banks`)

| Credentials | Server A | Server B |
|---|---|---|
| None | 401 | 401 |
| Wrong key (random) | 401 | 401 |
| The other business's key | 401 | 401 |
| Own key | 200 | 200 |
| `GET /health` (no key) | 200 | 200 |

## 2. Business isolation: access to the other business's existing bank

Each bank first held one private document, which the control row shows is
readable with the owner's key.

| Request | A's key → B's server, bank `olo-bizb` | B's key → A's server, bank `olo-biza` | No key → B |
|---|---|---|---|
| GET documents | 401 | 401 | 401 |
| GET document | 401 | 401 | 401 |
| POST recall | 401 | 401 | 401 |
| POST retain (write) | 401 | 401 | 401 |
| PATCH bank config | 401 | 401 | 401 |
| DELETE document | 401 | 401 | 401 |
| POST consolidate | 401 | 401 | 401 |
| Control: owner's key reads its own document | 200 | 200 | |

- **Wrong bank on own server:** A's key on A's server asking for `olo-bizb` gets 404. B's data lives in B's own pg0 instance.
- **Nothing changed:** after all attempts, each bank lists exactly its own document. B's strategies are unchanged.
- **Audit log:** the server's audit log records each rejected delete as `status=error`.
- **Recall:** recalled text never contains the other business's content.
- **A probe flaw, corrected:** a first recall probe matched the query string echoed in the response. It was re-checked on the recalled text.

## 3. Bank preparation (`olo.projection.prepare_bank`)

| Case | Result |
|---|---|
| A: bank missing | `created`; resolved `retain_strategies` = `structured-no-llm: {chunks, observations off}` |
| A: repeated | `unchanged` (read only) |
| B: bank holding an unrelated strategy `olo-probe-other` | `updated`; strategies = `{olo-probe-other, structured-no-llm}`; other overrides unchanged |
| B: repeated | `unchanged` |

## 4. Consolidation

**Findings that shaped the design (Hindsight 1.2.1):**

1. **Structured memories never drain from the pending count.** A memory retained with `structured-no-llm` (observations off) is still counted in the bank's `pending_consolidation`. The retain path never reads a strategy's `enable_observations`, so structured memories stay consolidation candidates. The consequences:
   - a bank-wide consolidation would send ledger-state and structured signal memories to the LLM;
   - the bank-wide pending count never drains.
2. **A skipped consolidation is reported as completed.** With no LLM configured, the server logs "Skipping consolidation" yet reports the operation `completed`, with empty `result_metadata`.
3. **The server's own pending count can be scoped.** `GET …/memories/list?consolidation_state=pending&tags=…` returns the server's count of pending source memories carrying a tag.

**Implementation:**
- **Scoping:** prose documents carry the tag `olo:prose`, and consolidation is triggered with `observation_scopes=[["olo:prose"]]`.
- **Required when:** the server reports prose memories pending.
- **Tracking:** the operation is recorded as `kind='consolidation'` in `hindsight_operations`.

**Live run** (bank A: one structured memory and one `olo:prose`-tagged chunk memory):

| Observation | Value |
|---|---|
| Bank-wide `pending_consolidation` | 2 |
| Prose pending (scoped count) | 1 |
| Run 1 | scoped consolidation triggered, recorded `submitted` |
| Runs 2–4 (back to back) | `consolidation_in_flight`; no second trigger |
| Run 5 | operation `completed`, prose still pending (skipped server-side), so retried |
| Further runs | third completed-but-ineffective attempt, then `consolidation_retries_exhausted` |
| LLM tokens (input / output) | 0 / 0 |

**Cancellation:** a retain that could not finish (a verbatim strategy needs an
LLM, so the server kept retrying it) was cancelled through
`cancel_operation`, going from `pending` to `cancelled`. Consolidation
cancellation (stale, or over budget) is covered by offline tests.

## Remaining risks

- **Structured memories in prose scopes:** a structured document deliberately tagged `olo:prose` would be consolidated. Only the projection stage adds the tag, and only to prose items.
- **Unauthenticated health:** `/health` answers without a key. It reveals liveness only.
- **No proof that observations formed:** the stage can tell that work remains (prose still pending), not why. When the attempt bound is reached it stops and reports; an operator must investigate.
- **No paid run yet:** prose fact extraction and consolidation by DeepSeek were not exercised. That is the paid S5 acceptance run, which needs separate approval.
- **Isolation depends on the topology:** isolation is per business server and key. One Hindsight server shared by several businesses would need a per-tenant extension, which 1.2.1's built-in one is not.
