# Post-audit remediation, batch 1

This batch was made against the M1–M3 hostile audit
(`M1_M3_HOSTILE_AUDIT_8a0feac0.md`). It connects the installed pipeline,
makes degradation visible, and repairs the authoritative retrieval
interface.

## 1. The installed pipeline

`hermes olo setup` can now enable the S5 stages for one named test business.
The settings are validated by the runner's own activation code
(`olo.activation.activate`) and written into the generated cron shim:

```bash
hermes olo setup --source gmail --option account=acme \
    --enable-commitments --enable-projection --business-id acme --bank-id olo-acme \
    --deepseek-key-file /path/to/deepseek.env --extract-max-calls 50 \
    --projection-max-docs 200 --projection-max-prose-docs 50 --projection-budget-usd 1.0 \
    --own-email owner@acme.example
hermes cron create "every 5m" --name olo-gmail-acme --script olo_monitor_gmail_acme.py \
    --no-agent --deliver local
```

- **Off by default.** Without `--enable-*`, setup writes the same shim as before; S5 options given without an enable flag are refused.
- **Key handling.** The key file must be owner-only. It is referenced by path, never copied, and the tenant key stays in the business `.env`.
- **Extraction destination.** `--deepseek-base-url` accepts only `https://api.deepseek.com[/v1]` or a loopback URL; business text is not sent anywhere else.
- **Validation happens at setup.** An unsafe or incomplete configuration fails at setup time, not silently at 3 a.m.
- **Failed fetches.** A Google outage, rate limit or revoked token no longer blocks signals already ingested: the commitment and projection stages run before the run is recorded as failed.
- **Overlap and restart** are unchanged: the source lease returns `skipped_busy`, and recovery resumes pending signals.

## 2. Health, status and exit codes

`olo.health` stores each stage's outcome per source in `pipeline_health`
(ingest, identity, commitment, projection, operator). It records the last
attempt, the last success, the status, the counts and the error, the number
of consecutive failures, and any operator action needed. The state survives
restarts.

`hermes olo status` now shows:

- **Pipeline health:** each stage, with its last success and consecutive failures.
- **Backlog**, computed from the tables that own it:
  - identity and commitment states, and extractions still retrying;
  - the projection outbox, operations in flight, and confirmed ledger versions not yet projected.
- **Attention**, the conditions an operator must act on:
  - degraded stages;
  - stalled sources (no successful ingestion for 2 hours);
  - OAuth failures;
  - items no retry will settle: ambiguous identities, permanently failed extractions, unresolved ledger matches, and documents out of retries.

**The worker never reports success when the pipeline is degraded.**

| Condition | Exit code |
|---|---|
| Fetch failed | 1 |
| A stage failed or degraded: budget exhausted, model or content errors, bank unprepared (with its reason), stats unavailable, usage budget exceeded, unregistered strategy, failed / lost / cancelled Hindsight work | 1, with the reason on stderr |
| The run added new items needing an operator | 1 |
| Items still waiting from earlier runs | 0, with a warning |

Installations without the S5 stages keep their existing exit behaviour.

## 3. Usage accounting

The projection's usage budget is no longer "cumulative total minus a stored
baseline":

1. Hindsight 1.2.1 sweeps trace rows after one day, so that subtraction lost spend.
2. Olo now records each hour bucket of the bank's trace (`stats?period=1d`) in `hindsight_usage_hours`, keeping the largest value it has seen for that hour.
3. Spend for a window is the sum of the hours overlapping it. The partial first hour is included, so the total is an upper bound.
4. A restart keeps the window, and a bucket the server shrinks or drops stays counted.

The tests reproduce the retention gap.

## 4. Authoritative retrieval — the interface for Hermes tools

`olo.current_state.customer_context(conn, client, bank, contact, query)` is
the one supported way to answer a question about a customer.

- `current` comes from the commitment ledger in olo.db, and only from there.
- `memories` are recalled Hindsight memories, restricted to the customer's entity tag and fetched with their source facts. Each one is a `LabelledMemory` with `authoritative=False`, labelled against the one commitment its provenance links it to:
  1. a ledger document id;
  2. a `sig:` document whose signal the ledger recorded (`signal_resolution`);
  3. an observation whose source facts all belong to that commitment;
  4. as a fallback, the text names exactly one of the customer's commitment keys.
- **No exactly-one link, no assignment.** A memory with no link, or links to several commitments, is `uncertain`; it is never silently assigned. So is a memory whose text names another commitment's key, or states a date the ledger never held.
- **Dates are compared with the linked commitment only.** A valid date of order A is never "historical" because order B once used it, and order B's pending request never relabels order A.
- **Raw recall is refused as truth.** `current_truth(context)` accepts only a `CustomerContext`. A raw Hindsight recall response, its results, or a look-alike dict raises `TypeError`, so raw memory cannot be treated as current operational truth.

## 5. Test environment correction

The WSL test venv's `hermes` editable install points at `/root/olo-linux`, a
stale pre-S5 copy. The cron E2E helpers now put the repository first on
`PYTHONPATH` for every `hermes` subprocess, so the CLI, the plugin and each
cron-run shim test the code under test. The existing Google and synthetic
cron E2E tests pass against the real code.

## Still required before unattended test-business operation

- A supervised Hindsight service. Nothing restarts the server, and the projection only reports `bank_unprepared` when it is down.
- Production OAuth publishing. Testing-mode refresh tokens expire after 7 days, and `status` reports the failure, but nobody is notified.
- An operator workflow: resolution of ambiguous identities and unresolved ledger matches is still DenyAll.
- An actual Hermes consumer of `customer_context`, and keeping business banks out of Hermes's own Hindsight memory provider.
- The audit's remaining P1 items: spam-rescued mail, delete and label propagation, calendar to the ledger, authored-text and HTML hardening, and same-date keyless commitments.
- A live run with real DeepSeek and Hindsight of the installed path, which needs separate approval.
