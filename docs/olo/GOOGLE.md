# Olo AI — Google Workspace Integration Guide

## Overview

Olo AI monitors your Gmail and Google Calendar using read-only OAuth scopes.
No message bodies, attachments, or event descriptions are stored. All data
stays in a local SQLite database (`olo.db`).

---

## Google Cloud Setup Checklist (§3.3)

Before running `hermes olo google connect`, complete these steps in the Google Cloud Console
(https://console.cloud.google.com/):

### 1. Create a project and enable APIs
- Create a dedicated Google Cloud project.
- Enable **Gmail API** and **Google Calendar API**.

### 2. Configure OAuth consent screen → Branding
- In **APIs & Services → OAuth consent screen → Branding**, fill in an app name, user-support
  email and developer contact. Leave the logo blank.

### 3. Configure OAuth consent screen → Audience
- Set **Audience** to **External**.
- Leave **Publishing status** as **Testing** (see §External/Testing consequences below).
- Add the dedicated test account as a **Test user**.

### 4. Configure OAuth consent screen → Data Access
- Click **Add or remove scopes**.
- Add both scopes:
  - `https://www.googleapis.com/auth/gmail.metadata`
  - `https://www.googleapis.com/auth/calendar.events.owned.readonly`
- **Tick both permission checkboxes** when prompted at consent.

### 5. Create an OAuth client → Clients
- In **APIs & Services → Credentials → Create credentials → OAuth client ID**:
  - Application type: **Desktop app**
  - Name: "Olo AI Monitor" (or similar)
  - **Leave "Use this client for an AI-powered agent" unticked.**
- Download the client secret JSON file. **This is the only time it can be downloaded;
  if lost, delete and recreate the client.**
- Keep the JSON file outside the repository (mode 0600 on Linux/macOS).

---

## External/Testing Consequences

**7-day token expiry.** When the consent screen is in Testing status, Google issues
refresh tokens that expire 7 days after the user consented. Run
`hermes olo google connect --account <alias>` again each week.

**Unverified app warning.** Users will see a "Google hasn't verified this app" warning.
This is expected for private/testing use.

**100-user limit.** Only accounts listed as test users can authorise.

Production verification is out of scope for M2.

---

## Scopes

| Scope | What it allows | What Olo stores |
|---|---|---|
| `gmail.metadata` | Message metadata only (headers + labels; no body) | Sender/recipients, subject, labels, timestamps |
| `calendar.events.owned.readonly` | Events on calendars you own | Event title, times, attendees, status |

Olo never requests and never stores:
- Message bodies, snippets or attachments
- Event descriptions
- Any write scope

---

## Connect an Account

```bash
# Interactive (loopback redirect — opens browser automatically):
hermes olo google connect --account acme --client-secret-file /path/to/client_secret.json

# Headless / no browser on this machine (--manual mode):
hermes olo google connect --account acme --client-secret-file /path/to/client_secret.json --manual
# Paste the full redirect URL from your browser when prompted.
```

**WSL2 and loopback.** When running in WSL2, the loopback listener is on the WSL2 VM's
`127.0.0.1`, which Windows browsers cannot reach by default. Either:
- use `--manual` (copy/paste the redirect URL), or
- enable WSL2 mirrored networking (`networkingMode=mirrored` in `.wslconfig`).

`<account>` is an alias matching `^[a-z0-9]{1,16}$`. It becomes part of the source
name: `gmail_acme`, `gcal_acme`.

**Accept both scopes** on the consent screen. If only one is ticked, disconnect and reconnect.

---

## Set Up Monitors

After connecting:

```bash
# Gmail (every 5 minutes):
hermes olo setup --source gmail --option account=acme
hermes cron create "every 5m" --name olo-gmail-acme \
    --script olo_monitor_gmail_acme.py --no-agent --deliver local

# Calendar (every 15 minutes):
hermes olo setup --source gcal --option account=acme --option lookback_days=7
hermes cron create "every 15m" --name olo-gcal-acme \
    --script olo_monitor_gcal_acme.py --no-agent --deliver local
```

**`hermes cron run <id>` is synchronous.** It executes the job immediately, prints
`Ran now: succeeded.` or `Ran now: failed.`, and returns exit code 0 (success) or 1 (failure).
There is no need to call `cron tick` after `cron run`.

---

## Status

```bash
# Show all accounts, scopes, and checkpoint phases:
hermes olo status

# Show connected Google accounts with expiry info:
hermes olo google status

# Live token refresh check (confirms scopes and testing/production status):
hermes olo google status --check
```

---

## Disconnect

```bash
# Revokes the OAuth token (best effort) and removes the credential file:
hermes olo google disconnect --account acme
```

---

## Recovery Runbook

### History ID expired (Gmail)

**Symptom:** Gmail run fails with `gmail history expired`.
**Cause:** Gmail history records are only available for approximately one week.

**Fix:**
```bash
hermes olo checkpoint reset --source gmail_acme --confirm
# Next run will re-bootstrap from the current historyId (no backfill).
```

### Sync token expired (Calendar)

**Symptom:** Calendar run fails with `calendar sync token expired`.
**Cause:** Google invalidated the incremental sync token.

**Fix:**
```bash
hermes olo checkpoint reset --source gcal_acme --confirm
# Next run will perform a fresh full sync from lookback_days.
# Events cancelled during the gap will not appear.
```

### Auth expired / revoked (both)

**Symptom:** Run fails with `google auth revoked or expired`.
**Cause:** Refresh token expired (Testing: 7 days), or user revoked access.

**Fix:**
```bash
hermes olo google connect --account acme --client-secret-file <path>
# Sign in again and accept the consent screen.
```

---

## Phase B Live Verification How-To

Phase B verifies the adapters against real Google data.

### Prerequisites

1. Complete the Cloud Setup steps above.
2. Build the Hermes dev venv (Linux/WSL2):
   ```bash
   python -m pm.build_env --source . --out <venv> --group dev --group test
   export HERMES_PYTHON=<venv>/bin/python
   ```
3. Create an isolated `HERMES_HOME` outside the repo:
   ```bash
   export LIVE_HOME=/tmp/olo_live_home
   mkdir -p "$LIVE_HOME"
   ```
4. Connect the test account:
   ```bash
   HERMES_HOME="$LIVE_HOME" hermes olo google connect \
       --account livetest \
       --client-secret-file /path/to/client_secret.json
   ```
5. Verify:
   ```bash
   HERMES_HOME="$LIVE_HOME" hermes olo google status --check
   # Must show: gmail.metadata ✓, calendar.events.owned.readonly ✓, testing
   ```

### Running the stages

```bash
# Stage 0: bootstrap (no data needed)
OLO_GOOGLE_TEST_HOME="$LIVE_HOME" OLO_LIVE_STAGE=0 \
    "$HERMES_PYTHON" -m pytest -m integration tests/olo/test_google_live.py -v -s

# Stage 1: after BBC creates P1-P8 and E1-E4
OLO_GOOGLE_TEST_HOME="$LIVE_HOME" OLO_LIVE_STAGE=1 \
    "$HERMES_PYTHON" -m pytest -m integration tests/olo/test_google_live.py -v -s

# Stage 2: after BBC mutates data (archive P1, open P2, star P3, trash+delete P4, rename E1, delete E3)
OLO_GOOGLE_TEST_HOME="$LIVE_HOME" OLO_LIVE_STAGE=2 \
    "$HERMES_PYTHON" -m pytest -m integration tests/olo/test_google_live.py -v -s

# Stage 3: after BBC sends Q1,draft-A,Q2,draft-B,Q3
OLO_GOOGLE_TEST_HOME="$LIVE_HOME" OLO_LIVE_STAGE=3 \
    "$HERMES_PYTHON" -m pytest -m integration tests/olo/test_google_live.py -v -s

# Failure/recovery tests
OLO_GOOGLE_TEST_HOME="$LIVE_HOME" OLO_LIVE_STAGE=failure \
    "$HERMES_PYTHON" -m pytest -m integration tests/olo/test_google_live.py -v -s
```

Cross-stage state is stored in `$LIVE_HOME/phase_b_state.json`.
Redacted observations (email hashes only) are written to `$LIVE_HOME/phase_b_observations.json`.

---

## Privacy Notes

- Email **subjects** and calendar event **titles** are stored in `signals.subject` (256-char limit).
- Sender/recipient addresses and attendee emails are stored in `signals.payload` only.
- No email bodies, snippets, event descriptions or attachments are stored.
- All data remains in a local SQLite file (`$HERMES_HOME/olo/olo.db`, mode 0600).
- Audit events use SHA-256 hashes of email addresses — no raw addresses in the audit log.
- The refresh token is stored in `$HERMES_HOME/credentials/google/<alias>.json` (mode 0600).
  It is scoped to read-only APIs but lives in a file the same OS user can read.
  See `docs/olo/SECURITY_REQUIREMENTS.md §3` for the full isolation requirements.

---

## Credential Storage

Credentials live at:
```
$HERMES_HOME/credentials/google/<alias>.json   (mode 0600)
$HERMES_HOME/credentials/google/<alias>.lock   (advisory lock file)
```

Stored fields: `client_id`, `client_secret`, `refresh_token`,
`granted_scopes`, `email_address`, `refresh_token_issued_at`,
`oauth_publishing_status`, `api_base_override` (tests only).

**Access tokens are never written to disk.**

---

## Limitations

- **No backfill for legacy accounts.** Accounts connected with M2 (gmail.metadata only)
  do not receive email.content signals. Upgrade with `--upgrade` (see §M2b Upgrade below).
- **Gmail backfill on upgrade.** When upgrading a connected account to full_v1 scopes,
  Olo performs a one-time backfill using `messages.list`. Backfill is best-effort; messages
  deleted before backfill completes are skipped silently.
- **Weekly reconnect** required when `oauth_publishing_status = "testing"`.
- **Checkpoint reset gap.** After `checkpoint reset`, events/messages from
  the gap period (since the last committed cursor) are not visible in Olo.
  New events/messages from that point forward will be ingested on the next run.
- **GitHub Actions disabled.** The `olo-guard.yml` CI workflow is inactive until
  BBC re-enables Actions. The same checks run locally via `scripts/olo/check_core_diff.sh`.

---

## Attribution

Built on **Hermes Agent by Nous Research**. See `LICENSE` for terms.

---

## M2b: Full Content and All Calendars

Milestone 2b adds:
- **Full Gmail content** (`email.content` signals with lossless message blobs)
- **All readable calendars** (not just primary — all calendars via `calendarList.list`)
- **Gmail backfill** after scope upgrade (one-time `messages.list` scan)
- **Calendar discovery signals** (`calendar.calendar.added` / `calendar.calendar.removed`)

### New Scopes (M2b)

| Scope | Replaces | What it allows |
|---|---|---|
| `gmail.readonly` | `gmail.metadata` | Full message content (headers + body + attachments metadata) |
| `calendar.events.readonly` | `calendar.events.owned.readonly` | Events on all calendars (not just owned) |
| `calendar.calendarlist.readonly` | _(new)_ | Discover all readable calendars |

**`gmail.metadata` must not coexist with `gmail.readonly`** — Google returns 403 on
`format=full` requests when both scopes are granted. The `scope_profile` field in the
credential file distinguishes the two generations:

| `scope_profile` | Scopes | Behavior |
|---|---|---|
| `legacy_v1` (or `null`) | M2 scopes | Metadata-only Gmail; primary calendar only |
| `full_v1` | M2b scopes | Full content Gmail; all readable calendars |

### Google Cloud Setup for M2b

The consent screen must be updated to include the three new scopes before running
`--upgrade`:

1. In **APIs & Services → OAuth consent screen → Data Access**, remove the old scopes
   and add:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/calendar.events.readonly`
   - `https://www.googleapis.com/auth/calendar.calendarlist.readonly`
2. Download a **new** client secret JSON (or reuse the same client, but generate a fresh
   credential file so the granted scopes are updated).
3. Run `--upgrade` (see below).

### Upgrading a Connected Account

```bash
# Reconnect with M2b scopes (opens browser):
hermes olo google connect \
    --account acme \
    --client-secret-file /path/to/client_secret_m2b.json \
    --upgrade

# Headless:
hermes olo google connect \
    --account acme \
    --client-secret-file /path/to/client_secret_m2b.json \
    --upgrade --manual
```

The `--upgrade` flag:
1. Verifies the new scopes are granted (`gmail.readonly`, not `gmail.metadata`).
2. Saves the credential with `scope_profile = "full_v1"`.
3. Clears the Gmail checkpoint so backfill starts on the next run.

After upgrading, the next Gmail run enters **backfill mode**: it uses `messages.list` to
scan backwards from the current time and emit `email.content` signals for existing messages.
Backfill continues across multiple runs (cursor carries the `{"phase":"backfill",...}` JSON).

```bash
# Verify after upgrade:
hermes olo google status --check
# Must show: gmail.readonly ✓, calendar.events.readonly ✓,
#            calendar.calendarlist.readonly ✓, scope_profile=full_v1
```

### Backfill Phases

| Mode | Trigger | Behavior |
|---|---|---|
| `bootstrap` | Fresh account, no prior signals | Scans all messages from epoch=0 (up to lookback) |
| `resume` | Checkpoint exists (plain historyId) | Resumes from that historyId |
| `gap` | History ID expired, signals exist | Records unrecoverable gap, then continues |

Backfill emits `email.content` only (not `email.received`/`email.sent` — those come
from incremental history sync). Backfill is bounded by a 48 MiB soft stop per run.

### Purging Full Content

If you need to remove stored message blobs (e.g., before sharing a database):

```bash
# Remove all content_blobs for a source (irreversible):
hermes olo purge-content --source gmail_acme --confirm
```

This deletes rows from `content_blobs` and clears the `blobs` reference in
the corresponding signal payloads. It does not remove the signal rows themselves.

### Composite Calendar Cursor (v2)

The Calendar cursor is now a JSON composite object:

```json
{
  "v": 2,
  "list": {"sync_token": "...", "page_token": null},
  "cals": {
    "primary": {"phase": "incr", "sync_token": "...", "page_token": null},
    "work@group.calendar.google.com": {"phase": "incr", "sync_token": "..."}
  }
}
```

- `list`: calendarList sync state (discovers new/removed calendars each tick).
- `cals`: per-calendar events cursor (independent incremental sync per calendar).
- **v1 cursors** (`{"phase":"incr",...}`) from M2 are automatically migrated on first use.

### Recovery

#### calendarList sync token expired

**Symptom:** Calendar run fails with `calendarList sync token expired`.

**Fix:** The adapter self-heals automatically — it restarts calendarList full sync and
continues. No manual intervention needed.

#### Single calendar 410 (events sync token expired)

**Fix:** The adapter self-heals per-calendar — restarts full sync for the affected
calendar only. Other calendars are unaffected.

#### Upgrading resets the Gmail checkpoint

After `--upgrade`, the Gmail checkpoint is intentionally cleared. The next run enters
backfill mode. This is expected and completes automatically over several runs.

---

## Production Paths

### Path A: New Account (M2b from the start)

```bash
hermes olo google connect \
    --account acme \
    --client-secret-file /path/to/client_secret_m2b.json
# grants: gmail.readonly, calendar.events.readonly, calendar.calendarlist.readonly
# scope_profile = full_v1
```

Setup monitors:

```bash
hermes olo setup --source gmail --option account=acme
hermes olo setup --source gcal --option account=acme --option lookback_days=30

hermes cron create "every 5m" --name olo-gmail-acme \
    --script olo_monitor_gmail_acme.py --no-agent --deliver local

hermes cron create "every 15m" --name olo-gcal-acme \
    --script olo_monitor_gcal_acme.py --no-agent --deliver local
```

### Path B: Existing M2 Account Upgrade

```bash
# 1. Update consent screen scopes in Google Cloud Console (see §M2b setup above).
# 2. Upgrade:
hermes olo google connect \
    --account acme \
    --client-secret-file /path/to/client_secret_m2b.json \
    --upgrade
# 3. Restart monitors (existing cron jobs work unchanged — adapter auto-detects scope_profile).
```

The Gmail monitor will enter backfill mode automatically on the next run.
The Calendar monitor will discover all calendars (not just primary) on the next run.
