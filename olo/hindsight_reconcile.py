"""olo/hindsight_reconcile.py — make Hindsight hold what olo.db says it should.

olo.db is the authority; a Hindsight bank is a projection of it that can
silently fall behind:

* **bank loss** — the bank (or the whole server state) is gone; the
  projection stage's bank preparation recreates it empty;
* **restart** — operations in flight are forgotten (the stage already
  records them 'lost' and resubmits);
* **partial retain** — an operation completes but some documents never
  appear;
* **cancellation** — Hindsight cancels cooperatively, so a cancelled retain
  may have written some documents after all;
* **drift** — a document the server holds no longer carries what Olo sent
  for it (metadata or tags), e.g. an older write landing last.

``reconcile_bank`` lists what the bank holds and compares it with the
projection outbox (``hindsight_projection``) and the documents olo.db wants
(``desired_documents`` over every source).  It never writes to Hindsight and
never deletes anything: it only corrects the outbox, so the normal
projection stage — with its batching, caps and usage guard — resubmits what
is missing or drifted, in place under the same document id.  Historical
documents (ledger versions, signals) keep their ids, so recovery never
duplicates them.

  ===========================  ==============  ================================
  server                       outbox          becomes
  ===========================  ==============  ================================
  absent                       confirmed       failed (reported as missing)
  present, differs             confirmed       failed (reported as drifted)
  present, matches             failed          reported only (present_unconfirmed)
  present, not desired         any / none      reported only (unexpected)
  ===========================  ==============  ================================

'failed' only means "not confirmed": the stage resubmits it.  Retry
budgets count failed *operations*, so a reconciliation never uses one up.
A document a cancelled or failed retain left on the server is resubmitted,
not confirmed: Hindsight writes the document row before its extraction
finishes, so its presence does not prove it was processed.  Resubmission
replaces it in place under the same id.

Bank isolation holds: only the business's bound bank is read.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = ["ReconcileReport", "mark_bank_lost", "reconcile_bank", "server_documents"]

_DDL = """\
CREATE TABLE IF NOT EXISTS hindsight_reconciliations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_id     TEXT NOT NULL,
    at          TEXT NOT NULL,
    trigger     TEXT NOT NULL,           -- scheduled | bank_recreated | restart | operator
    status      TEXT NOT NULL,           -- ok | recovered | bank_missing | failed
    report      TEXT NOT NULL            -- JSON ReconcileReport
);
"""


@dataclass
class ReconcileReport:
    bank_missing: bool = False
    server_documents: int = 0
    confirmed: int = 0
    missing: list[str] = field(default_factory=list)
    drifted: list[str] = field(default_factory=list)
    present_unconfirmed: list[str] = field(default_factory=list)  # held, resubmitted anyway
    retired: list[str] = field(default_factory=list)     # confirmed, no longer desired, absent
    unexpected: list[str] = field(default_factory=list)   # on the server, not desired

    @property
    def status(self) -> str:
        if self.bank_missing:
            return "bank_missing"
        return "recovered" if (self.missing or self.drifted) else "ok"

    def summary(self) -> dict[str, Any]:
        return {"status": self.status, "server_documents": self.server_documents,
                "confirmed": self.confirmed, "missing": len(self.missing),
                "drifted": len(self.drifted), "present_unconfirmed": len(self.present_unconfirmed),
                "unexpected": len(self.unexpected), "retired": len(self.retired)}


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def server_documents(client: Any, bank_id: str, page_size: int = 200) -> dict[str, dict] | None:
    """Every document the bank holds (id → listing row); None if no bank."""
    from olo.m3.hindsight.client import HindsightAPIError
    out: dict[str, dict] = {}
    offset = 0
    while True:
        try:
            page = client.list_documents(limit=page_size, offset=offset, bank_id=bank_id) or {}
        except HindsightAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        items = page.get("items") or []
        for item in items:
            out[str(item.get("id"))] = item
        if len(items) < page_size:
            return out
        offset += page_size


def _matches(server: dict, wanted: dict) -> bool:
    """The server's copy carries what Olo sent.

    Hindsight 1.2.1's document listing always carries the document's tags
    (they include ``ledger:current`` / ``ledger:superseded``, so a ledger
    document left behind by a newer version is caught); its metadata is
    compared when the server reports any.  Content changes are the outbox
    fingerprint's job: a changed document is resubmitted by the stage.
    """
    if set(server.get("tags") or []) != set(wanted.get("tags") or []):
        return False
    got_meta = server.get("document_metadata") or {}
    if not got_meta:
        return True
    want_meta = wanted.get("metadata") or {}
    return {k: str(v) for k, v in got_meta.items()} == {k: str(v) for k, v in want_meta.items()}


def _desired(conn: sqlite3.Connection, await_commitment: bool) -> dict[str, tuple[dict, str]]:
    from olo.projection import _fingerprint, desired_documents
    out: dict[str, tuple[dict, str]] = {}
    for (source,) in conn.execute("SELECT DISTINCT source FROM signals ORDER BY source"):
        for item in desired_documents(conn, source, await_commitment=await_commitment):
            out[item["document_id"]] = (item, _fingerprint(item))
    return out


def mark_bank_lost(conn: sqlite3.Connection, bank_id: str, now: str, trigger: str, *,
                   await_commitment: bool = True) -> int:
    """The bank was lost (recreated empty): every confirmed document olo.db
    still wants is missing.  Returns how many; the stage resubmits them."""
    migrate(conn)
    wanted = _desired(conn, await_commitment)
    conn.execute("BEGIN IMMEDIATE")
    try:
        docs = [r[0] for r in conn.execute(
            "SELECT signal_id FROM hindsight_projection WHERE bank_id=? AND status='confirmed'",
            (bank_id,)) if r[0] in wanted]
        for doc in docs:
            _unconfirm(conn, bank_id, doc)
        n = len(docs)
        report = ReconcileReport(bank_missing=True)
        conn.execute(
            "INSERT INTO hindsight_reconciliations (bank_id, at, trigger, status, report)"
            " VALUES (?,?,?,?,?)",
            (bank_id, now, trigger, "bank_missing",
             json.dumps({**report.summary(), "marked_missing": n})))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return n


def reconcile_bank(conn: sqlite3.Connection, client: Any, bank_id: str, now: str, *,
                   trigger: str = "scheduled", await_commitment: bool = True) -> ReconcileReport:
    """Compare the bank with olo.db and correct the outbox (see module doc)."""
    migrate(conn)
    report = ReconcileReport()
    try:
        held = server_documents(client, bank_id)
    except Exception as exc:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO hindsight_reconciliations (bank_id, at, trigger, status, report)"
            " VALUES (?,?,?,?,?)", (bank_id, now, trigger, "failed",
                                    json.dumps({"error": f"{type(exc).__name__}: {exc}"[:300]})))
        conn.execute("COMMIT")
        raise
    if held is None:
        report.bank_missing = True
        held = {}
    report.server_documents = len(held)
    desired = _desired(conn, await_commitment)
    outbox = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT signal_id, content_sha256, status FROM hindsight_projection WHERE bank_id=?",
        (bank_id,))}
    submitted = {r[0]: r[1] for r in conn.execute(
        "SELECT p.signal_id, o.submitted_at FROM hindsight_projection p"
        " JOIN hindsight_operations o ON o.operation_id = p.operation_id"
        " WHERE p.bank_id=?", (bank_id,))}
    in_flight = {r[0] for r in conn.execute(
        "SELECT d.document_id FROM hindsight_operation_docs d JOIN hindsight_operations o"
        " ON o.operation_id = d.operation_id WHERE o.bank_id=? AND o.status='submitted'",
        (bank_id,))}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for doc, (fp, status) in sorted(outbox.items()):
            if doc in in_flight:
                continue                      # its operation decides
            server = held.get(doc)
            wanted = desired.get(doc)
            if status == "confirmed":
                if server is None and wanted is None:
                    report.retired.append(doc)    # no longer projected: nothing to redo
                elif server is None:
                    report.missing.append(doc)
                    _unconfirm(conn, bank_id, doc)
                elif wanted is not None and not _matches(server, wanted[0]):
                    report.drifted.append(doc)
                    _unconfirm(conn, bank_id, doc)
                else:
                    report.confirmed += 1
            elif server is not None and _written_since(server, submitted.get(doc)):
                report.present_unconfirmed.append(doc)
        report.unexpected = sorted(d for d in held if d not in desired and d not in outbox)
        conn.execute(
            "INSERT INTO hindsight_reconciliations (bank_id, at, trigger, status, report)"
            " VALUES (?,?,?,?,?)",
            (bank_id, now, trigger, report.status,
             json.dumps({**report.summary(), "missing_ids": report.missing[:50],
                         "drifted_ids": report.drifted[:50],
                         "unexpected_ids": report.unexpected[:50]})))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return report


def _written_since(server: dict, submitted_at: str | None) -> bool:
    """The server wrote this document at or after its latest submission, so
    the copy it holds is that submission's, not an older version."""
    from olo.ledger import utc_instant
    written = utc_instant(str(server.get("updated_at") or "").replace("Z", "+00:00"))
    sent = utc_instant(str(submitted_at or "").replace("Z", "+00:00"))
    return written is not None and sent is not None and written >= sent


def _unconfirm(conn: sqlite3.Connection, bank_id: str, doc: str) -> None:
    conn.execute("UPDATE hindsight_projection SET status='failed', confirmed_at=NULL"
                 " WHERE signal_id=? AND bank_id=?", (doc, bank_id))


def last_reconciliation(conn: sqlite3.Connection, bank_id: str) -> dict | None:
    migrate(conn)
    row = conn.execute(
        "SELECT at, trigger, status, report FROM hindsight_reconciliations WHERE bank_id=?"
        " ORDER BY id DESC LIMIT 1", (bank_id,)).fetchone()
    return None if row is None else {"at": row[0], "trigger": row[1], "status": row[2],
                                     **json.loads(row[3])}


def report_dict(report: ReconcileReport) -> dict:
    return asdict(report)
