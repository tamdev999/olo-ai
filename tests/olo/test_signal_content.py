"""tests/olo/test_signal_content.py — S5 part 1: one production preparation path.

Signals here are built by the real Gmail adapters (``gmail_full``
build_email_content_signal and ``gmail`` metadata conversion) from Gmail API
message shapes, then prepared through olo.signal_content — the same path the
extractor, the Hindsight item builder and the worker use.  No network.
"""
from __future__ import annotations

import base64
import json
from olo.store import connect as store_connect
from datetime import UTC, datetime
from pathlib import Path

import pytest

from olo import extractor
from olo.adapters.contacts import IDENTITY_AMBIGUOUS, resolve_signal_identity
from olo.adapters.gmail import GmailAdapter
from olo.adapters.gmail_full import GMAIL_MSG_MEDIA_TYPE, build_email_content_signal
from olo.extractor import ExtractionStatus, extract_prepared
from olo.signal_content import (
    PREP_CONTENT_ERROR,
    PREP_MISSING_CONTENT,
    PREP_NO_DIRECTION,
    PREP_NO_TEXT,
    PREP_READY,
    load_content,
    load_prepared_signal,
    prepare_signal,
)

pytestmark = pytest.mark.not_integration

OWNER = "owner@biz.example"
SOURCE = "gmail_biz"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _gmail_msg(*, sent: bool, to: str, frm: str, subject: str, plain: str | None = None,
               html: str | None = None, msg_id: str = "m1") -> dict:
    parts = []
    if plain is not None:
        parts.append({"partId": "0", "mimeType": "text/plain", "filename": "",
                      "body": {"size": len(plain), "data": _b64(plain)}})
    if html is not None:
        parts.append({"partId": "1", "mimeType": "text/html", "filename": "",
                      "body": {"size": len(html), "data": _b64(html)}})
    return {
        "id": msg_id, "threadId": "t1", "historyId": "9",
        "labelIds": ["SENT"] if sent else ["INBOX"],
        "internalDate": str(int(datetime(2024, 3, 20, 15, 30, tzinfo=UTC).timestamp() * 1000)),
        "sizeEstimate": 1000,
        "payload": {
            "mimeType": "multipart/alternative", "filename": "", "body": {"size": 0},
            "headers": [
                {"name": "From", "value": frm}, {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
                {"name": "Message-ID", "value": f"<{msg_id}@mail>"},
            ],
            "parts": parts,
        },
    }


def _content_signal(**kw):
    return build_email_content_signal(SOURCE, _gmail_msg(**kw), "9")


def _reader(sig):
    blobs = {b.sha256: b.data for b in sig.blobs}
    return blobs.get


def _prepare(sig, reader=None):
    return prepare_signal(sig.kind, sig.payload, subject=sig.subject,
                          occurred_at=sig.occurred_at,
                          blob_reader=reader if reader is not None else _reader(sig))


RECEIVED_BODY = (
    "Hi, please deliver order ORD-7 on 2024-04-10.\n\n"
    "On Tue, 19 Mar 2024, Owner <owner@biz.example> wrote:\n"
    "> We can deliver on 2024-04-03.\n"
)


# ── Real content from Gmail full-content signals ──────────────────────────────

class TestGmailContent:
    def test_received_message_renders_subject_and_body(self) -> None:
        sig = _content_signal(sent=False, to=OWNER, frm="Alice <alice@x.com>",
                              subject="Order ORD-7", plain=RECEIVED_BODY)
        p = _prepare(sig)
        assert p.status == PREP_READY
        assert p.actor_role == "customer"
        assert p.full_text.startswith("Subject: Order ORD-7\n\nHi, please deliver")
        assert "> We can deliver on 2024-04-03." in p.full_text       # evidence kept
        assert "2024-04-03" not in p.authored_text                     # quote excluded
        assert "ORD-7 on 2024-04-10" in p.authored_text
        assert p.content_sha256 == sig.blobs[0].sha256
        assert p.thread_id == "t1"
        assert p.occurred_at == "2024-03-20T15:30:00.000000Z"

    def test_sent_message_is_business(self) -> None:
        sig = _content_signal(sent=True, to="Alice <alice@x.com>", frm=OWNER,
                              subject="Confirmed", plain="Delivery confirmed for 2024-04-10.")
        p = _prepare(sig)
        assert p.status == PREP_READY and p.actor_role == "business"

    def test_html_only_message_quotes_blockquotes(self) -> None:
        html = ("<div>Delivery confirmed for 2024-04-10.</div>"
                "<blockquote>Can you do 2024-04-03?</blockquote>")
        sig = _content_signal(sent=True, to="alice@x.com", frm=OWNER, subject="Re", html=html)
        p = _prepare(sig)
        assert "Delivery confirmed for 2024-04-10." in p.authored_text
        assert "> Can you do 2024-04-03?" in p.full_text
        assert "2024-04-03" not in p.authored_text

    def test_feeder_renders_gmail_documents_not_raw_json(self) -> None:
        sig = _content_signal(sent=False, to=OWNER, frm="alice@x.com",
                              subject="Hello", plain="Body text.")
        loaded = load_content(sig.payload, _reader(sig))
        assert loaded.status == "ok" and loaded.media_type == GMAIL_MSG_MEDIA_TYPE
        assert loaded.rendered == "Subject: Hello\n\nBody text."
        assert not loaded.rendered.lstrip().startswith("{")

    def test_plain_text_blob_is_verbatim(self) -> None:
        data = b"  exact synthetic text\r\nwith spacing  "
        import hashlib
        sha = hashlib.sha256(data).hexdigest()
        payload = {"content_refs": [{"sha256": sha, "media_type": "text/plain;charset=utf-8"}]}
        assert load_content(payload, {sha: data}.get).rendered == data.decode()


# ── Missing, corrupt and untrusted content ────────────────────────────────────

class TestFailures:
    def test_missing_blob_is_an_explicit_failure(self) -> None:
        sig = _content_signal(sent=False, to=OWNER, frm="a@x.com", subject="S", plain="B")
        p = _prepare(sig, reader=lambda sha: None)
        assert p.status == PREP_MISSING_CONTENT and not p.ready
        out = extract_prepared(p, SOURCE, model_fn=lambda q: 1 / 0)
        assert out.status == ExtractionStatus.CONTENT_ERROR

    def test_corrupt_blob_is_an_explicit_failure(self) -> None:
        sig = _content_signal(sent=False, to=OWNER, frm="a@x.com", subject="S", plain="B")
        p = _prepare(sig, reader=lambda sha: b"tampered")
        assert p.status == PREP_CONTENT_ERROR

    def test_content_without_direction_is_not_trusted(self) -> None:
        sig = _content_signal(sent=False, to=OWNER, frm="a@x.com", subject="S", plain="B")
        payload = {k: v for k, v in sig.payload.items() if k != "direction"}
        p = prepare_signal(sig.kind, payload, blob_reader=_reader(sig))
        assert p.status == PREP_NO_DIRECTION and p.actor_role is None
        out = extract_prepared(p, SOURCE, model_fn=lambda q: 1 / 0)
        assert out.status == ExtractionStatus.REJECTED

    def test_transcript_without_direction_is_not_trusted(self) -> None:
        p = prepare_signal("call.transcript", {"transcript": "Agent: delivery 2024-04-10."})
        assert p.status == PREP_NO_DIRECTION

    def test_empty_message_is_no_text(self) -> None:
        sig = _content_signal(sent=False, to=OWNER, frm="a@x.com", subject="", plain="")
        assert _prepare(sig).status == PREP_NO_TEXT


# ── Metadata signals and participants ─────────────────────────────────────────

class TestMetadataAndParticipants:
    def _meta(self, **kw):
        adapter = GmailAdapter({"account": "biz"})
        return adapter._message_to_signal(_gmail_msg(**kw), "9")

    def test_metadata_sent_signal_uses_header_subject(self) -> None:
        sig = self._meta(sent=True, to="alice@x.com", frm=OWNER,
                         subject="Delivery on 2024-04-10", plain="ignored")
        p = prepare_signal(sig.kind, sig.payload, subject=sig.subject, occurred_at=sig.occurred_at)
        assert sig.kind == "email.sent"
        assert p.status == PREP_READY and p.actor_role == "business"
        assert p.full_text == "Subject: Delivery on 2024-04-10"

    def test_ambiguous_participants_are_flagged_not_guessed(self) -> None:
        sig = _content_signal(sent=True, to="alice@x.com, bob@y.com", frm=OWNER,
                              subject="Schedule", plain="Delivery confirmed for 2024-04-10.")
        assert _prepare(sig).actor_role == "business"
        ident = resolve_signal_identity(sig.kind, sig.payload, {})
        assert ident.status == IDENTITY_AMBIGUOUS


# ── One path: synthetic corpus shape and production share preparation ─────────

class TestSharedPath:
    def test_inline_corpus_payload_text_is_unchanged(self) -> None:
        payload = {"subject": "Delivery REF-1", "body": "Scheduled for 2024-03-15."}
        expected = "Subject: Delivery REF-1\n\nScheduled for 2024-03-15."
        assert prepare_signal("email.sent", payload).full_text == expected
        assert extractor._signal_text("email.sent", payload) == expected

    def test_extraction_from_gmail_content(self) -> None:
        sig = _content_signal(sent=True, to="alice@x.com", frm=OWNER, subject="ORD-7",
                              plain="Your delivery ORD-7 is confirmed for 2024-04-10.")
        resp = json.dumps({
            "event_type": "delivery", "action": "commit", "business_key": "ORD-7",
            "confirmed_date": "2024-04-10", "proposed_date": None,
            "prior_confirmed_date": None, "confidence": 0.9,
            "supporting_passage": "Your delivery ORD-7 is confirmed for 2024-04-10",
        })
        out = extract_prepared(_prepare(sig), SOURCE, model_fn=lambda q: (resp, 10))
        assert out.status == ExtractionStatus.EXTRACTED
        assert out.result.actor_role == "business" and out.result.thread_id == "t1"

    def test_quoted_customer_text_in_business_reply_is_rejected(self) -> None:
        body = ("Thanks, checking.\n\nOn Mon, Alice <alice@x.com> wrote:\n"
                "> Please deliver on 2024-04-10.")
        sig = _content_signal(sent=True, to="alice@x.com", frm=OWNER, subject="Re", plain=body)
        resp = json.dumps({
            "event_type": "delivery", "action": "commit", "business_key": None,
            "confirmed_date": "2024-04-10", "proposed_date": None,
            "prior_confirmed_date": None, "confidence": 0.9,
            "supporting_passage": "Please deliver on 2024-04-10.",
        })
        out = extract_prepared(_prepare(sig), SOURCE, model_fn=lambda q: (resp, 10))
        assert out.status == ExtractionStatus.REJECTED


# ── Worker entry point over the real store ────────────────────────────────────

class TestStoredSignals:
    def _ingest(self, tmp_path: Path, signals: list) -> Path:
        from olo.adapters import FetchBatch
        from olo.runner import run_source

        class _Adapter:
            source = SOURCE
            _done = False

            def fetch(self, cursor, *, limit):
                if self._done:
                    return FetchBatch(signals=[], has_more=False, next_cursor=cursor)
                self._done = True
                return FetchBatch(signals=signals, has_more=False, next_cursor="c1")

        db = tmp_path / "olo.db"
        assert run_source(_Adapter(), db_path=db).status == "succeeded"
        return db

    def test_load_prepared_signal_reads_stored_blob(self, tmp_path: Path) -> None:
        sig = _content_signal(sent=False, to=OWNER, frm="alice@x.com",
                              subject="Order", plain=RECEIVED_BODY)
        db = self._ingest(tmp_path, [sig])
        conn = store_connect(db)
        p = load_prepared_signal(conn, SOURCE, sig.external_id)
        assert p.status == PREP_READY and p.actor_role == "customer"
        assert p == _prepare(sig)

    def test_no_cross_source_reads(self, tmp_path: Path) -> None:
        sig = _content_signal(sent=False, to=OWNER, frm="alice@x.com", subject="S", plain="B")
        db = self._ingest(tmp_path, [sig])
        conn = store_connect(db)
        assert load_prepared_signal(conn, "gmail_other", sig.external_id).status \
            == PREP_MISSING_CONTENT
