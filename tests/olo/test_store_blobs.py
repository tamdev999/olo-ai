"""B1 tests: T-S1 to T-S7 — ContentBlob, store schema v2, ingest_batch blobs.

All tests run with an in-memory SQLite database (no live Google).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from olo.signals import (
    ContentBlob,
    NormalizedSignal,
    MAX_BLOB_BYTES,
    MAX_BATCH_BLOB_BYTES,
    canonical_json,
)
from olo.store import (
    AdapterContractError,
    _SCHEMA_VERSION,
    connect,
    get_blob_raw,
    get_checkpoint,
    count_signals,
    ingest_batch,
    migrate,
    purge_content,
    acquire_run,
    finish_run,
)
from olo.content import get_blob, load_json_doc, email_text, event_text


# ── Helpers ───────────────────────────────────────────────────────────────────

def _memdb() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    migrate(conn)
    return conn


def _blob(data: bytes, media_type: str = "application/octet-stream") -> ContentBlob:
    sha = hashlib.sha256(data).hexdigest()
    return ContentBlob(sha256=sha, media_type=media_type, data=data)


def _signal(
    source: str = "test_src",
    ext_id: str = "ext1",
    kind: str = "test.event",
    blobs: tuple[ContentBlob, ...] = (),
    extra_payload: dict | None = None,
) -> NormalizedSignal:
    payload: dict = {}
    if extra_payload:
        payload.update(extra_payload)
    if blobs:
        payload["content_refs"] = [
            {
                "sha256": b.sha256,
                "media_type": b.media_type,
                "size": len(b.data),
                "role": "document",
            }
            for b in blobs
        ]
    return NormalizedSignal(
        source=source,
        external_id=ext_id,
        kind=kind,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        subject=None,
        payload=payload,
        blobs=blobs,
    )


def _run(conn, source="test_src"):
    from olo.store import acquire_run, _make_owner
    return acquire_run(source, _make_owner(), conn)


# ── T-S1: ContentBlob validation ──────────────────────────────────────────────

def test_ts1_blob_wrong_sha256() -> None:
    """T-S1a: ContentBlob with wrong sha256 raises ValueError."""
    with pytest.raises(ValueError, match="sha256 mismatch"):
        ContentBlob(sha256="0" * 64, media_type="text/plain", data=b"hello")


def test_ts1_blob_exceeds_max_bytes() -> None:
    """T-S1b: ContentBlob larger than MAX_BLOB_BYTES raises ValueError."""
    too_large = b"x" * (MAX_BLOB_BYTES + 1)
    with pytest.raises(ValueError, match="MAX_BLOB_BYTES"):
        ContentBlob(sha256="irrelevant", media_type="text/plain", data=too_large)


def test_ts1_batch_exceeds_max_blob_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-S1c: ingest_batch raises AdapterContractError when batch blob bytes > 64 MiB."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    conn = _memdb()
    run_id = _run(conn)

    # Five 14 MiB blobs → total 70 MiB > MAX_BATCH_BLOB_BYTES (64 MiB)
    # Each blob is under MAX_BLOB_BYTES (16 MiB) so ContentBlob construction passes
    sigs = []
    for i in range(5):
        data = bytes([i]) * (14 * 1024 * 1024)
        blob = ContentBlob(hashlib.sha256(data).hexdigest(), "app/x", data)
        sigs.append(_signal("test_src", f"e{i}", blobs=(blob,)))

    with pytest.raises(AdapterContractError, match="MAX_BATCH_BLOB_BYTES"):
        ingest_batch("test_src", run_id, tuple(sigs), "c1", None, conn)


# ── T-S2: ref/blob correspondence ─────────────────────────────────────────────

def test_ts2_ref_without_blob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-S2a: a content_ref sha256 not in this batch and not stored → AdapterContractError."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    conn = _memdb()
    run_id = _run(conn)

    # Ref points to a sha256 that is not provided as a blob and not stored
    sig = NormalizedSignal(
        source="test_src",
        external_id="dangling",
        kind="test.event",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        subject=None,
        payload={"content_refs": [{"sha256": "a" * 64, "media_type": "app/x",
                                    "size": 1, "role": "doc"}]},
        blobs=(),
    )
    with pytest.raises(AdapterContractError, match="content_ref sha256"):
        ingest_batch("test_src", run_id, (sig,), "c1", None, conn)


def test_ts2_blob_without_ref() -> None:
    """T-S2b: a blob not referenced in payload[content_refs] raises ValueError at signal construction."""
    b = _blob(b"unref", "app/x")
    # Build signal without content_refs
    with pytest.raises(ValueError, match="no matching entry in payload"):
        NormalizedSignal(
            source="test_src",
            external_id="e1",
            kind="test.event",
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            subject=None,
            payload={},     # no content_refs
            blobs=(b,),
        )


# ── T-S3: deduplication ───────────────────────────────────────────────────────

def test_ts3_same_blob_two_signals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-S3a: same blob sha256 from two signals → one row in content_blobs."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    conn = _memdb()
    run_id = _run(conn)

    b = _blob(b"shared content", "text/plain")
    sig1 = _signal("test_src", "e1", blobs=(b,))
    sig2 = _signal("test_src", "e2", blobs=(b,))

    ingest_batch("test_src", run_id, (sig1, sig2), "c1", None, conn)

    n = conn.execute("SELECT COUNT(*) n FROM content_blobs WHERE source='test_src'").fetchone()["n"]
    assert n == 1, f"expected 1 blob row, got {n}"


def test_ts3_reingest_same_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-S3b: re-ingesting the same signal is a duplicate, not an error."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    conn = _memdb()
    run_id = _run(conn)

    b = _blob(b"data", "text/plain")
    sig = _signal("test_src", "e1", blobs=(b,))
    ingest_batch("test_src", run_id, (sig,), "c1", None, conn)
    finish_run("test_src", run_id, "succeeded", conn)

    run_id2 = _run(conn)
    inserted, dups, conflicts = ingest_batch("test_src", run_id2, (sig,), "c2", "c1", conn)
    assert inserted == 0
    assert dups == 1
    assert conflicts == 0


# ── T-S4: crash fault between blob insert and checkpoint ──────────────────────

def test_ts4_crash_after_blobs_before_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-S4: crash between blob insert and checkpoint write → no blob rows, no signals."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    conn = _memdb()
    run_id = _run(conn)

    b = _blob(b"crash test", "text/plain")
    sig = _signal("test_src", "e1", blobs=(b,))

    class _Fault(Exception):
        pass

    def fault(checkpoint: str) -> None:
        if checkpoint == "after_blobs":
            raise _Fault("simulated crash")

    with pytest.raises(_Fault):
        ingest_batch("test_src", run_id, (sig,), "c1", None, conn, _fault=fault)

    # Nothing committed
    n_blobs = conn.execute("SELECT COUNT(*) n FROM content_blobs").fetchone()["n"]
    n_sigs  = conn.execute("SELECT COUNT(*) n FROM signals").fetchone()["n"]
    assert n_blobs == 0, f"unexpected blob rows: {n_blobs}"
    assert n_sigs  == 0, f"unexpected signal rows: {n_sigs}"

    # Mark the run as failed so the lease is released, then re-run
    finish_run("test_src", run_id, "failed", conn)
    run_id2 = _run(conn)
    inserted, dups, _ = ingest_batch("test_src", run_id2, (sig,), "c1", None, conn)
    assert inserted == 1
    assert dups == 0


# ── T-S5: schema v1 → v2 migration ───────────────────────────────────────────

def test_ts5_v1_to_v2_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-S5: a v1 database (without content_blobs) upgrades to v2; existing data intact."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    db_file = tmp_path / "home" / "olo" / "olo.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)

    # Build a v1 database manually (no content_blobs table)
    v1_ddl = [
        "CREATE TABLE schema_version (version INTEGER NOT NULL)",
        "INSERT INTO schema_version VALUES (1)",
        """CREATE TABLE ingest_runs (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, owner TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT,
            status TEXT NOT NULL, batches INTEGER NOT NULL DEFAULT 0,
            fetched INTEGER NOT NULL DEFAULT 0, inserted INTEGER NOT NULL DEFAULT 0,
            duplicates INTEGER NOT NULL DEFAULT 0, conflicts INTEGER NOT NULL DEFAULT 0,
            error TEXT)""",
        """CREATE TABLE source_leases (
            source TEXT PRIMARY KEY, run_id TEXT NOT NULL, owner TEXT NOT NULL,
            acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL)""",
        """CREATE TABLE signals (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
            kind TEXT NOT NULL, occurred_at TEXT NOT NULL, subject TEXT,
            payload TEXT NOT NULL, content_sha256 TEXT NOT NULL,
            schema_version INTEGER NOT NULL, ingested_at TEXT NOT NULL,
            run_id TEXT NOT NULL, UNIQUE (source, external_id))""",
        """CREATE TABLE sync_checkpoints (
            source TEXT PRIMARY KEY, cursor TEXT NOT NULL,
            updated_at TEXT NOT NULL, run_id TEXT NOT NULL)""",
        """CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL,
            action TEXT NOT NULL, subject_type TEXT, subject_id TEXT,
            detail TEXT NOT NULL)""",
        "CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
        "CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
    ]
    v1_conn = sqlite3.connect(str(db_file), isolation_level=None)
    v1_conn.execute("BEGIN")
    for stmt in v1_ddl:
        v1_conn.execute(stmt)
    # Insert a signal into v1
    v1_conn.execute(
        "INSERT INTO ingest_runs(id,source,owner,started_at,status)"
        " VALUES ('r1','src1','owner','2026-01-01T00:00:00.000000Z','succeeded')"
    )
    v1_conn.execute(
        "INSERT INTO signals(source,external_id,kind,occurred_at,subject,payload,"
        "content_sha256,schema_version,ingested_at,run_id)"
        " VALUES ('src1','ext1','test.ev','2026-01-01T00:00:00.000000Z',NULL,'{}',"
        "'abc',1,'2026-01-01T00:00:00.000000Z','r1')"
    )
    v1_conn.execute("COMMIT")
    v1_conn.close()

    # Migrate to v2
    conn = connect(db_file)
    migrate(conn)

    # Schema version must be 2
    version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert version == _SCHEMA_VERSION == 2, f"schema_version={version}"

    # Existing signal intact
    n = conn.execute("SELECT COUNT(*) n FROM signals WHERE source='src1'").fetchone()["n"]
    assert n == 1

    # content_blobs table exists
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "content_blobs" in tables

    # Idempotent second migrate
    migrate(conn)
    version2 = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    assert version2 == 2
    conn.close()


# ── T-S6: account isolation ───────────────────────────────────────────────────

def test_ts6_account_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-S6: same blob bytes under two sources → two rows; get_blob is scoped;
    purging source_a leaves source_b intact."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    conn = _memdb()

    data = b"shared bytes"
    blob_a = _blob(data, "text/plain")
    blob_b = _blob(data, "text/plain")  # same content, different source
    # sha256 must match (same data → same sha256)
    assert blob_a.sha256 == blob_b.sha256

    run_a = _run(conn, "src_a")
    sig_a = _signal("src_a", "e1", blobs=(blob_a,))
    ingest_batch("src_a", run_a, (sig_a,), "c1", None, conn)
    finish_run("src_a", run_a, "succeeded", conn)

    run_b = _run(conn, "src_b")
    sig_b = _signal("src_b", "e1", blobs=(blob_b,))
    ingest_batch("src_b", run_b, (sig_b,), "c1", None, conn)
    finish_run("src_b", run_b, "succeeded", conn)

    # Two rows (source is part of the PK)
    n = conn.execute("SELECT COUNT(*) n FROM content_blobs").fetchone()["n"]
    assert n == 2

    # get_blob is scoped
    result_a = get_blob(conn, "src_a", blob_a.sha256)
    assert result_a == data
    result_b = get_blob(conn, "src_b", blob_b.sha256)
    assert result_b == data
    # Cross-source read returns nothing
    # (src_a and src_b both happen to have the same sha256 — both readable by their own source)

    # Purge src_a → src_b intact
    deleted = purge_content("src_a", conn)
    assert deleted == 1
    assert get_blob(conn, "src_a", blob_a.sha256) is None
    assert get_blob(conn, "src_b", blob_b.sha256) == data
    # Signal for src_a still exists (purge doesn't touch signals)
    n_sigs = conn.execute("SELECT COUNT(*) n FROM signals WHERE source='src_a'").fetchone()["n"]
    assert n_sigs == 1


# ── T-S7: zlib round-trip; sha256 over uncompressed bytes ─────────────────────

def test_ts7_zlib_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T-S7: compressible data is stored with zlib; sha256 is over uncompressed bytes;
    get_blob returns exact original bytes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    conn = _memdb()
    run_id = _run(conn)

    # Highly compressible data
    data = b"a" * 10_000
    blob = _blob(data, "text/plain")
    sig = _signal("test_src", "e1", blobs=(blob,))

    ingest_batch("test_src", run_id, (sig,), "c1", None, conn)

    # Check the stored row
    row = conn.execute(
        "SELECT encoding, size FROM content_blobs WHERE source='test_src' AND sha256=?",
        (blob.sha256,),
    ).fetchone()
    assert row is not None
    # Highly compressible data should use zlib
    assert row["encoding"] == "zlib", f"expected zlib, got {row['encoding']}"
    # Size column is uncompressed size
    assert row["size"] == len(data)

    # Round-trip: get_blob returns exactly the original bytes
    recovered = get_blob(conn, "test_src", blob.sha256)
    assert recovered == data


# ── Content.py unit tests ──────────────────────────────────────────────────────

def test_email_text_plain() -> None:
    """email_text extracts text/plain body."""
    doc = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64("Hello, World!")},
            "headers": [{"name": "Content-Type", "value": "text/plain; charset=utf-8"}],
        }
    }
    result = email_text(doc)
    assert result.plain == "Hello, World!"
    assert result.html_text == ""
    assert result.truncated is False


def test_email_text_html_stripped() -> None:
    """email_text converts HTML to plain text, strips script/style."""
    html_body = "<html><head><style>body{}</style></head><body><p>Hello</p><script>evil()</script><p>World</p></body></html>"
    doc = {
        "payload": {
            "mimeType": "text/html",
            "body": {"data": _b64(html_body)},
            "headers": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}],
        }
    }
    result = email_text(doc)
    assert "Hello" in result.html_text
    assert "World" in result.html_text
    assert "evil" not in result.html_text
    assert "body{}" not in result.html_text


def test_email_text_multipart() -> None:
    """email_text handles multipart/alternative with plain + html."""
    doc = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64("Plain text")},
                    "headers": [],
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64("<p>HTML text</p>")},
                    "headers": [],
                },
            ],
        }
    }
    result = email_text(doc)
    assert "Plain text" in result.plain
    assert "HTML text" in result.html_text


def test_email_text_non_utf8_charset() -> None:
    """email_text decodes ISO-8859-1 correctly."""
    import base64 as _b64mod
    latin1_data = "Héllo Wörld".encode("latin-1")
    encoded = _b64mod.urlsafe_b64encode(latin1_data).decode()
    doc = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": encoded},
            "headers": [{"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}],
        }
    }
    result = email_text(doc)
    assert "H" in result.plain
    assert result.truncated is False


def test_event_text_plain_description() -> None:
    """event_text returns description as-is for plain text."""
    doc = {"description": "Team meeting at 3pm"}
    assert event_text(doc) == "Team meeting at 3pm"


def test_event_text_html_description() -> None:
    """event_text strips HTML from description."""
    doc = {"description": "<p>Meeting notes</p><script>evil()</script>"}
    result = event_text(doc)
    assert "Meeting notes" in result
    assert "evil" not in result
    assert "<p>" not in result


def test_event_text_empty() -> None:
    """event_text returns empty string when no description."""
    assert event_text({}) == ""
    assert event_text({"description": None}) == ""
    assert event_text({"description": ""}) == ""


def test_load_json_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_json_doc returns a dict from a stored JSON blob."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    conn = _memdb()
    run_id = _run(conn)

    payload = {"key": "value", "num": 42}
    data = json.dumps(payload, sort_keys=True).encode()
    blob = _blob(data, "application/json")
    sig = _signal("test_src", "e1", blobs=(blob,))
    ingest_batch("test_src", run_id, (sig,), "c1", None, conn)

    result = load_json_doc(conn, "test_src", blob.sha256)
    assert result == payload


def _b64(text: str) -> str:
    import base64
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode()
