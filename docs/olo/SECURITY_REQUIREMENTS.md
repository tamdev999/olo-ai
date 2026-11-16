# Olo AI — Future-Milestone Security Requirements

**Status:** Documentation only. None of the requirements below are implemented
in M1. Each requirement includes its source reference.

---

## 1. Approvals bind the complete action

**Source:** `tools/approval.py::_run_approval_gate` (~L1226)

Hermes's approval gate is not Olo's authorization boundary.
`_run_approval_gate` approves under `--yolo`, `approvals.mode: off`, a prior
session/`always` grant, and `approvals.cron_mode: approve`. An Olo approval
will be a durable record bound to a SHA-256 hash computed with
`olo.signals.canonical_json` over the **complete proposed action**:

- Operation type
- Destination (system, endpoint, recipient/account)
- Business identity (tenant/profile and the acting business account)
- All parameters
- Execution constraints (amount or quantity limits, currency, validity
  window/expiry, idempotency key, action schema version)

Any change to any of these fields invalidates the approval. The executor
recomputes the hash immediately before execution, consumes the approval exactly
once, and records approver identity and hash in the audit log.

---

## 2. Hook dispatch has a narrower fail-open path

**Source:** `model_tools.py::_pre_dispatch_guards` (~L793–812);
`hermes_cli/plugins_dispatch.py` (~L60, ~L304–323)

Individual `pre_tool_call` callbacks fail closed on exception or timeout
(`hermes_cli/plugins_dispatch.py` ~L60, ~L304–323). However,
`model_tools.py::_pre_dispatch_guards` (~L793–812) logs and proceeds if the
dispatch machinery itself raises (the outer `except Exception: logger.debug`
path). Hooks are defense in depth only; the Olo executor enforces approval
independently of the hook layer.

---

## 3. Genuine executor credential isolation

**Source:** `tools/env_passthrough.py`; `tools/environments/local_env_policy.py`
(~L196, ~L390)

File permissions (`0600`) are not isolation: the Hermes agent runs as the same
OS user and can read such a file through any code-capable path. Write
credentials must be held by a separate execution principal that the agent
process cannot read or impersonate — at minimum a separate OS user or
container/service with its own secret store, reached only through a narrow
interface that accepts an approval id and verifies the approval record.

Credentials must never enter:
- Hermes `.env`
- `os.environ`
- Skill passthrough (`tools/env_passthrough.py`)
- Any agent-reachable path

Hermes secret scrubbing is name-based (`tools/environments/local_env_policy.py`
~L196, ~L390) and cannot be relied on as a security boundary. Olo agent
sessions get no `terminal`, `execute_code`, `file` or MCP toolsets. Monitor
scripts use separate read-only credentials.

---

## 4. Approval endpoints enforce their own authentication and authorization

**Source:** `website/docs/user-guide/features/extending-the-dashboard.md`
(~L750)

The dashboard's auth gate (plugin routes under `/api/plugins/olo/`) is
necessary but not sufficient. Approval endpoints must independently:

- Authenticate a specific human user
- Authorize that user for the action class, business, and amount
- Require an explicit per-action decision (no bulk or "always" grants)
- Protect against cross-site request forgery and replay
- Record approver identity

The executor re-verifies the approval record independently of the endpoint. The
dashboard must not be exposed on a public interface.

---

## 5. Jev may only authorize preparation

There is no code path from a Jev decision to execution.

---

## 6. Audit triggers are not tamper-proof

**Source:** `olo/store.py` (`audit_no_update`, `audit_no_delete` triggers)

The SQLite `audit_no_update` and `audit_no_delete` triggers on `audit_events`
protect against accidental application-level modification. They are **not
tamper-proof**: anyone with write access to `olo.db` can drop the triggers and
modify the table directly.

Stronger integrity mechanisms (hash chaining over audit rows, forwarding to an
external append-only log, write-ahead log export) are a later-milestone
decision.
