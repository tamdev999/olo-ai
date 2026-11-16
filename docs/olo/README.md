# Olo AI — M1 Developer Guide

Olo AI is a business-focused extension of Hermes Agent. Milestone 1 (M1)
delivers a zero-LLM business signal pipeline: signals are ingested from
configurable adapters on a cron schedule without calling any language model.

---

## Quick start (25 signals in a fresh temp home)

```bash
# From the olo-ai repo root
source ./activate   # or .\activate.ps1 on Windows

export HERMES_HOME="$(mktemp -d)/hermes-home"

# Install the Olo plugin
mkdir -p "$HERMES_HOME/plugins"
ln -s "$PWD/olo/hermes_plugin" "$HERMES_HOME/plugins/olo"
hermes plugins enable olo

# Initialise the DB and install the cron shim
hermes olo setup --source synthetic

# Create the cron job
hermes cron create "15m" \
  --name olo-synthetic \
  --script olo_monitor_synthetic.py \
  --no-agent --deliver local

# Fire once
hermes cron run <job-id>

# Verify
hermes olo status
```

After `cron run` completes you will see 25 signals, checkpoint `"25"`, and
one `succeeded` run in `hermes olo status`.

---

## Running the tests

```bash
# Unit and integration tests only (no subprocess E2E):
export HERMES_HOME="$(mktemp -d)/hermes-home"
export HERMES_RUNTIME_DIR="$(mktemp -d)/hermes-runtime"
export HERMES_PYTHON="$PWD/.venv/Scripts/python.exe"   # Windows
# export HERMES_PYTHON="$PWD/.venv/bin/python"          # POSIX
scripts/run_tests.sh tests/olo/
```

All tests must pass with zero failures and zero flaky retries.

---

## Core-diff guard

`scripts/olo/check_core_diff.sh` verifies that no Hermes core file has been
modified since the pinned base commit `f65af53f`. Run it before every push:

```bash
bash scripts/olo/check_core_diff.sh
```

Exit codes: `0` OK, `1` core change found, `2` base not in this clone.

---

## CI

`.github/workflows/olo-guard.yml` runs the guard and `tests/olo/` on every PR
and push to `main` or `olo/**` branches. **This workflow will not trigger while
GitHub Actions is disabled on the repository.** That is expected; the same
checks run locally and in `tests/olo/`. Enabling Actions is BBC's decision.

The `actions/checkout` step is pinned to full commit SHA
`11bd71901bbe5b1630ceea73d27597364c9af683` (v4.2.2) per Hermes F17 policy.

---

## Limitations and known constraints

### Audit triggers are not tamper-proof

The `audit_no_update` and `audit_no_delete` SQLite triggers protect the
`audit_events` table against accidental modification from application code.
They are **application-level protection, not tamper-proof auditing**: anyone
with write access to `olo.db` can drop the triggers and modify the table.
Stronger integrity (hash chaining, external append-only log) is a future-
milestone decision. See `docs/olo/SECURITY_REQUIREMENTS.md` §6.

### Lease TTL

The default lease TTL is 900 seconds. This must exceed the slowest single
`fetch()` call an adapter may make. If you implement a slow adapter (e.g. one
that calls a rate-limited external API), set a longer `lease_ttl` explicitly
in `run_source()`. Document the expected worst-case fetch latency in the
adapter's docstring so operators can size the TTL correctly.

### GitHub Actions disabled

Actions is currently disabled on this repository. The `olo-guard.yml` workflow
exists but will not run on GitHub until Actions is re-enabled by BBC. All the
same checks run locally and in `tests/olo/`.

---

## Recovery

If a cron run was interrupted mid-flight (process killed, machine restarted):

1. The run row remains `running` with its lease intact.
2. Immediately re-firing returns `skipped_busy` until the lease expires (default 900 s).
3. After expiry, the next fire abandons the interrupted run and resumes from
   the last committed checkpoint.
4. No signals are duplicated (at-most-once commit, unique key on `(source, external_id)`).
