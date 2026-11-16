# Olo AI — Google Workspace Integration Guide

## Overview

Olo AI monitors your Gmail and Google Calendar using read-only OAuth scopes.
No message bodies, attachments, or event descriptions are stored. All data
stays in a local SQLite database (`olo.db`).

---

## BBC Cloud Setup Checklist (§3.3)

Before running `hermes olo google connect`, complete these steps:

1. **Create a dedicated Google Cloud project** at https://console.cloud.google.com/
2. **Enable APIs**: Gmail API and Google Calendar API
3. **Configure the OAuth consent screen**:
   - Audience: **External**
   - Publishing status: **Testing** (see §External/Testing consequences below)
   - Add the dedicated test account as a **test user**
   - On the **Data Access** page, add both scopes:
     - `https://www.googleapis.com/auth/gmail.metadata`
     - `https://www.googleapis.com/auth/calendar.events.owned.readonly`
4. **Create an OAuth client**:
   - Application type: **Desktop app**
   - Name: "Olo AI Monitor" (or similar)
   - Download the client secret JSON file

---

## External/Testing Consequences

**7-day token expiry.** When the consent screen is in Testing status, Google
issues refresh tokens that expire 7 days after the user consented. You must
run `hermes olo google connect --account <alias>` again each week.

**Unverified app warning.** Users will see a "Google hasn't verified this app"
warning on the consent screen. This is expected for private/testing use.

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

# Headless / no browser on this machine:
hermes olo google connect --account acme --client-secret-file /path/to/client_secret.json --manual
# (paste the full redirect URL from your browser)
```

`<account>` is an alias matching `^[a-z0-9]{1,16}$`.  It becomes part of
the source name: `gmail_acme`, `gcal_acme`.

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

---

## Status

```bash
# Show all accounts, scopes, and checkpoint phases:
hermes olo status

# Show connected Google accounts with expiry info:
hermes olo google status

# Live token refresh check:
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
