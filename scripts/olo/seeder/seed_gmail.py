"""Gmail seeder v2: generate P1-P8 test messages via Gmail API.

Fixes:
- P8: single messages.import with INBOX+SENT+UNREAD → one message, one signal.
- P5/P6/P7 threading: messages.insert with explicit threadId for all three.
  (messages.import rejects threadId; messages.insert accepts it.)
- P1-P4: messages.import with INBOX+UNREAD (no threadId needed).

Nothing here touches Olo's production code or credentials.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

MARKER        = "OLO-BODY-MARKER-SEEDER-STAGE1"
EXTERNAL_FROM = "olo-test-sender@example-olo-test.invalid"
EXTERNAL_NAME = "OLO Test Sender"


# ── RFC 5322 builder ──────────────────────────────────────────────────────────

def _build_raw(
    msg_from: str,
    msg_to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    reply_to_msg_id: str | None = None,
    date: str | None = None,
) -> str:
    from email.mime.text import MIMEText
    from email.utils import formatdate
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"]    = msg_from
    msg["To"]      = msg_to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    msg["Message-ID"] = f"<olo-seed-{int(time.time()*1000)}@example-olo-test.invalid>"
    if reply_to_msg_id:
        msg["In-Reply-To"] = reply_to_msg_id
        msg["References"]  = reply_to_msg_id
    msg["Date"] = date or formatdate()
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


# ── API helpers ───────────────────────────────────────────────────────────────

def _media(raw: str):
    from googleapiclient.http import MediaInMemoryUpload
    return MediaInMemoryUpload(
        base64.urlsafe_b64decode(raw), mimetype="message/rfc822"
    )


def _fetch(svc, mid: str) -> dict:
    return svc.users().messages().get(
        userId="me", id=mid, format="metadata",
        metadataHeaders=["Message-ID", "Subject", "From", "To", "In-Reply-To"],
    ).execute()


def _import(svc, raw: str, labels: list[str]) -> dict:
    """messages.import — does NOT support threadId."""
    result = svc.users().messages().import_(
        userId="me",
        body={"labelIds": labels},
        media_body=_media(raw),
        internalDateSource="dateHeader",
        processForCalendar=False,
        deleted=False,
        neverMarkSpam=True,
    ).execute()
    return _fetch(svc, result["id"])


def _insert(svc, raw: str, labels: list[str],
            thread_id: str | None = None) -> dict:
    """messages.insert — supports threadId for forcing thread membership."""
    body: dict = {"labelIds": labels}
    if thread_id:
        body["threadId"] = thread_id
    result = svc.users().messages().insert(
        userId="me",
        body=body,
        media_body=_media(raw),
        internalDateSource="dateHeader",
    ).execute()
    return _fetch(svc, result["id"])


def _hdr(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"] == name:
            return h["value"]
    return ""


def get_history_id(svc) -> str:
    return svc.users().getProfile(userId="me").execute()["historyId"]


# ── Seeder ────────────────────────────────────────────────────────────────────

def run_seeder(svc, account_email: str, state_path: str) -> dict:
    if os.path.exists(state_path):
        with open(state_path) as f:
            existing = json.load(f)
        if existing.get("seeder_done"):
            print("Gmail seeder already ran; returning existing state.")
            return existing

    print(f"\n=== Gmail Seeder v2 ===")
    print(f"Account: {account_email}  Marker: {MARKER}\n")

    hid_before = get_history_id(svc)
    print(f"historyId before: {hid_before}")

    from email.utils import formatdate
    def ts(offset: int = 0) -> str:
        return formatdate(time.time() - offset)

    state: dict = {}

    # ── P1 received ───────────────────────────────────────────────────────────
    print("P1...")
    r = _import(svc, _build_raw(
        f"{EXTERNAL_NAME} <{EXTERNAL_FROM}>", account_email,
        "[OLO-LIVE-P1] Alpha signal", f"P1. {MARKER}", date=ts(70),
    ), ["INBOX", "UNREAD"])
    state["p1_id"] = r["id"]
    print(f"  id={r['id']}")

    # ── P2 received with Cc ───────────────────────────────────────────────────
    print("P2...")
    r = _import(svc, _build_raw(
        f"{EXTERNAL_NAME} <{EXTERNAL_FROM}>", account_email,
        "[OLO-LIVE-P2] Beta signal", f"P2. {MARKER}",
        cc="olo-cc@example-olo-test.invalid", date=ts(60),
    ), ["INBOX", "UNREAD"])
    state["p2_id"] = r["id"]
    print(f"  id={r['id']}")

    # ── P3 received ───────────────────────────────────────────────────────────
    print("P3...")
    r = _import(svc, _build_raw(
        f"{EXTERNAL_NAME} <{EXTERNAL_FROM}>", account_email,
        "[OLO-LIVE-P3] Gamma signal", f"P3. {MARKER}", date=ts(50),
    ), ["INBOX", "UNREAD"])
    state["p3_id"] = r["id"]
    print(f"  id={r['id']}")

    # ── P4 received (to be trashed in Round 2) ────────────────────────────────
    print("P4...")
    r = _import(svc, _build_raw(
        f"{EXTERNAL_NAME} <{EXTERNAL_FROM}>", account_email,
        "[OLO-LIVE-P4] Delta signal", f"P4. {MARKER}", date=ts(40),
    ), ["INBOX", "UNREAD"])
    state["p4_id"] = r["id"]
    print(f"  id={r['id']}")

    # ── P5: thread anchor — use insert so we get a fresh thread under our control
    print("P5 (thread anchor)...")
    r_p5 = _insert(svc, _build_raw(
        f"{EXTERNAL_NAME} <{EXTERNAL_FROM}>", account_email,
        "[OLO-LIVE-P5] Epsilon signal", f"P5. {MARKER}", date=ts(30),
    ), ["INBOX", "UNREAD"])
    state["p5_id"]            = r_p5["id"]
    state["p5_thread"]        = r_p5["threadId"]
    state["p5_msg_id_header"] = _hdr(r_p5, "Message-ID")
    print(f"  id={r_p5['id']}  thread={r_p5['threadId']}")

    # ── P6: sent reply, forced into P5's thread ───────────────────────────────
    print("P6 (sent reply, threadId=P5's thread)...")
    r_p6 = _insert(svc, _build_raw(
        account_email, f"{EXTERNAL_NAME} <{EXTERNAL_FROM}>",
        "Re: [OLO-LIVE-P5] Epsilon signal", f"P6. {MARKER}",
        reply_to_msg_id=state["p5_msg_id_header"], date=ts(20),
    ), ["SENT"], thread_id=state["p5_thread"])
    state["p6_id"]            = r_p6["id"]
    state["p6_thread"]        = r_p6["threadId"]
    state["p6_msg_id_header"] = _hdr(r_p6, "Message-ID")
    print(f"  id={r_p6['id']}  thread={r_p6['threadId']}")
    _assert_thread(r_p6, state["p5_thread"], "P6")

    # ── P7: received reply, forced into P5's thread ───────────────────────────
    print("P7 (received reply to P6, threadId=P5's thread)...")
    r_p7 = _insert(svc, _build_raw(
        f"{EXTERNAL_NAME} <{EXTERNAL_FROM}>", account_email,
        "Re: [OLO-LIVE-P5] Epsilon signal", f"P7. {MARKER}",
        reply_to_msg_id=state["p6_msg_id_header"], date=ts(10),
    ), ["INBOX", "UNREAD"], thread_id=state["p5_thread"])
    state["p7_id"]    = r_p7["id"]
    state["p7_thread"] = r_p7["threadId"]
    print(f"  id={r_p7['id']}  thread={r_p7['threadId']}")
    _assert_thread(r_p7, state["p5_thread"], "P7")

    # ── P8: single self-email (INBOX + SENT + UNREAD → one message, one signal)
    print("P8 (self-email, INBOX+SENT+UNREAD, single message)...")
    r_p8 = _import(svc, _build_raw(
        account_email, account_email,
        "[OLO-LIVE-P8] Self-loop signal", f"P8. {MARKER}", date=ts(5),
    ), ["INBOX", "UNREAD", "SENT"])
    state["p8_id"]    = r_p8["id"]
    state["p8_thread"] = r_p8["threadId"]
    print(f"  id={r_p8['id']}  labels={r_p8.get('labelIds')}")

    # ── Verify ────────────────────────────────────────────────────────────────
    hid_after = get_history_id(svc)
    delta = int(hid_after) - int(hid_before)
    print(f"\nhistoryId: {hid_before} → {hid_after}  Δ={delta}")
    assert delta > 0, f"STOP: historyId did not advance"

    p5t, p6t, p7t = state["p5_thread"], state.get("p6_thread"), state.get("p7_thread")
    if p5t == p6t == p7t:
        print(f"Threading P5/P6/P7: all in {p5t} ✓")
    else:
        print(f"Threading NOTE: P5={p5t}  P6={p6t}  P7={p7t}")

    state.update(hid_before_seed=hid_before, hid_after_seed=hid_after,
                 seeder_done=True, marker=MARKER)
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)
    print(f"State saved: {state_path}")
    return state


def _assert_thread(msg: dict, expected_thread: str, label: str) -> None:
    actual = msg["threadId"]
    if actual == expected_thread:
        print(f"  {label} thread ✓")
    else:
        print(f"  WARNING {label}: thread={actual} != expected={expected_thread}")
