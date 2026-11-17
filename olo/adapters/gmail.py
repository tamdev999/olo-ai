"""Olo AI — Gmail adapter (metadata-only, no message bodies).

§5: sync algorithm, signal mapping, §5.0 empty-page rule.
I5: empty history[] with nextPageToken raises SourceError at cap.
I7: alias pattern enforcement.
Registered as "gmail"; per-account source = "gmail_<alias>".

No side effects at import time.
"""
from __future__ import annotations

import email.header
import email.utils
import hashlib
import re
from collections.abc import Mapping
from datetime import datetime, timezone, UTC
from typing import Optional

from olo.adapters import (
    AdapterConfigError,
    FetchBatch,
    MonitorAdapter,
    SourceError,
)
from olo.signals import NormalizedSignal

# ── Constants ─────────────────────────────────────────────────────────────────
_SYSTEM_LABELS = frozenset({
    "INBOX", "UNREAD", "STARRED", "IMPORTANT",
    "SENT", "TRASH", "SPAM", "DRAFT", "CHAT",
})
_SKIP_LABELS = frozenset({"DRAFT", "SPAM", "TRASH", "CHAT"})
_CHANGE_SIGNAL_LABELS = frozenset({
    "INBOX", "UNREAD", "STARRED", "IMPORTANT", "SENT", "TRASH", "SPAM",
})
_METADATA_HEADERS = [
    "From", "To", "Cc", "Subject", "Date",
    "Message-ID", "In-Reply-To",
]
_MAX_ADDRESSES = 50
_MAX_HEADER_CHARS = 998
_MAX_SUBJECT_CHARS = 256
_MAX_EMPTY_PAGES = 20

# I7: alias pattern
_ALIAS_RE = re.compile(r"^[a-z0-9]{1,16}$")


# ── Signal subject from RFC 2047 header ──────────────────────────────────────

def _decode_header_value(raw: str) -> Optional[str]:
    """RFC 2047 decode, whitespace-collapse, truncate to 256 chars."""
    if not raw:
        return None
    try:
        parts = email.header.decode_header(raw)
        decoded = str(email.header.make_header(parts))
    except Exception:
        decoded = raw
    collapsed = re.sub(r"\s+", " ", decoded).strip()
    if not collapsed:
        return None
    return collapsed[:_MAX_SUBJECT_CHARS]


def _parse_addresses(raw: str) -> list[dict]:
    if not raw:
        return []
    raw = raw[:_MAX_HEADER_CHARS]
    pairs = email.utils.getaddresses([raw])
    result = []
    for name, addr in pairs[:_MAX_ADDRESSES]:
        result.append({"name": name.strip(), "email": addr.strip().lower()})
    return result


def _extract_headers(payload_headers: list[dict]) -> dict:
    hmap: dict[str, str] = {}
    for h in payload_headers:
        name = h.get("name", "").strip().lower()
        value = h.get("value", "")
        hmap[name] = value
    return {
        "from": _parse_addresses(hmap.get("from", "")),
        "to": _parse_addresses(hmap.get("to", "")),
        "cc": _parse_addresses(hmap.get("cc", "")),
        # F5b: cap non-address header strings at _MAX_HEADER_CHARS (998)
        "subject": hmap.get("subject", "")[:_MAX_HEADER_CHARS],
        "date": hmap.get("date", "")[:_MAX_HEADER_CHARS],
        "message_id_header": hmap.get("message-id", "")[:_MAX_HEADER_CHARS],
        "in_reply_to": hmap.get("in-reply-to", "")[:_MAX_HEADER_CHARS],
    }


# ── Gmail adapter ─────────────────────────────────────────────────────────────

