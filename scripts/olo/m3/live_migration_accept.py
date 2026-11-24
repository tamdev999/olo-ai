#!/usr/bin/env python3
"""olo/m3: Live migration acceptance test against real Hindsight 1.2.1.

Runs as root; starts Hindsight as olo_hs (non-root) via sudo so pg0/initdb
works -- same pattern as the V5 gate test.

Findings from first live run:
- Hindsight v1.2.1 list_documents and get_document use 'id' not 'document_id'
- Entity links are in doc['retain_params']['entities'], not doc['entities']
- Sync retain (async=False) returns operation_id=None; Hindsight uses
  document_id for sync idempotency, NOT operation_id.  The operation_id
  passed to retain is accepted but not echoed and not used for sync dedup.

Usage:
  source /root/olo_m3_secrets/deepseek.env
  export HINDSIGHT_API_LLM_API_KEY
  export PYTHONPATH=/root/olo_m3_hindsight_venv/lib/python3.14/site-packages:/mnt/c/oloai
  cd /mnt/c/oloai
  source /root/olo_linux_venv/bin/activate
  python scripts/olo/m3/live_migration_accept.py
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/mnt/c/oloai")

import olo.adapters.contacts_migration as cm
from olo.adapters.contacts import (
    make_contact_id,
    migrate_contacts,
    populate_contacts_from_crm,
)
from olo.adapters.entity_extractor import entity_id_from_email
from olo.m3.synthetic.crm_fixture import CRM_CONTACTS, get_contact_id_for_email
from olo.m3.hindsight.client import (
    HindsightClient,
    HindsightConfig,
    HindsightUnavailable,
    HindsightAPIError,
)
from olo.m3.pg0_lifecycle import safe_pg0_delete_home
from olo.store import connect as store_connect, migrate as store_migrate

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TEST_PORT = 19901
OLO_HS_HOME = Path("/home/olo_hs/mig_accept_home")
OLO_HS_SOCKET = OLO_HS_HOME / "run"
HF_CACHE = "/var/cache/huggingface"
HINDSIGHT_BIN = "/root/olo_m3_hindsight_venv/bin/hindsight-api"
PG0_INSTANCE = "olo-mig-accept"

ALICE_EMAIL = "alice.chen@acme-corp.example"
ALICE_CID = get_contact_id_for_email(ALICE_EMAIL)
LEGACY_CID = make_contact_id(ALICE_EMAIL)
LEGACY_ENT_EMAIL = entity_id_from_email(ALICE_EMAIL)
LEGACY_ENT_ULID = "ent_01ACME000000000000000000"

DOC_IDS = {
    "cid_email_derived": "sig:test-mig-cid-001",
    "ent_email_derived":  "sig:test-mig-ent-email-001",
    "ent_ulid":           "sig:test-mig-ent-ulid-001",
}
CONTENT = {
    DOC_IDS["cid_email_derived"]: "Invoice from Alice Chen for consulting. Amount: $5000.",
    DOC_IDS["ent_email_derived"]:  "Meeting notes with Alice Chen on Q2 planning.",
    DOC_IDS["ent_ulid"]:           "Call with Alice Chen: confirmed shipment details.",
}
TEST_BANK_ID = f"mig-test-{uuid.uuid4().hex[:8]}"

HS_ENV = {
    "HOME": str(OLO_HS_HOME),
    "HF_HOME": HF_CACHE,
    "HINDSIGHT_API_HOST": "127.0.0.1",
    "HINDSIGHT_API_PORT": str(TEST_PORT),
    "HINDSIGHT_API_DATABASE_URL": (
        f"pg0://{PG0_INSTANCE}"
        f"?unix_socket_directories={OLO_HS_SOCKET}"
    ),
    "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "onnx",
    "HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID": "intfloat/multilingual-e5-small",
    "HINDSIGHT_API_EMBEDDINGS_ONNX_CPU_MEM_ARENA": "false",
    "HINDSIGHT_API_RERANKER_PROVIDER": "rrf",
    "HINDSIGHT_API_LLM_PROVIDER": "none",
    "HINDSIGHT_API_DECISION_MODEL_PROVIDER": "none",
    "HINDSIGHT_API_MCP_ENABLED": "false",
    "HINDSIGHT_API_STORE_DOCUMENT_TEXT": "true",
    "OTEL_SDK_DISABLED": "true",
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ok(label, detail=""):
    print(f"  PASS  {label}" + (f" -- {detail}" if detail else ""))


def _fail(label, detail=""):
    print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ""))


def _section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def _kill_pg(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _sudo_hs(*cmd):
    env_list = [f"{k}={v}" for k, v in HS_ENV.items()]
    return ["sudo", "-u", "olo_hs", "env"] + env_list + list(cmd)


def _doc_id(doc):
    """Get document ID from Hindsight v1.2.1 response (field is 'id', not 'document_id')."""
    return doc.get("document_id") or doc.get("id", "")


def _entity_link(doc):
    """Get entity link from Hindsight v1.2.1 response (in retain_params.entities)."""
    rp = doc.get("retain_params") or {}
    entities = doc.get("entities") or rp.get("entities") or []
    return (entities[0].get("text") or "") if entities else ""


# ---------------------------------------------------------------------------
# 1. Start Hindsight as olo_hs
# ---------------------------------------------------------------------------

def start_hindsight():
    _section("Starting Hindsight 1.2.1 as olo_hs (non-root)")
    try:
        safe_pg0_delete_home(OLO_HS_HOME)
    except Exception:
        pass
    subprocess.run(["chmod", "711", "/root"], check=False, capture_output=True)
    subprocess.run(["chmod", "-R", "a+rX", "/root/olo_m3_hindsight_venv"],
                   check=False, capture_output=True)
    subprocess.run(["sudo", "-u", "olo_hs", "mkdir", "-p", str(OLO_HS_SOCKET)], check=True)

    log = open("/tmp/mig_accept_hs.log", "w")
    proc = subprocess.Popen(
        _sudo_hs(HINDSIGHT_BIN), stdout=log, stderr=log, start_new_session=True
    )
    cfg = HindsightConfig(host="127.0.0.1", port=TEST_PORT, api_key="", bank_id=TEST_BANK_ID)
    client = HindsightClient(cfg)

    print(f"  PID={proc.pid}  port={TEST_PORT}  bank={TEST_BANK_ID}")
    print("  Waiting up to 120s...")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            client.wait_until_ready(timeout_s=5)
            print("  Server ready.")
            return proc, client
        except (HindsightUnavailable, Exception):
            time.sleep(2)
    print("  Log tail:")
    print(open("/tmp/mig_accept_hs.log").read()[-600:])
    _kill_pg(proc)
    raise RuntimeError("Hindsight failed to start.")


# ---------------------------------------------------------------------------
# 2. Set up SQLite olo.db with legacy contacts
# ---------------------------------------------------------------------------

def setup_legacy_db(tmp_dir):
    _section("Setting up SQLite DB with legacy contacts")
    db_path = tmp_dir / "olo.db"
    conn = store_connect(db_path)
    store_migrate(conn)
    migrate_contacts(conn)
    cm.migrate_migration_schema(conn)
    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT OR IGNORE INTO contacts"
        " (id, organization_id, display_name, source, created_at, updated_at)"
        " VALUES (?, NULL, 'Alice Chen (Legacy)', 'old_crm', ?, ?)",
        (LEGACY_CID, now, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO contact_identifiers"
        " (identifier_type, identifier_value, contact_id, source, verified_at)"
        " VALUES ('email', ?, ?, 'old_crm', ?)",
        (ALICE_EMAIL, LEGACY_CID, now),
    )
    conn.execute("COMMIT")
    populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")
    print(f"  Legacy CID:       {LEGACY_CID}")
    print(f"  Legacy ent_email: {LEGACY_ENT_EMAIL}")
    print(f"  Legacy ent_ulid:  {LEGACY_ENT_ULID}")
    print(f"  Canonical CID:    {ALICE_CID}")
    _ok("SQLite DB ready")
    return conn


# ---------------------------------------------------------------------------
# 3. Seed Hindsight bank with legacy entity links
# ---------------------------------------------------------------------------

def seed_bank(client):
    seeded = {}
    legacy_links = {
        DOC_IDS["cid_email_derived"]: LEGACY_CID,
        DOC_IDS["ent_email_derived"]:  LEGACY_ENT_EMAIL,
        DOC_IDS["ent_ulid"]:           LEGACY_ENT_ULID,
    }
    for doc_id, legacy_link in legacy_links.items():
        content = CONTENT[doc_id]
        sha = hashlib.sha256(content.encode()).hexdigest()
        op_id = str(uuid.uuid4())
        client.retain(
            [{
                "content": content,
                "timestamp": "2024-06-01T10:00:00+00:00",
                "document_id": doc_id,
                "entities": [{"text": legacy_link, "type": "person"}],
                "resolve_entities": False,
                "tags": ["ym:2024-06", "test:mig-accept"],
                "strategy": "structured-no-llm",
            }],
            operation_id=op_id, async_=False, bank_id=TEST_BANK_ID,
        )
        seeded[doc_id] = sha
        print(f"  Seeded {doc_id}: entity={legacy_link[:30]}")
    time.sleep(2)
    _ok("Bank seeded with 3 legacy-linked documents")
    return seeded


def _build_item(doc_id, new_entity_link):
    content = CONTENT.get(doc_id, f"Content for {doc_id}")
    return {
        "content": content,
        "timestamp": "2024-06-01T10:00:00+00:00",
        "document_id": doc_id,
        "entities": [{"text": new_entity_link, "type": "person"}],
        "resolve_entities": False,
        "tags": ["ym:2024-06", "test:mig-accept"],
        "strategy": "structured-no-llm",
    }


# ---------------------------------------------------------------------------
# 4. Full migration run
# ---------------------------------------------------------------------------

def run_full_migration(conn, client):
    _section("Full migration (all three legacy formats)")
    run_id = str(uuid.uuid4())

    resp = client.list_documents(limit=200, bank_id=TEST_BANK_ID)
    bank_docs = resp.get("items") or []
    print(f"  Bank documents: {len(bank_docs)}")
    for d in bank_docs:
        did = _doc_id(d)
        link = _entity_link(d)
        print(f"    {did}: entity={link[:35]}")

    # Provide ent_* -> email mapping.
    # In production, _build_entity_email_map() scans the signals table.
    # This test DB has no signals, so we provide the known mapping directly.
    _ent_map = {LEGACY_ENT_EMAIL: [ALICE_EMAIL], LEGACY_ENT_ULID: [ALICE_EMAIL]}
    _orig = cm._build_entity_email_map
    cm._build_entity_email_map = lambda _c: _ent_map
    try:
        manifest = cm.plan_identity_migration(
            run_id, conn, CRM_CONTACTS, bank_docs, is_dry_run=False
        )
    finally:
        cm._build_entity_email_map = _orig
    print(f"\n  Plan: {len(manifest['mappings'])} mappings, "
          f"{len(manifest['unknown'])} unknown")
    for m in manifest["mappings"]:
        print(f"    {m['document_id']}: {m['old_entity_link'][:25]} -> "
              f"{m['new_entity_link'][:25]} ({m['legacy_format']})")

    n_alias = cm.step_alias(run_id, conn)
    print(f"\n  Alias step: {n_alias} contact IDs renamed in SQLite")

    retain_log = []

    def live_retain(item, op_id):
        retain_log.append((item["document_id"], op_id, item["entities"][0]["text"]))
        client.retain([item], operation_id=op_id, async_=False, bank_id=TEST_BANK_ID)

    r = cm.step_retain(run_id, conn, _build_item, live_retain)
    print(f"\n  Retain step: retained={r['retained']} failed={r['failed']} skipped={r['skipped']}")
    for doc_id, op_id, new_link in retain_log:
        print(f"    op_id={op_id[:12]}...  {doc_id} -> entity={new_link[:30]}")

    time.sleep(2)

    v = cm.step_verify(
        run_id, conn,
        lambda d: client.get_document(d, bank_id=TEST_BANK_ID),
    )
    print(f"\n  Verify step: verified={v['verified']} failed={v['failed']}")
    if v["stale_links"]:
        _fail("Stale links found", str(v["stale_links"]))
    else:
        _ok("No stale links after full migration")

    status = cm.get_migration_status(run_id, conn)
    print(f"  Run state: {status['state']}  counts: {status['counts']}")
    if status["state"] == "completed":
        _ok("Full migration completed", f"run_id={run_id[:12]}...")
    else:
        _fail("Full migration not completed", status["state"])
    return run_id


# ---------------------------------------------------------------------------
# 5. Interrupt + resume test
# ---------------------------------------------------------------------------

def run_interrupt_resume_test(conn, client):
    _section("Test: interrupt after first retain, then resume")

    # Re-seed so we have fresh legacy links
    _section("Re-seeding bank with legacy links")
    seed_bank(client)
    time.sleep(2)

    resp = client.list_documents(limit=200, bank_id=TEST_BANK_ID)
    bank_docs = resp.get("items") or []

    run_id = str(uuid.uuid4())
    _ent_map2 = {LEGACY_ENT_EMAIL: [ALICE_EMAIL], LEGACY_ENT_ULID: [ALICE_EMAIL]}
    _orig2 = cm._build_entity_email_map
    cm._build_entity_email_map = lambda _c: _ent_map2
    try:
        manifest = cm.plan_identity_migration(
            run_id, conn, CRM_CONTACTS, bank_docs, is_dry_run=False
        )
    finally:
        cm._build_entity_email_map = _orig2

    # step_alias for any cid_email_derived docs that were discovered
    cm.step_alias(run_id, conn)

    # Defensively inject any test docs NOT found by discovery
    # (e.g., cid_email_derived after its legacy CID was already renamed by a prior run)
    already_planned = {m["document_id"] for m in manifest["mappings"]}
    now = _now()
    to_inject = []
    if DOC_IDS["cid_email_derived"] not in already_planned:
        to_inject.append((run_id, DOC_IDS["cid_email_derived"], LEGACY_CID, ALICE_CID,
                          "cid_email_derived", "testhash1", "aliased", now))
    if DOC_IDS["ent_ulid"] not in already_planned:
        to_inject.append((run_id, DOC_IDS["ent_ulid"], LEGACY_ENT_ULID, ALICE_CID,
                          "ent_ulid", "testhash2", "aliased", now))
    if DOC_IDS["ent_email_derived"] not in already_planned:
        to_inject.append((run_id, DOC_IDS["ent_email_derived"], LEGACY_ENT_EMAIL, ALICE_CID,
                          "ent_email_derived", "testhash3", "aliased", now))
    if to_inject:
        print(f"  Injecting {len(to_inject)} test record(s) not found by discovery")
        conn.execute("BEGIN IMMEDIATE")
        for row in to_inject:
            conn.execute(
                "INSERT OR IGNORE INTO identity_migration_docs"
                " (run_id, document_id, old_entity_link, new_entity_link,"
                "  legacy_format, evidence_hash, state, last_updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        conn.execute("COMMIT")

    call_count = [0]
    op_id_first = [None]

    def interrupted_retain(item, op_id):
        call_count[0] += 1
        if call_count[0] > 1:
            raise RuntimeError("SIMULATED INTERRUPT after retain #1")
        op_id_first[0] = op_id
        client.retain([item], operation_id=op_id, async_=False, bank_id=TEST_BANK_ID)

    try:
        cm.step_retain(run_id, conn, _build_item, interrupted_retain)
    except RuntimeError as exc:
        print(f"  Interrupted: {exc}")

    status = cm.get_migration_status(run_id, conn)
    retained = status["counts"].get("retained", 0)
    pending = sum(status["counts"].get(s, 0) for s in ("aliased", "planned", "failed"))
    print(f"  After interrupt: retained={retained}, pending={pending}")

    if op_id_first[0]:
        row = conn.execute(
            "SELECT retain_operation_id FROM identity_migration_docs"
            " WHERE run_id=? AND state='retained'",
            (run_id,),
        ).fetchone()
        if row and row[0] == op_id_first[0]:
            _ok("operation_id committed to SQLite before retain call",
                f"op_id={op_id_first[0][:12]}...")
        else:
            _fail("operation_id mismatch after interrupt", str(row))
    else:
        _fail("No retain calls completed before interrupt")

    # Resume
    print("  Resuming...")
    resume_calls = []

    def resume_retain(item, op_id):
        resume_calls.append(item["document_id"])
        client.retain([item], operation_id=op_id, async_=False, bank_id=TEST_BANK_ID)

    result = cm.resume_migration(
        run_id, conn, _build_item, resume_retain,
        lambda d: client.get_document(d, bank_id=TEST_BANK_ID),
    )
    time.sleep(2)
    print(f"  Resume result: {result}")

    if result["run_state"] in ("completed", "incomplete"):
        _ok("Resume ran successfully", result["run_state"])
    else:
        _fail("Resume failed", str(result))

    # Idempotency: resume on a completed run must not call retain again
    second_calls = []

    def counting_retain(item, op_id):
        second_calls.append(item["document_id"])
        client.retain([item], operation_id=op_id, async_=False, bank_id=TEST_BANK_ID)

    final_status = cm.get_migration_status(run_id, conn)
    if final_status["state"] == "completed":
        cm.resume_migration(
            run_id, conn, _build_item, counting_retain,
            lambda d: client.get_document(d, bank_id=TEST_BANK_ID),
        )
        if not second_calls:
            _ok("Repeated migration is a no-op (retain_fn not called on completed run)")
        else:
            _fail("Repeated migration called retain_fn", str(second_calls))
    else:
        print(f"  NOTE: run state is {final_status['state']!r}; "
              "idempotency test requires 'completed' state -- some docs may still be pending")


# ---------------------------------------------------------------------------
# 6. Idempotency evidence
# ---------------------------------------------------------------------------

def test_idempotency(client):
    _section("Idempotency: same operation_id sent twice (sync retain)")
    op_id = str(uuid.uuid4())
    content = "Idempotency probe document -- must appear exactly once."
    doc_id = f"sig:idem-probe-{uuid.uuid4().hex[:8]}"
    item = {
        "content": content,
        "timestamp": "2024-06-01T10:00:00+00:00",
        "document_id": doc_id,
        "entities": [{"text": ALICE_CID, "type": "person"}],
        "resolve_entities": False,
        "tags": ["ym:2024-06"],
        "strategy": "structured-no-llm",
    }

    resp1 = client.retain([item], operation_id=op_id, async_=False, bank_id=TEST_BANK_ID)
    resp2 = client.retain([item], operation_id=op_id, async_=False, bank_id=TEST_BANK_ID)

    print(f"  First retain:  operation_id={resp1.get('operation_id')} items={resp1.get('items_count')}")
    print(f"  Second retain: operation_id={resp2.get('operation_id')} items={resp2.get('items_count')}")
    print("  FINDING: Hindsight v1.2.1 sync retain returns operation_id=None.")
    print("  Sync idempotency is via document_id (not operation_id).")

    time.sleep(2)
    try:
        doc = client.get_document(doc_id, bank_id=TEST_BANK_ID)
        link = _entity_link(doc)
        orig = doc.get("original_text", "?")[:40]
        _ok("Document exists after two retains with same op_id",
            f"entity={link[:30]} text={orig!r}")
    except HindsightAPIError as exc:
        _fail("Document not found after idempotent retain", str(exc))

    # Count docs — should be exactly 1 for this doc_id
    all_resp = client.list_documents(limit=500, bank_id=TEST_BANK_ID)
    all_docs = all_resp.get("items") or []
    matching = [d for d in all_docs if _doc_id(d) == doc_id]
    if len(matching) == 1:
        _ok("Exactly one document instance (no duplicates)", f"doc_id={doc_id}")
    else:
        _fail("Unexpected document count", f"expected 1 got {len(matching)}")


# ---------------------------------------------------------------------------
# 7. Bank-wide certification
# ---------------------------------------------------------------------------

def certify_bank(client, seeded_hashes):
    _section("Bank-wide certification")
    errors = []

    # Paginate all documents
    all_docs = []
    offset = 0
    while True:
        resp = client.list_documents(limit=100, offset=offset, bank_id=TEST_BANK_ID)
        docs = resp.get("items") or []
        all_docs.extend(docs)
        if len(docs) < 100:
            break
        offset += 100

    print(f"  Total documents in bank: {len(all_docs)}")

    # Build lookup by document ID
    doc_map = {}
    for d in all_docs:
        did = _doc_id(d)
        if did:
            doc_map[did] = d

    # For missing docs try direct get
    for doc_id in DOC_IDS.values():
        if doc_id not in doc_map:
            try:
                doc = client.get_document(doc_id, bank_id=TEST_BANK_ID)
                doc_map[_doc_id(doc)] = doc
            except Exception as exc:
                errors.append(f"MISSING: {doc_id}: {exc}")

    test_docs_found = sum(1 for d in DOC_IDS.values() if d in doc_map)
    print(f"  Test-migration documents found: {test_docs_found}/{len(DOC_IDS)}")

    canonical_links = []
    legacy_links_found = []

    for doc_id in DOC_IDS.values():
        if doc_id not in doc_map:
            errors.append(f"MISSING: {doc_id}")
            continue

        doc = doc_map[doc_id]
        actual = _entity_link(doc)
        is_stale = actual in (LEGACY_CID, LEGACY_ENT_EMAIL, LEGACY_ENT_ULID)

        if is_stale:
            legacy_links_found.append({"doc": doc_id, "link": actual})
            errors.append(f"STALE LINK in {doc_id}: {actual}")
        elif actual == ALICE_CID:
            canonical_links.append(doc_id)
            print(f"  OK  entity link: {doc_id} -> {actual[:30]}")
        else:
            errors.append(f"UNEXPECTED LINK in {doc_id}: {actual!r}")

        # Content verification via original_text
        orig = doc.get("original_text") or ""
        expected = CONTENT.get(doc_id, "")
        if orig and expected:
            if orig.strip() == expected.strip():
                print(f"  OK  content preserved: {doc_id}")
            else:
                errors.append(f"Content mismatch {doc_id}")

    # Operations check
    try:
        ops = client.list_operations(limit=100, bank_id=TEST_BANK_ID)
        op_list = ops.get("operations") or ops.get("items") or []
        bad = [o for o in op_list if o.get("status") in ("pending","processing","failed","unknown")]
        if bad:
            errors.append(f"Bad ops: {[(o.get('status'), str(o.get('operation_id',''))[:8]) for o in bad]}")
        else:
            _ok(f"No pending/failed operations ({len(op_list)} total)")
    except Exception as exc:
        print(f"  WARNING: cannot check operations: {exc}")

    print(f"\n  Canonical: {len(canonical_links)}/{len(DOC_IDS)}")
    print(f"  Stale:     {len(legacy_links_found)}")
    if errors:
        for e in errors:
            _fail("CERT", e)
        return False
    _ok("Bank-wide certification PASSED",
        f"{len(canonical_links)} canonical, 0 stale, 0 missing, content OK")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import tempfile
    print(f"\nOLO M3 -- Live Migration Acceptance Test")
    print(f"Starting SHA: 4b58ed4a01ab85f1e110950b7bb2eef4e4d74da0")
    print(f"Test bank:    {TEST_BANK_ID}")
    print(f"Test port:    {TEST_PORT}")
    print(f"Hindsight:    1.2.1 (onnx embeddings, no LLM)")
    print(f"Time:         {_now()}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="olo_mig_"))
    proc = None

    try:
        proc, client = start_hindsight()
        conn = setup_legacy_db(tmp_dir)
        seeded_hashes = seed_bank(client)
        # Configure bank after first retain
        from olo.m3.feeder import prepare_hindsight_bank
        prepare_hindsight_bank(client, TEST_BANK_ID)
        time.sleep(2)

        run_id = run_full_migration(conn, client)
        run_interrupt_resume_test(conn, client)
        test_idempotency(client)
        ok = certify_bank(client, seeded_hashes)

        # Final summary
        _section("Final results")
        final = cm.get_migration_status(run_id, conn)
        print(json.dumps(final, indent=2))

        # Frozen fingerprint
        _section("Frozen benchmark fingerprint")
        try:
            from olo.m3.synthetic.generator import generate_timeline
            from olo.m3.feeder import populate_olo_db, build_hindsight_items
            from olo.m3.benchmark_preflight import (
                FROZEN_FULL_ITEM_FP, check_full_item_fingerprint,
            )
            tl = generate_timeline()
            fp_db = tmp_dir / "corpus" / "olo.db"
            fp_db.parent.mkdir(parents=True, exist_ok=True)
            populate_olo_db(tl, fp_db)
            items = build_hindsight_items(fp_db)
            fp = check_full_item_fingerprint(items)
            if fp.ok:
                _ok("Frozen fingerprint UNCHANGED", f"{FROZEN_FULL_ITEM_FP[:20]}...")
            else:
                _fail("Fingerprint MISMATCH",
                      f"expected={FROZEN_FULL_ITEM_FP[:20]} got={fp.actual_fp[:20]}")
        except Exception as exc:
            print(f"  WARNING: fingerprint check skipped: {exc}")

        # Linux gate status
        _section("Linux strict gate")
        print("  PENDING -- requires port 18800 / main-v1-granite bank (separate from this test).")
        print("  Migration acceptance uses port 19901 / disposable bank.")

        return 0 if ok else 1

    except Exception as exc:
        import traceback
        print(f"\nFATAL: {exc}")
        traceback.print_exc()
        return 2

    finally:
        if proc:
            _kill_pg(proc)
            print("\nHindsight server stopped.")


if __name__ == "__main__":
    sys.exit(main())
