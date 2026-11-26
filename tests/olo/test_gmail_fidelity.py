"""Post-audit batch 2 — Gmail capture completeness and email meaning.

Capture: messages first filed in SPAM / TRASH / as drafts are captured when
moved into scope; deletions and label moves become mailbox state, ordered by
history id, without erasing evidence or cancelling commitments.

Meaning: realistic Gmail API payloads (tests/olo/gmail_fixtures.py) render
to the text a person reads — plain alternative preferred, charsets honoured,
HTML stripped of head/style/script, attachments and attached messages left
out — and authored text excludes Gmail / Outlook quoted history without
truncating a legitimate line that begins with "From:".  The extractor and
the Hindsight projection consume the same authored text.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from olo.adapters.gmail import GmailAdapter
from olo.adapters.gmail_full import build_email_content_signal
from olo.message_state import apply_signals, state_of
from olo.signal_content import authored_text, prepare_signal
from olo.signals import NormalizedSignal
from olo.store import connect, migrate
from tests.olo import gmail_fixtures as gf
from tests.olo.fake_google import FakeGoogleServer
from tests.olo.test_s5_pipeline import content

pytestmark = pytest.mark.not_integration

ALICE = "Alice Customer <alice@customer.test>"
OWNER = "Owner <owner@biz.test>"


def _prepare(raw: dict):
    """(signal, prepared) exactly as the pipeline stores and reads them."""
    sig = build_email_content_signal("gmail_biz", raw, "1")
    blobs = {b.sha256: b.data for b in sig.blobs}
    return sig, prepare_signal("email.content", sig.payload, subject=sig.subject,
                               occurred_at=sig.occurred_at, blob_reader=blobs.get)


def _msg(payload: dict, *, labels=("INBOX",), msg_id="m1", frm=ALICE, to=OWNER, subject="ORD-501"):
    return gf.raw_message(msg_id, gf.with_headers(payload, gf.message_headers(
        frm=frm, to=to, subject=subject, msg_id=msg_id)), labels=list(labels))


# ── MIME, charset, HTML ───────────────────────────────────────────────────────

class TestRendering:
    def test_alternative_prefers_the_plain_part(self) -> None:
        _, p = _prepare(_msg(gf.alternative("Delivery confirmed for 2026-06-10.",
                                            "<p>HTML ONLY <b>2026-06-10</b></p>")))
        assert "Delivery confirmed for 2026-06-10." in p.full_text and "HTML ONLY" not in p.full_text

    def test_html_only_mail_loses_head_style_script_and_comments(self) -> None:
        html = ("<html><head><title>T</title><style>p{color:red}</style></head><body>"
                "<script>alert(1)</script><!-- tracking --><p>Your delivery is&nbsp;confirmed "
                "for 2026-06-10 &amp; paid.</p><div>Thanks</div></body></html>")
        _, p = _prepare(_msg(gf.text_part(html, mime="text/html")))
        assert "color" not in p.full_text and "alert" not in p.full_text and "tracking" not in p.full_text
        assert "Your delivery is confirmed for 2026-06-10 & paid." in p.full_text
        assert "\nThanks" in p.full_text

    @pytest.mark.parametrize("charset,text", [
        ("ISO-8859-1", "Café: livraison confirmée le 2026-06-10."),
        ("windows-1252", "Total €120 — delivery on 2026-06-10."),
        ("UTF-8", "Lieferung am 2026-06-10 bestätigt ✓"),
    ])
    def test_declared_charset_is_decoded(self, charset, text) -> None:
        _, p = _prepare(_msg(gf.text_part(text, charset=charset)))
        assert text in p.full_text and "�" not in p.full_text

    def test_attachments_and_attached_messages_are_not_body_text(self) -> None:
        forwarded = {"partId": "2", "mimeType": "message/rfc822", "filename": "",
                     "headers": [{"name": "Content-Type", "value": "message/rfc822"}],
                     "body": {"size": 0},
                     "parts": [gf.text_part("Old: delivery confirmed for 2026-05-01.", part_id="2.0")]}
        payload = gf.multipart("multipart/mixed", [
            gf.alternative("Please see attached.", "<p>Please see attached.</p>"),
            gf.attachment_part("notes.txt", inline_text="Internal: ship 2026-07-01"),
            forwarded])
        _, p = _prepare(_msg(payload))
        assert "Please see attached." in p.full_text
        assert "2026-07-01" not in p.full_text and "2026-05-01" not in p.full_text


# ── Authored text vs quoted history ───────────────────────────────────────────

class TestAuthoredText:
    def test_a_legitimate_from_line_is_not_quoted_history(self) -> None:
        body = ("Hi Alice,\nFrom: Monday the warehouse is closed, so\n"
                "your ORD-9 delivery is confirmed for 2026-07-02.\nOwner")
        assert "confirmed for 2026-07-02" in authored_text("email.content", body)

    def test_outlook_header_block_starts_quoted_history(self) -> None:
        body = ("Could we move ORD-501 to 2026-06-15?\n\n________________________________\n"
                "From: Owner <owner@biz.test>\nSent: Monday, June 1, 2026 9:00 AM\n"
                "To: Alice Customer\nSubject: ORD-501\n\nYour delivery is confirmed for 2026-06-10.")
        own = authored_text("email.content", body)
        assert own == "Could we move ORD-501 to 2026-06-15?"

    def test_outlook_header_block_without_separator(self) -> None:
        body = ("Confirmed.\n\nFrom: Owner <owner@biz.test>\nDate: Mon, 1 Jun 2026\n"
                "Subject: ORD-501\nYour delivery is confirmed for 2026-06-10.")
        assert authored_text("email.content", body) == "Confirmed."

    def test_gmail_wrapped_attribution(self) -> None:
        body = ("Could we move it to 2026-06-15?\n\nOn Mon, Jun 1, 2026 at 9:00 AM Owner "
                "<owner@biz.test>\nwrote:\n\n> Your delivery is confirmed for 2026-06-10.")
        assert authored_text("email.content", body) == "Could we move it to 2026-06-15?"

    def test_forward_markers(self) -> None:
        for marker in ("---------- Forwarded message ---------", "-----Original Message-----",
                       "Begin forwarded message:"):
            body = f"FYI\n\n{marker}\nFrom: X <x@y>\nDelivery on 2026-06-10"
            assert authored_text("email.content", body) == "FYI"

    def test_gmail_html_reply_quote_is_not_authored(self) -> None:
        html = ('<div dir="ltr">Could we move ORD-501 to 2026-06-15?</div><br>'
                '<div class="gmail_quote"><div dir="ltr" class="gmail_attr">On Mon, Jun 1, 2026 '
                'Owner wrote:<br></div><blockquote class="gmail_quote">Your delivery is confirmed '
                'for 2026-06-10.</blockquote></div>')
        _, p = _prepare(_msg(gf.text_part(html, mime="text/html")))
        assert "2026-06-10" in p.full_text                      # the original is kept…
        assert "2026-06-10" not in p.authored_text and "2026-06-15" in p.authored_text
        assert p.quoted_omitted

    def test_outlook_html_reply_quote_is_not_authored(self) -> None:
        html = ('<div>Confirmed, thanks.</div><hr><div id="divRplyFwdMsg"><b>From:</b> Owner<br>'
                '<b>Sent:</b> Monday<br><b>To:</b> Alice<br></div><div>Your delivery is confirmed '
                'for 2026-06-10.</div>')
        _, p = _prepare(_msg(gf.text_part(html, mime="text/html")))
        assert p.authored_text.endswith("Confirmed, thanks.") and "2026-06-10" not in p.authored_text


# ── Evidence, checksums, one representation ───────────────────────────────────

class TestEvidence:
    def test_full_original_is_preserved_and_checksummed(self) -> None:
        raw = _msg(gf.alternative("Could we move it?\n\nOn Mon, 1 Jun 2026, Owner wrote:\n> Old date",
                                  "<p>x</p>"))
        sig, p = _prepare(raw)
        [blob] = sig.blobs
        assert hashlib.sha256(blob.data).hexdigest() == sig.payload["content_refs"][0]["sha256"]
        doc = json.loads(blob.data)
        assert doc["payload"]["parts"][0]["body"]["data"] == raw["payload"]["parts"][0]["body"]["data"]
        assert "> Old date" in p.full_text and "Old date" not in p.authored_text
        assert p.authored_sha256 == hashlib.sha256(p.authored_text.encode()).hexdigest()

    def test_extraction_and_projection_use_the_same_authored_text(self, tmp_path) -> None:
        from tests.olo.test_s5_pipeline import Pipeline, answer
        reply = ("Could we move ORD-1 from 2024-04-10 to 2024-04-15?\n\n"
                 "On Mon, 1 Apr 2024, Owner wrote:\n> Your ORD-1 is confirmed for 2024-04-10.")
        routes = {"Could we move ORD-1": answer("request_change",
                                               "Could we move ORD-1 from 2024-04-10 to 2024-04-15",
                                               key="ORD-1", proposed="2024-04-15", prior="2024-04-10")}
        p = Pipeline(tmp_path, routes=routes)
        raw = gf.raw_message("m2", gf.with_headers(gf.alternative(reply, "<p>x</p>"),
                                                   gf.message_headers(frm=ALICE, to=OWNER,
                                                                      subject="Re: ORD-1", msg_id="m2")),
                             labels=["INBOX"], internal_ms="1712131200000")
        p.run(build_email_content_signal("gmail_biz", raw, "1"))
        doc = p.hindsight.docs(p.bank)["sig:c:m2"]
        conn = connect(p.db)
        from olo.signal_content import load_prepared_signal
        prepared = load_prepared_signal(conn, "gmail_biz", "c:m2")
        conn.close()
        assert doc["content"] == prepared.authored_text
        assert doc["metadata"]["authored_sha256"] == prepared.authored_sha256
        assert "> Your ORD-1" not in doc["content"]


# ── Gmail capture: rescue and label moves ─────────────────────────────────────

def _adapter(fake: FakeGoogleServer) -> GmailAdapter:
    from olo.google.http import GoogleClient
    client = GoogleClient(fake.make_credentials("test", gmail_readonly=True))
    client._access_token = fake.tokens.access_token
    return GmailAdapter({"account": "test"}, client=client)


def _kinds(batch) -> list[tuple[str, str]]:
    return [(s.kind, s.external_id) for s in batch.signals]


class TestRescue:
    @pytest.mark.parametrize("arrived,move_add,move_remove", [
        (["SPAM"], ["INBOX"], ["SPAM"]),                        # "Not spam"
        (["TRASH"], ["INBOX"], ["TRASH"]),                      # restored from trash
        (["DRAFT"], ["SENT"], ["DRAFT"]),                       # draft sent
    ])
    def test_message_out_of_a_skipped_label_is_captured_with_full_content(
            self, arrived, move_add, move_remove) -> None:
        with FakeGoogleServer() as fake:
            a = _adapter(fake)
            cursor = a.fetch(None, limit=100).next_cursor
            fake.gmail.add_message("m1", labels=list(arrived), subject="ORD-501 confirmed",
                                   from_="alice@customer.test", to="owner@biz.test")
            b1 = a.fetch(cursor, limit=100)
            assert [k for k, _ in _kinds(b1)] == []              # skipped on arrival
            fake.gmail.move_labels("m1", add=move_add, remove=move_remove)
            b2 = a.fetch(b1.next_cursor, limit=100)
            kinds = dict((ext, k) for k, ext in _kinds(b2))
            want = "email.sent" if "SENT" in move_add else "email.received"
            assert kinds.get("m1") == want and kinds.get("c:m1") == "email.content"

    def test_chat_stays_excluded(self) -> None:
        with FakeGoogleServer() as fake:
            a = _adapter(fake)
            cursor = a.fetch(None, limit=100).next_cursor
            fake.gmail.add_message("c1", labels=["CHAT"])
            fake.gmail.move_labels("c1", add=["INBOX"], remove=[])
            b = a.fetch(cursor, limit=100)
            assert not any(s.external_id in ("c1", "c:c1") for s in b.signals)

    def test_read_and_star_changes_do_not_refetch(self) -> None:
        with FakeGoogleServer() as fake:
            a = _adapter(fake)
            cursor = a.fetch(None, limit=100).next_cursor
            fake.gmail.add_message("m1", labels=["INBOX", "UNREAD"])
            b1 = a.fetch(cursor, limit=100)
            fake.gmail.remove_labels("m1", ["UNREAD"])
            fake.gmail.add_labels("m1", ["STARRED"])
            b2 = a.fetch(b1.next_cursor, limit=100)
            assert {k for k, _ in _kinds(b2)} == {"email.labels_removed", "email.labels_added"}


# ── Mailbox state policy ──────────────────────────────────────────────────────

def _sig(kind: str, ext: str, **payload) -> NormalizedSignal:
    return NormalizedSignal(source="gmail_biz", external_id=ext, kind=kind,
                            occurred_at=datetime(2026, 6, 1, tzinfo=UTC), subject=None,
                            payload={"message_id": "m1", **payload})


class TestMailboxState:
    def _conn(self, tmp_path: Path):
        conn = connect(tmp_path / "olo.db")
        migrate(conn)
        return conn

    def test_deletion_and_moves_are_recorded_and_never_rolled_back(self, tmp_path) -> None:
        conn = self._conn(tmp_path)
        apply_signals(conn, "gmail_biz", [_sig("email.received", "m1", label_ids=["INBOX"],
                                               history_id="10")], "t")
        assert state_of(conn, "gmail_biz", "m1") == "active"
        apply_signals(conn, "gmail_biz", [_sig("email.deleted", "h12:m1:deleted", history_id="12")], "t")
        apply_signals(conn, "gmail_biz", [_sig("email.labels_added", "h11:m1:added",
                                               current_label_ids=["INBOX", "SPAM"], history_id="11")], "t")
        apply_signals(conn, "gmail_biz", [_sig("email.content", "c:m1", label_ids=["INBOX"])], "t")
        assert state_of(conn, "gmail_biz", "m1") == "deleted"   # older / unordered never win

    def test_spam_then_restore(self, tmp_path) -> None:
        conn = self._conn(tmp_path)
        apply_signals(conn, "gmail_biz", [
            _sig("email.labels_added", "h5:m1:added", current_label_ids=["SPAM"], history_id="5"),
            _sig("email.labels_removed", "h6:m1:removed", current_label_ids=["INBOX"], history_id="6"),
        ], "t")
        assert state_of(conn, "gmail_biz", "m1") == "active"

    def test_deleted_confirmation_keeps_the_ledger_and_is_marked_downstream(self, tmp_path) -> None:
        from olo.current_state import customer_state, label_memories
        from tests.olo.test_s5_pipeline import COMMIT_BODY, Pipeline, content
        p = Pipeline(tmp_path)
        p.run(content("m1", sent=True, body=COMMIT_BODY, day=1))
        [cid] = p.commitments()
        p.run(NormalizedSignal(source="gmail_biz", external_id="h9:m1:deleted", kind="email.deleted",
                               occurred_at=datetime(2024, 3, 5, tzinfo=UTC), subject=None,
                               payload={"message_id": "m1", "thread_id": "t1", "history_id": "9"}))
        assert p.state(cid)["scheduled_date"] == "2024-04-10"     # not cancelled
        conn = connect(p.db)
        contact = conn.execute("SELECT contact_id FROM commitments").fetchone()[0]
        [s] = customer_state(conn, contact)
        from olo.current_state import signal_links, signal_messages
        from olo.message_state import message_states
        [m], _ = label_memories([{"type": "world", "text": "ORD-1 confirmed for 2024-04-10",
                                  "document_id": "sig:c:m1", "tags": [f"ent:{contact}"]}],
                                [s], f"ent:{contact}", links=signal_links(conn),
                                messages=signal_messages(conn), mailbox=message_states(conn))
        conn.close()
        assert s.history[0]["evidence_state"] == "deleted" and s.current_date == "2024-04-10"
        assert m.source_state == "deleted" and "not a retraction" in m.notes[-1]
        assert m.label == "consistent"

    def test_metadata_copy_stops_waiting_when_the_message_was_deleted(self, tmp_path) -> None:
        from tests.olo.test_s5_pipeline import Pipeline, metadata
        p = Pipeline(tmp_path)
        p.run(metadata("m1", sent=True, body="x", day=1),
              content("m0", sent=True, body="Note: thanks.", day=1))   # content enabled
        assert p.resolution()["m1"]["commitment_status"] == "pending"
        p.run(NormalizedSignal(source="gmail_biz", external_id="h9:m1:deleted", kind="email.deleted",
                               occurred_at=datetime(2024, 3, 5, tzinfo=UTC), subject=None,
                               payload={"message_id": "m1", "thread_id": "t1", "history_id": "9"}))
        r = p.resolution()["m1"]
        assert (r["commitment_status"], r["commitment_outcome"]) == ("skipped", "content_unavailable")

    def test_mailbox_state_survives_a_crash_between_ingest_and_tracking(self, tmp_path) -> None:
        from tests.olo.test_s5_pipeline import Pipeline

        class Crash(Exception):
            pass

        def fault(point):
            if point == "after_ingest":
                raise Crash()

        p = Pipeline(tmp_path)
        deleted = NormalizedSignal(source="gmail_biz", external_id="h9:m7:deleted", kind="email.deleted",
                                   occurred_at=datetime(2024, 3, 5, tzinfo=UTC), subject=None,
                                   payload={"message_id": "m7", "thread_id": "t7", "history_id": "9"})
        with pytest.raises(Crash):
            p.run(deleted, _fault=fault)
        p.run()                                                   # recovery
        conn = connect(p.db)
        assert state_of(conn, "gmail_biz", "m7") == "deleted"
        conn.close()