class GmailAdapter(MonitorAdapter):
    """Read-only Gmail metadata adapter.

    Options: account=<alias> (required; no others).
    source = "gmail_<alias>" (instance attribute, V7).
    """

    source: str = "gmail_unknown"

    def __init__(
        self, options: Mapping[str, str], *, client: Optional["GoogleClient"] = None
    ) -> None:
        alias = options.get("account", "").strip()
        if not alias:
            raise AdapterConfigError(
                "gmail adapter requires option account=<alias>"
            )
        # I7: enforce alias pattern
        if not _ALIAS_RE.match(alias):
            raise AdapterConfigError(
                f"gmail adapter: invalid account alias {alias!r}; "
                f"must match ^[a-z0-9]{{1,16}}$"
            )
        extra = set(options) - {"account"}
        if extra:
            raise AdapterConfigError(
                f"gmail adapter does not accept options: {sorted(extra)}"
            )
        self._alias = alias
        self.source = f"gmail_{alias}"
        self._client: Optional["GoogleClient"] = client

    def _get_client(self) -> "GoogleClient":
        if self._client is None:
            from olo.google.credentials import load_credentials
            from olo.google.http import GoogleClient
            creds = load_credentials(self._alias)
            if not creds.has_gmail_scope():
                raise AdapterConfigError(
                    f"account {self._alias!r} has no gmail.metadata scope; "
                    f"run: hermes olo google connect --account {self._alias}"
                )
            self._client = GoogleClient(creds)
        return self._client

    # ── §5.2 sync algorithm ───────────────────────────────────────────────────

    def fetch(self, cursor: str | None, *, limit: int) -> FetchBatch:
        """Return up to *limit* signals; never returns empty with has_more=True."""
        if limit < 1:
            raise AdapterConfigError(f"fetch limit must be ≥ 1, got {limit}")

        client = self._get_client()

        if cursor is None:
            return self._bootstrap(client)
        return self._incremental(client, cursor, limit)

    def _bootstrap(self, client: "GoogleClient") -> FetchBatch:
        """§5.2 step 1: get profile historyId, return empty batch."""
        profile = client.get_json("gmail", "/users/me/profile", [])
        history_id = str(profile.get("historyId", ""))
        if not history_id:
            raise SourceError("gmail profile returned no historyId")
        return FetchBatch(signals=(), next_cursor=history_id, has_more=False)

    def _incremental(
        self,
        client: "GoogleClient",
        cursor: str,
        limit: int,
    ) -> FetchBatch:
        """§5.2 step 2 + F2 fix: incremental sync from cursor.

        F2 fix: carry page_token across loop iterations so every request uses
        the current token consistently.  The old code did a one-shot re-fetch
        inside the empty-page branch; if that second response was also empty
        the next loop iteration would rebuild params without any pageToken,
        producing a None→p2→None→p2 oscillation that never reached real records.

        Contract:
        - page_token is local to one fetch() call; never persisted.
        - Every history.list request uses startHistoryId=in_memory_cursor plus
          pageToken when set.
        - Empty history[] + token: count toward _MAX_EMPTY_PAGES, set
          page_token = token, continue.
        - Records present: process them (record-whole rules unchanged). After a
          signal-less page with a token set in_memory_cursor to the last record
          id and page_token = token.
        - Empty history[] without token: return ((), response historyId, False).
          Only this case may advance to the mailbox historyId.
        - Cap reached while tokens keep arriving: raise SourceError.
        """
        empty_page_count = 0
        in_memory_cursor = cursor
        page_token: str | None = None
        all_signals: list[NormalizedSignal] = []
        final_has_more = False
        final_cursor = cursor
        mailbox_history_id = cursor  # updated each response

        while True:
            # ── Build request params ──────────────────────────────────────────
            params: list[tuple[str, str]] = [
                ("startHistoryId", in_memory_cursor),
                ("historyTypes", "messageAdded"),
                ("historyTypes", "messageDeleted"),
                ("historyTypes", "labelAdded"),
                ("historyTypes", "labelRemoved"),
                ("maxResults", str(min(limit, 500))),
            ]
            if page_token is not None:
                params.append(("pageToken", page_token))

            resp = client.get_json("gmail", "/users/me/history", params)

            mailbox_history_id = str(resp.get("historyId", in_memory_cursor))
            records = resp.get("history", [])
            next_page_token = resp.get("nextPageToken")

            # ── Empty page ────────────────────────────────────────────────────
            if not records:
                if not next_page_token:
                    # Terminal empty page: advance cursor to mailbox historyId.
                    return FetchBatch(
                        signals=(),
                        next_cursor=mailbox_history_id,
                        has_more=False,
                    )
                # Empty page with token: count, set token, loop.
                empty_page_count += 1
                if empty_page_count >= _MAX_EMPTY_PAGES:
                    raise SourceError(
                        "gmail: too many consecutive empty history pages with "
                        "nextPageToken — sync appears blocked; "
                        "run 'hermes olo checkpoint reset' to restart"
                    )
                page_token = next_page_token
                continue

            # ── Records present ───────────────────────────────────────────────
            batch_signals, last_included_id, records_remain = self._process_records(
                client, records, limit - len(all_signals), in_memory_cursor
            )

            if batch_signals:
                all_signals.extend(batch_signals)
                final_cursor = last_included_id
                final_has_more = records_remain or (next_page_token is not None)
                break
            else:
                # §5.0: signal-less page — advance in-memory cursor, loop.
                empty_page_count += 1
                in_memory_cursor = str(records[-1].get("id", in_memory_cursor))
                if not next_page_token:
                    return FetchBatch(
                        signals=(),
                        next_cursor=mailbox_history_id,
                        has_more=False,
                    )
                if empty_page_count >= _MAX_EMPTY_PAGES:
                    return FetchBatch(
                        signals=(),
                        next_cursor=in_memory_cursor,
                        has_more=False,
                    )
                page_token = next_page_token
                continue

        if not all_signals:
            return FetchBatch(
                signals=(),
                next_cursor=mailbox_history_id,
                has_more=False,
            )

        return FetchBatch(
            signals=tuple(all_signals),
            next_cursor=final_cursor,
            has_more=final_has_more,
        )

    def _process_records(
        self,
        client: "GoogleClient",
        records: list[dict],
        remaining: int,
        base_cursor: str,
    ) -> tuple[list[NormalizedSignal], str, bool]:
        """Process history records into signals.

        Returns (signals, last_included_record_id, records_remain).
        A record is included whole or not at all.
        """
        signals: list[NormalizedSignal] = []
        last_id = base_cursor
        seen_message_ids: set[str] = set()

        for record in records:
            record_id = str(record.get("id", ""))
            if not record_id:
                continue

            record_signals = self._record_to_signals(
                client, record, seen_message_ids
            )

            if signals and len(signals) + len(record_signals) > remaining:
                return signals, last_id, True

            if not signals and len(record_signals) > remaining:
                raise SourceError(
                    "gmail history record exceeds batch limit; raise --limit"
                )

            if record_signals:
                signals.extend(record_signals)
            last_id = record_id

        return signals, last_id, False

    def _record_to_signals(
        self,
        client: "GoogleClient",
        record: dict,
        seen_ids: set[str],
    ) -> list[NormalizedSignal]:
        """Convert one history record to signals (new messages + change events)."""
        record_id = str(record.get("id", ""))
        now_utc = datetime.now(UTC)
        signals: list[NormalizedSignal] = []

        for entry in record.get("messagesAdded", []):
            msg_stub = entry.get("message", {})
            msg_id = msg_stub.get("id", "")
            if not msg_id or msg_id in seen_ids:
                continue
            msg = client.get_message_or_none(msg_id, _METADATA_HEADERS)
            if msg is None:
                continue
            label_ids: list[str] = msg.get("labelIds", [])
            if any(lbl in _SKIP_LABELS for lbl in label_ids):
                continue
            sig = self._message_to_signal(msg, record_id)
            if sig is not None:
                signals.append(sig)
                seen_ids.add(msg_id)

        for entry in record.get("messagesDeleted", []):
            msg_stub = entry.get("message", {})
            msg_id = msg_stub.get("id", "")
            if not msg_id:
                continue
            # F5b: skip deletions on DRAFT or CHAT messages (spec §5.2)
            msg_label_ids: list[str] = msg_stub.get("labelIds", [])
            if any(lbl in _SKIP_LABELS for lbl in msg_label_ids):
                continue
            ext_id = f"h{record_id}:{msg_id}:deleted"
            signals.append(NormalizedSignal(
                source=self.source,
                external_id=ext_id,
                kind="email.deleted",
                occurred_at=now_utc,
                subject=None,
                payload={
                    "message_id": msg_id,
                    "thread_id": msg_stub.get("threadId", ""),
                    "history_id": record_id,
                },
            ))

        for event_type, event_kind in [
            ("labelsAdded", "email.labels_added"),
            ("labelsRemoved", "email.labels_removed"),
        ]:
            for entry in record.get(event_type, []):
                msg_stub = entry.get("message", {})
                msg_id = msg_stub.get("id", "")
                # F5b: skip label changes on DRAFT or CHAT messages (spec §5.2)
                msg_label_ids = msg_stub.get("labelIds", [])
                if any(lbl in _SKIP_LABELS for lbl in msg_label_ids):
                    continue
                changed_labels = [
                    lbl for lbl in entry.get("labelIds", [])
                    if lbl in _CHANGE_SIGNAL_LABELS
                ]
                if not changed_labels or not msg_id:
                    continue
                current_labels = [
                    lbl for lbl in msg_stub.get("labelIds", [])
                    if lbl in _CHANGE_SIGNAL_LABELS
                ]
                suffix = "added" if event_type == "labelsAdded" else "removed"
                ext_id = f"h{record_id}:{msg_id}:{suffix}"
                signals.append(NormalizedSignal(
                    source=self.source,
                    external_id=ext_id,
                    kind=event_kind,
                    occurred_at=now_utc,
                    subject=None,
                    payload={
                        "message_id": msg_id,
                        "thread_id": msg_stub.get("threadId", ""),
                        "history_id": record_id,
                        "labels": changed_labels,
                        "current_label_ids": current_labels,
                    },
                ))

        return signals

    def _message_to_signal(self, msg: Mapping, record_id: str) -> Optional[NormalizedSignal]:
        msg_id = msg.get("id", "")
        thread_id = msg.get("threadId", "")
        label_ids: list[str] = msg.get("labelIds", [])
        kind = "email.sent" if "SENT" in label_ids else "email.received"

        try:
            ts_ms = int(msg.get("internalDate", "0"))
            occurred_at = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
        except (ValueError, TypeError):
            occurred_at = datetime.now(UTC)

        payload_headers = msg.get("payload", {}).get("headers", [])
        hdrs = _extract_headers(payload_headers)
        subject = _decode_header_value(hdrs.get("subject", ""))

        payload: dict = {
            "message_id": msg_id,
            "thread_id": thread_id,
            "label_ids": label_ids,
            "size_estimate": msg.get("sizeEstimate", 0),
            "history_id": record_id,
            "headers": {
                "from": hdrs["from"],
                "to": hdrs["to"],
                "cc": hdrs["cc"],
                "subject": hdrs.get("subject", ""),
                "date": hdrs.get("date", ""),
                "message_id_header": hdrs.get("message_id_header", ""),
                "in_reply_to": hdrs.get("in_reply_to", ""),
            },
        }

        return NormalizedSignal(
            source=self.source,
            external_id=msg_id,
            kind=kind,
            occurred_at=occurred_at,
            subject=subject,
            payload=payload,
        )
