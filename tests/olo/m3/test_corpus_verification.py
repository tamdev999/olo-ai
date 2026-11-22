"""Tests proving corpus mismatch detection blocks benchmark scoring.

Per Opus release audit: the previous spot-check (6 signal IDs) was insufficient.
These tests prove that:
- A bank with stale content (content hash differs) is rejected.
- A bank missing expected signals is rejected.
- A fingerprint mismatch is rejected.
"""
from __future__ import annotations

import hashlib, json
from unittest.mock import MagicMock, patch

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
        # Paginate
        page = docs[offset:offset + limit]
        return {"items": page}

    client.list_documents = _fake_list
    return client


def _expected_docs() -> list[dict]:
    """Build the expected documents list from the current generator."""
    import sys; sys.path.insert(0, "/mnt/c/oloai")
    from olo.m3.synthetic.generator import generate_timeline
    tl = generate_timeline()
    result = []
    for sig in tl:
        sha = hashlib.sha256(sig.body_text.encode()).hexdigest()
        result.append({
            "document_id": f"sig:{sig.signal_id}",
            "metadata": {"content_sha256": sha},
        })
    return result


class TestCorpusVerification:
    def test_correct_bank_passes(self) -> None:
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs()
        client = _client_with_docs(docs)
        # Should not raise
        verify_corpus_match(client, fp)

    def test_stale_content_raises(self) -> None:
        """Bank has correct signal IDs but old content hashes → rejected."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)

        docs = _expected_docs()
        # Corrupt the first signal's content hash (simulating a stale bank)
        docs[0]["metadata"]["content_sha256"] = "0000000000000000000000000000000000000000000000000000000000000000"
        client = _client_with_docs(docs)

        with pytest.raises(CorpusMismatchError, match="stale content"):
            verify_corpus_match(client, fp)

    def test_missing_signal_raises(self) -> None:
        """Bank is missing one expected signal → rejected."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs()[:-1]  # Drop last signal
        client = _client_with_docs(docs)

        with pytest.raises(CorpusMismatchError, match="missing"):
            verify_corpus_match(client, fp)

    def test_fingerprint_mismatch_raises(self) -> None:
        """Generator fingerprint mismatch → rejected before any bank check."""
        docs = _expected_docs()
        client = _client_with_docs(docs)
        wrong_fp = "0" * 64

        with pytest.raises(CorpusMismatchError, match="fingerprint mismatch"):
            verify_corpus_match(client, wrong_fp)
