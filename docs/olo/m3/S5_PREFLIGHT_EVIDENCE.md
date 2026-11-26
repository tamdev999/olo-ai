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

# Final S5 preflight check (consolidation scopes, acceptance readiness)

Recorded 2026-11-26.

**No paid calls.** A disposable server set up by setup.sh used an
OpenAI-compatible provider pointed at a local fake LLM on 127.0.0.1. The fake
logs every prompt, answers deterministically, and reports token usage. The
signal and ledger documents were built by Olo's real projection code.

## 1. Does `observation_scopes=[["olo:prose"]]` override customer scopes?

**No.** Source of the pinned Hindsight 1.2.1, `engine/consolidation/consolidator.py`:

- **The request scope only selects candidates.** `_fetch_unconsolidated_rows` calls `find_unconsolidated` with `tags ⊇ scope` for each scope in the request.
- **Batching uses each memory's own stored scopes.** `_consolidation_batch_key` keys batches on the memory's resolved `observation_scopes`. Memories with different target scopes never share an LLM call.
- **Write passes use the memory's scopes too.** Each scope from `_resolve_obs_tags_list(sub_batch[0])` gets its own pass and becomes that pass's `obs_tags_override`.
- **Existing observations are looked up strictly.** `_find_related_observations` searches with `tags_match="all_strict"` on the pass scope.
- **Re-queued rounds keep the request scope.** A round-limit re-queue passes the same `observation_scopes` on.

**Live run** (one bank, two verified customers, Alice and Bob):

| Check | Result |
|---|---|
| Facts in any consolidation call | always one customer only (5 Alice calls, 4 Bob calls) |
| Observations offered to a call | only that customer's |
| Dedup checks (merge/keep) | one customer each |
| Observations written | tags `{ent:<customer>, ym:<month>}` or `{ent:<customer>}`; never two customers; never `olo:prose` |
| Cross-channel | Alice's call transcript and email consolidated together (same customer scope) |
| Cross-month | April facts were offered the same customer's March observations through the `[ent]` pass |
| Ledger-state memory | never in any prompt; stays unconsolidated |
| Retain-extraction and consolidation tokens | both appear in the bank's LLM trace; `UsageGuard` raised `BudgetExceededError` from consolidation spend |

## 2. Unsafe case found and corrected

**The problem:** unlinked prose (identity unverified, so it has no
`observation_scopes`) carried `olo:prose` in commit 3abbde41. Hindsight
consolidates such a memory in "combined" mode over its own tags,
`{ym:YYYY-MM, olo:prose}`.

**Live proof:** two unrelated groups (Carol/Dan and Erin/Frank) went into one
LLM call and wrote shared observations tagged `{olo:prose, ym:2024-03}`.

**Correction:** only prose with a verified customer scope is tagged. The
corrected copies stayed unconsolidated in the same run.

## 3. Ledger exclusion made independent of server settings

Every automatic consolidation Hindsight queues is bank-wide:
- after a retain;
- after a document update or delete;
- after a memory edit;
- after a bank delete.

Hindsight's default is `enable_auto_consolidation = true`, and ledger-state
documents carry customer observation scopes. `prepare_bank` now also sets the
bank-level `enable_auto_consolidation` to false, a supported per-bank field,
verified live. Until now the exclusion relied only on setup.sh's server-wide
value.

## 4. Production defect found and corrected: mixed-strategy retains

Hindsight 1.2.1 answers `400 "operation_id requires all retain items to
resolve to a single strategy"`. The stage batched prose signals with
structured ledger-state documents under one operation_id, so every such
submission would have failed.

**Fix:** a submission now holds one strategy. The test stand-in enforces the
server's rule, and the regression tests fail on the previous code.

## 5. Acceptance harness dry run (zero spend)

`scripts/olo/m3/s5_acceptance/driver.py` ran unchanged, end to end:
- **Hindsight:** the local fake LLM.
- **Olo extractor:** the test suite's scripted model.
- **No DeepSeek traffic:** no server log line names api.deepseek.com.

| Check | Result |
|---|---|
| Preflight | no key → 401, wrong key → 401, trace on, no banks |
| Bank preparation | `created`; auto-consolidation false; structured strategy in effect |
| Ledger | delivery v1 commit 2026-06-10; v2 customer `request_change`, unconfirmed; v3 business `confirm_change` 2026-06-15; meeting a separate commitment |
| Retains | 4 single-strategy operations, all completed |
| Restart replay | 4 duplicates; versions, operations and documents unchanged |
| Probes | same document_id replaced; cancellation → `cancelled` |

The runbook for the paid run is `docs/olo/m3/S5_ACCEPTANCE_RUNBOOK.md`.
