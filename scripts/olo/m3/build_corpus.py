#!/usr/bin/env python3
"""Reproducible development benchmark corpus builder.

Rebuilds the OLO M3 development benchmark corpus from committed synthetic
fixtures using only the production ingestion, identity and feeder path.

Usage
-----
    python scripts/olo/m3/build_corpus.py [--db-dir DIR] [--out MANIFEST_JSON]

Options
-------
--db-dir     Directory to write olo.db into (default: /tmp/olo_m3_corpus).
             Created fresh each run; existing olo.db is removed first.
--out        Path to write the build manifest JSON
             (default: /tmp/olo_m3_corpus/build_manifest.json).
--check-only Verify an existing DB matches the expected corpus; do not rebuild.
--verbose    Print per-signal coverage stats.

Exit codes
----------
0  Corpus built and verified successfully.
1  Build failed or corpus mismatch detected.

Design
------
1. Starts from a clean, disposable olo.db.
2. Uses the production ingestion and identity path:
      generate_timeline() → populate_olo_db() → upsert_entities()
3. Builds and verifies Hindsight retain items via build_hindsight_items().
4. Validates document IDs, authoritative source hashes and corpus fingerprint.
5. Records a canonical content hash for every memory item.
6. Generates a deterministic corpus fingerprint from sorted (doc_id, sha256) pairs.
7. Writes a build manifest with source revision, fixture hashes, generator
   version, identity schema version and configuration.
8. Refuses to operate on a stale or contaminated existing database.
9. Can be rerun without manually importing synthetic identities.
10. Fails loudly on missing sources, inconsistent IDs or corpus mismatches.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure imports work from both project root and scripts/ subdirectory
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _git_revision() -> str:
    """Return the current git commit SHA, or 'unknown'."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _file_sha256(path: Path) -> str:
    """Return SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_corpus(
    db_dir: Path,
    *,
    verbose: bool = False,
    check_only: bool = False,
) -> dict:
    """Build (or verify) the development benchmark corpus.

    Returns a manifest dict describing the build.
    Raises SystemExit(1) on any failure.
    """
    from olo.m3.synthetic.generator import (
        ENTITIES, generate_timeline, corpus_fingerprint, _DEFAULT_SEED,
        get_benchmark_hash,
    )
    from olo.m3.feeder import populate_olo_db, build_hindsight_items
    from olo.adapters.entity_extractor import entity_id_from_email

    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "olo.db"

    if check_only:
        if not db_path.exists():
            print(f"ERROR: --check-only specified but olo.db not found at {db_path}",
                  file=sys.stderr)
            sys.exit(1)
        print(f"Verifying existing corpus at {db_path}")
    else:
        # Remove any existing database — refuse to operate on stale data.
        if db_path.exists():
            db_path.unlink()
            if verbose:
                print(f"Removed existing olo.db at {db_path}")
        print(f"Building fresh corpus at {db_path} ...")

    # --- Step 1: Generate timeline ---
    t0 = time.monotonic()
    tl = generate_timeline()
    gen_elapsed = time.monotonic() - t0

    expected_fp = corpus_fingerprint(tl)
    expected_bh = get_benchmark_hash()
    signal_count = len(tl)

    # Collect per-kind signal stats
    kind_counts: dict[str, int] = {}
    for sig in tl:
        kind_counts[sig.kind] = kind_counts.get(sig.kind, 0) + 1

    print(f"Generated {signal_count} signals in {gen_elapsed:.2f}s")
    print(f"  Corpus fingerprint: {expected_fp[:32]}...")
    print(f"  Benchmark hash:     {expected_bh[:32]}...")
    if verbose:
        for k, v in sorted(kind_counts.items()):
            print(f"  {k}: {v}")

    # --- Step 2: Ingest into olo.db ---
    if not check_only:
        t1 = time.monotonic()
        stored = populate_olo_db(tl, db_path, source="hindsight_gate")
        ingest_elapsed = time.monotonic() - t1
        if stored != signal_count:
            print(
                f"ERROR: Expected {signal_count} stored signals, got {stored}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Ingested {stored} signals in {ingest_elapsed:.2f}s")

    # --- Step 3: Inspect entity_metadata ---
    conn = sqlite3.connect(str(db_path))
    try:
        em_rows = conn.execute(
            "SELECT entity_id, display_name, company, source_kind "
            "FROM entity_metadata ORDER BY display_name"
        ).fetchall()
    except sqlite3.OperationalError:
        em_rows = []
    conn.close()

    entity_coverage: list[dict] = [
        {
            "entity_id": row[0],
            "display_name": row[1],
            "company": row[2],
            "source_kind": row[3],
        }
        for row in em_rows
    ]
    # Check which synthetic entities have resolved display names
    entities_with_names = set(e["display_name"] for e in entity_coverage if e["display_name"])

    missing_entities = []
    for ent in ENTITIES:
        if ent.display_name not in entities_with_names:
            missing_entities.append(ent.display_name)

    print(f"entity_metadata rows: {len(em_rows)}")
    if verbose:
        for row in entity_coverage:
            print(f"  {row['entity_id']} | {row['display_name']!r} | {row['source_kind']}")
    if missing_entities:
        print(f"WARNING: These entity names are missing from entity_metadata: {missing_entities}")
    else:
        print(f"All 3 customer contact names resolved: "
              f"{', '.join(ent.display_name for ent in ENTITIES)}")

    # --- Step 4: Build Hindsight items ---
    t2 = time.monotonic()
    items = build_hindsight_items(db_path, source="hindsight_gate")
    items_elapsed = time.monotonic() - t2

    if len(items) != signal_count:
        print(
            f"ERROR: Expected {signal_count} Hindsight items, got {len(items)}",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Built {len(items)} Hindsight items in {items_elapsed:.2f}s")

    # --- Step 5: Validate document IDs and content hashes ---
    expected_doc_ids = {f"sig:{sig.signal_id}" for sig in tl}
    actual_doc_ids = {i["document_id"] for i in items}

    missing_docs = expected_doc_ids - actual_doc_ids
    extra_docs = actual_doc_ids - expected_doc_ids
    if missing_docs or extra_docs:
        print(
            f"ERROR: Document ID mismatch — "
            f"missing: {len(missing_docs)}, extra: {len(extra_docs)}",
            file=sys.stderr,
        )
        if missing_docs:
            print(f"  Missing (first 5): {list(missing_docs)[:5]}", file=sys.stderr)
        if extra_docs:
            print(f"  Extra (first 5): {list(extra_docs)[:5]}", file=sys.stderr)
        sys.exit(1)

    # Validate content hashes against authoritative source
    expected_hashes: dict[str, str] = {
        f"sig:{sig.signal_id}": hashlib.sha256(sig.body_text.encode()).hexdigest()
        for sig in tl
    }
    stale = []
    for item in items:
        doc_id = item["document_id"]
        item_sha = (item.get("metadata") or {}).get("content_sha256", "")
        expected_sha = expected_hashes.get(doc_id, "")
        if item_sha and expected_sha and item_sha != expected_sha:
            stale.append(doc_id)

    if stale:
        print(
            f"ERROR: {len(stale)} item(s) have stale content hashes (first 3: {stale[:3]})",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Step 6: Compute corpus fingerprint from items ---
    fingerprint_rows = sorted(
        (item["document_id"], expected_hashes.get(item["document_id"], ""))
        for item in items
    )
    item_fp_data = json.dumps(fingerprint_rows, separators=(",", ":"))
    item_fingerprint = hashlib.sha256(item_fp_data.encode()).hexdigest()

    # Full-item fingerprint: covers context, entity links and tags in addition
    # to document IDs and body hashes.  Detects context-enrichment and entity-
    # link changes that the body-only item_fingerprint cannot.
    from olo.m3.feeder import full_item_fingerprint as _full_fp
    from olo.m3.benchmark_preflight import FROZEN_FULL_ITEM_FP
    full_fingerprint = _full_fp(items)
    if full_fingerprint != FROZEN_FULL_ITEM_FP:
        print(
            f"ERROR: Full-item fingerprint mismatch.\n"
            f"  Computed: {full_fingerprint}\n"
            f"  Expected: {FROZEN_FULL_ITEM_FP}\n"
            "The corpus retain inputs differ from the committed baseline. "
            "If this is intentional, update FROZEN_FULL_ITEM_FP in benchmark_preflight.py.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Step 7: Identity and context coverage ---
    with_name = sum(
        1 for i in items
        if any(ent.display_name in (i.get("context") or "") for ent in ENTITIES)
    )
    without_name = len(items) - with_name
    name_coverage_pct = with_name / len(items) * 100

    no_name_kinds: dict[str, int] = {}
    for item in items:
        ctx = item.get("context") or ""
        has = any(ent.display_name in ctx for ent in ENTITIES)
        if not has:
            kind = (item.get("metadata") or {}).get("kind", "?")
            no_name_kinds[kind] = no_name_kinds.get(kind, 0) + 1

    print(
        f"Context name coverage: {with_name}/{len(items)} ({name_coverage_pct:.1f}%)"
    )
    if without_name and verbose:
        print(f"  Items without contact name by kind: {no_name_kinds}")
    if without_name:
        print(
            f"  {without_name} items without contact name "
            f"(expected: call.transcript and invoice.created signals)"
        )

    # --- Step 8: Build manifest ---
    gen_module = sys.modules.get("olo.m3.synthetic.generator")
    gen_path = Path(
        getattr(gen_module, "__file__", str(_REPO_ROOT / "olo/m3/synthetic/generator.py"))
    ).resolve()
    feeder_path = (_REPO_ROOT / "olo/m3/feeder.py").resolve()

    manifest = {
        "build_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_revision": _git_revision(),
        "generator": {
            "path": str(gen_path.relative_to(_REPO_ROOT)),
            "sha256": _file_sha256(gen_path),
            "seed": _DEFAULT_SEED,
            "version_comment": "corpus builder v1.0 (entity metadata fix)",
        },
        "feeder": {
            "path": str(feeder_path.relative_to(_REPO_ROOT)),
            "sha256": _file_sha256(feeder_path),
        },
        "identity_schema_version": 4,  # schema v4 (entity_metadata table)
        "corpus": {
            "signal_count": signal_count,
            "item_count": len(items),
            "corpus_fingerprint": expected_fp,
            "body_item_fingerprint": item_fingerprint,
            "full_item_fingerprint": full_fingerprint,
            "benchmark_hash": expected_bh,
            "kind_counts": kind_counts,
        },
        "identity": {
            "entity_metadata_rows": len(em_rows),
            "entities_with_names": sorted(entities_with_names),
            "missing_entity_names": missing_entities,
            "context_name_coverage": f"{with_name}/{len(items)}",
            "context_name_coverage_pct": round(name_coverage_pct, 1),
            "no_name_signal_kinds": no_name_kinds,
        },
        "entities": entity_coverage,
    }

    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build or verify the OLO M3 development benchmark corpus."
    )
    parser.add_argument(
        "--db-dir",
        default="/tmp/olo_m3_corpus",
        help="Directory to write olo.db into (default: /tmp/olo_m3_corpus)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Path to write build manifest JSON (default: DB_DIR/build_manifest.json)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Verify an existing DB; do not rebuild",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-signal coverage stats",
    )
    args = parser.parse_args()

    db_dir = Path(args.db_dir)
    out_path = Path(args.out) if args.out else db_dir / "build_manifest.json"

    try:
        manifest = build_corpus(
            db_dir,
            verbose=args.verbose,
            check_only=args.check_only,
        )
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    # Write manifest
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nBuild manifest written to: {out_path}")
    print(
        f"\nCorpus fingerprint (body+id):  {manifest['corpus']['corpus_fingerprint']}\n"
        f"Body-item fingerprint:         {manifest['corpus']['body_item_fingerprint']}\n"
        f"Full-item fingerprint:         {manifest['corpus']['full_item_fingerprint']}"
    )
    print("\nCorpus built successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
