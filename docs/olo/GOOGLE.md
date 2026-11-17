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

- **No backfill.** Gmail bootstrap sets the cursor to the current historyId;
  mail received before setup is not ingested.
- **Weekly reconnect** required when `oauth_publishing_status = "testing"`.
- **Checkpoint reset gap.** After `checkpoint reset`, events/messages from
  the gap period (since the last committed cursor) are not visible in Olo.
  New events/messages from that point forward will be ingested on the next run.
- **GitHub Actions disabled.** The `olo-guard.yml` CI workflow is inactive until
  BBC re-enables Actions. The same checks run locally via `scripts/olo/check_core_diff.sh`.

---

## Attribution

Built on **Hermes Agent by Nous Research**. See `LICENSE` for terms.
