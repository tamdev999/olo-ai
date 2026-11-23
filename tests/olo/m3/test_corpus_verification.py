"""Tests proving corpus mismatch detection blocks benchmark scoring.

Per Opus fourth-remediation audit: the previous implementation read
doc["metadata"]["content_sha256"] — a client-supplied value that the server
cannot independently verify.  A stale bank can supply any hash it likes at
ingest time.  The corrected implementation reads the server-generated
``content_hash`` from the list-documents response.

These tests prove that:
- A bank with correct server content_hash values passes.
- A bank with stale content_hash (mismatches body_text SHA-256) is rejected.
- A bank with documents that have no content_hash is rejected.
- A bank missing expected signals is rejected.
- A bank with extra (unexpected) documents is rejected.
- A fingerprint mismatch is rejected before any bank check.
- A bank that supplies the right hash in metadata but wrong in content_hash
  is rejected (the metadata hash is ignored).
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from scripts.olo.m3.run_live_benchmark import (
    CorpusMismatchError, verify_corpus_match
)
from olo.m3.hindsight.client import HindsightConfig, HindsightClient


def _client_with_docs(docs: list[dict]) -> HindsightClient:
    """Return a mock client whose list_documents returns `docs`."""
    cfg = HindsightConfig(host="127.0.0.1", port=19999)
    client = HindsightClient(cfg)

    def _fake_list(limit=100, offset=0, bank_id=None):
        page = docs[offset:offset + limit]
        return {"items": page}

    client.list_documents = _fake_list
    return client


def _expected_docs_correct() -> list[dict]:
    """Build docs with correct server-generated content_hash values."""
    from olo.m3.synthetic.generator import generate_timeline
    tl = generate_timeline()
    result = []
    for sig in tl:
        # content_hash = SHA-256 of the body_text, as the server would compute it
        sha = hashlib.sha256(sig.body_text.encode("utf-8")).hexdigest()
        result.append({
            "document_id": f"sig:{sig.signal_id}",
            # Server-generated hash — this is what verify_corpus_match reads
            "content_hash": sha,
            # Also populate document_metadata (but verify_corpus_match must NOT
            # rely on this for correctness)
            "document_metadata": {"content_sha256": sha},
        })
    return result


class TestCorpusVerification:
    def test_correct_bank_passes(self) -> None:
        """A bank with correct content_hash values passes verification."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)
        # Should not raise
        verify_corpus_match(client, fp)

    def test_stale_content_hash_raises(self) -> None:
        """Bank has correct signal IDs but wrong server content_hash → rejected."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)

        docs = _expected_docs_correct()
        # Corrupt the first signal's server-generated content_hash
        docs[0] = dict(docs[0])
        docs[0]["content_hash"] = "0" * 64
        client = _client_with_docs(docs)

        with pytest.raises(CorpusMismatchError, match="stale content"):
            verify_corpus_match(client, fp)

    def test_metadata_only_hash_not_sufficient(self) -> None:
        """Bank supplies correct hash in metadata but wrong content_hash → rejected.

        This is the core corpus_live.py Case B scenario:
        stale text with metadata claiming the correct hash.
        verify_corpus_match must NOT accept metadata hashes as proof of content.
        """
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)

        docs = _expected_docs_correct()
        # Replace content_hash with stale value, keep document_metadata correct
        docs[0] = dict(docs[0])
        docs[0]["content_hash"] = "stale" + "0" * 59  # wrong
        # document_metadata claims the correct hash — must be ignored
        # (docs[0]["document_metadata"] is left correct from _expected_docs_correct)
        client = _client_with_docs(docs)

        with pytest.raises(CorpusMismatchError, match="stale content"):
            verify_corpus_match(client, fp)

    def test_missing_content_hash_raises(self) -> None:
        """A bank document with no content_hash field is rejected.

        Case C from corpus_live.py: stale text with no metadata hash.
        Without content_hash, we cannot verify corpus integrity.
        """
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)

        docs = _expected_docs_correct()
        # Remove content_hash entirely from first doc
        docs[0] = {k: v for k, v in docs[0].items() if k != "content_hash"}
        client = _client_with_docs(docs)

        with pytest.raises(CorpusMismatchError, match="no server-generated"):
            verify_corpus_match(client, fp)

    def test_missing_signal_raises(self) -> None:
        """Bank is missing one expected signal → rejected."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()[:-1]  # Drop last signal
        client = _client_with_docs(docs)

        with pytest.raises(CorpusMismatchError, match="missing"):
            verify_corpus_match(client, fp)

    def test_extra_document_raises(self) -> None:
        """Bank has an extra unexpected document → rejected (not just noted).

        Case E from corpus_live.py: extra document with otherwise correct data.
        """
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)

        docs = _expected_docs_correct()
        # Append an unexpected extra document
        docs.append({
            "document_id": "sig:extra_unexpected_doc",
            "content_hash": hashlib.sha256(b"extra").hexdigest(),
            "document_metadata": {},
        })
        client = _client_with_docs(docs)

        with pytest.raises(CorpusMismatchError, match="unexpected"):
            verify_corpus_match(client, fp)

    def test_fingerprint_mismatch_raises(self) -> None:
        """Generator fingerprint mismatch → rejected before any bank check."""
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)
        wrong_fp = "0" * 64

        with pytest.raises(CorpusMismatchError, match="fingerprint mismatch"):
            verify_corpus_match(client, wrong_fp)

    def test_all_docs_missing_content_hash_raises(self) -> None:
        """Every document missing content_hash → rejected (not silently passing)."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)

        # Strip content_hash from every document
        docs = [
            {"document_id": d["document_id"], "document_metadata": d.get("document_metadata", {})}
            for d in _expected_docs_correct()
        ]
        client = _client_with_docs(docs)

        with pytest.raises(CorpusMismatchError):
            verify_corpus_match(client, fp)
