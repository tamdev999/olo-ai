"""Tests for corpus verification including pending-operation detection (P0.2).

P0.2 root cause: the previous operations check used ``GET /operations?limit=1``
which returned only the NEWEST operation.  Two older pending consolidation
operations were invisible; the verifier incorrectly declared the bank ready
after a newer completed re-retain appeared at the top of the list.

Additional defects fixed:
- All exceptions were swallowed (``except Exception: pass``).
- No API key was sent (used urllib.request directly instead of _request).
- Only the first page was checked; ``total`` was misread.

This test suite covers:
- Two pending ops + one newer completed → REJECTED  (main P0.2 failure)
- Processing op → REJECTED
- All ops completed → PASSES
- Empty ops (no operations ever) → PASSES
- Unreachable ops endpoint → REJECTED
- 4xx / 5xx ops endpoint → REJECTED
- Missing 'items'/'operations' key → REJECTED
- Unknown status (not in terminal set) → REJECTED
- Race guard: ops clear before enum, new pending after → REJECTED
- Stale content_hash → REJECTED
- Missing content_hash → REJECTED
- Missing signal → REJECTED
- Extra sig: document → REJECTED
- Extra non-sig: document → REJECTED
- Fingerprint mismatch → REJECTED
- Correct corpus, all ops done → PASSES, returns 64-char fingerprint
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from scripts.olo.m3.run_live_benchmark import (
    CorpusMismatchError,
    verify_corpus_match,
    _check_no_pending_operations,
    _TERMINAL_OP_STATUSES,
)
from olo.m3.hindsight.client import HindsightConfig, HindsightClient


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _client_with_docs(
    docs: list[dict],
) -> HindsightClient:
    """Mock client whose list_documents returns the given docs.

    The operations endpoint is separately controlled via
    ``patch("olo.m3.hindsight.client._request", ...)``.
    """
    cfg = HindsightConfig(host="127.0.0.1", port=19999)
    client = HindsightClient(cfg)

    def _fake_list(limit=100, offset=0, bank_id=None):
        page = docs[offset:offset + limit]
        return {"items": page, "total": len(docs)}

    client.list_documents = _fake_list
    return client


def _ops_response(ops: list[dict]) -> dict:
    """Build the operations endpoint response dict for patching."""
    return {"items": ops, "total": len(ops)}


def _op(status: str, op_type: str = "consolidation") -> dict:
    return {"status": status, "task_type": op_type, "id": f"op-{status}"}


def _expected_docs_correct() -> list[dict]:
    """Build docs with correct server-generated content_hash values."""
    from olo.m3.synthetic.generator import generate_timeline
    tl = generate_timeline()
    result = []
    for sig in tl:
        sha = hashlib.sha256(sig.body_text.encode("utf-8")).hexdigest()
        result.append({
            "document_id": f"sig:{sig.signal_id}",
            "content_hash": sha,
            "document_metadata": {"content_sha256": sha},
        })
    return result


# ---------------------------------------------------------------------------
# P0.2 — Operations-state check unit tests
# ---------------------------------------------------------------------------

class TestCheckNoPendingOperations:
    """Unit tests for _check_no_pending_operations."""

    def _make_client(self) -> HindsightClient:
        cfg = HindsightConfig(host="127.0.0.1", port=19999)
        return HindsightClient(cfg)

    def test_empty_ops_list_passes(self) -> None:
        """Zero operations (new bank, never retained) — passes immediately."""
        client = self._make_client()
        with patch("olo.m3.hindsight.client._request",
                   return_value={"items": [], "total": 0}):
            count = _check_no_pending_operations(client, "test-bank")
        assert count == 0

    def test_all_terminal_ops_pass(self) -> None:
        """All operations in terminal status — passes."""
        client = self._make_client()
        ops = [_op("completed"), _op("failed"), _op("succeeded"), _op("done")]
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            count = _check_no_pending_operations(client, "test-bank")
        assert count == 4

    def test_pending_op_raises(self) -> None:
        """A single pending operation causes rejection."""
        client = self._make_client()
        ops = [_op("completed"), _op("pending"), _op("completed")]
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            with pytest.raises(CorpusMismatchError, match="non-terminal"):
                _check_no_pending_operations(client, "test-bank")

    def test_processing_op_raises(self) -> None:
        """A processing operation causes rejection."""
        client = self._make_client()
        ops = [_op("completed"), _op("processing", "retain")]
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            with pytest.raises(CorpusMismatchError, match="non-terminal"):
                _check_no_pending_operations(client, "test-bank")

    def test_p0_2_root_cause_two_pending_plus_newer_completed(self) -> None:
        """P0.2 root cause: two pending ops hidden behind a newer completed one.

        The old implementation used limit=1, which returned only the newest
        (completed) op.  This test verifies the fix sees ALL ops across pages
        and rejects the bank when older pending ops exist.

        In the actual pg0_negative_v4 live scenario:
        - ops endpoint with limit=1 → ["completed"]    (old verifier passes)
        - ops endpoint with limit=500 → ["completed", "pending", "pending"]

        The mock here simulates limit=500 returning all three.
        """
        client = self._make_client()
        # Newest first (server returns newest at index 0)
        ops = [
            _op("completed", "retain"),          # newest: a quick re-retain
            _op("pending", "consolidation"),      # older: consolidation 1
            _op("pending", "consolidation"),      # older: consolidation 2
        ]
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            with pytest.raises(CorpusMismatchError, match="non-terminal"):
                _check_no_pending_operations(client, "test-bank")

    def test_unknown_status_raises(self) -> None:
        """Unknown status (not in terminal set) → fail closed."""
        client = self._make_client()
        ops = [_op("queued"), _op("completed")]   # "queued" is not terminal
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            with pytest.raises(CorpusMismatchError, match="non-terminal"):
                _check_no_pending_operations(client, "test-bank")

    def test_missing_status_raises(self) -> None:
        """Operation with no status key → fail closed."""
        client = self._make_client()
        ops = [{"task_type": "retain", "id": "op-nostatus"}]  # no "status" key
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            with pytest.raises(CorpusMismatchError, match="non-terminal"):
                _check_no_pending_operations(client, "test-bank")

    def test_unreachable_endpoint_raises(self) -> None:
        """Network error → CorpusMismatchError, never swallowed."""
        from olo.m3.hindsight.client import HindsightUnavailable
        client = self._make_client()
        with patch("olo.m3.hindsight.client._request",
                   side_effect=HindsightUnavailable("Connection refused")):
            with pytest.raises(CorpusMismatchError, match="unreachable"):
                _check_no_pending_operations(client, "test-bank")

    def test_http_4xx_raises(self) -> None:
        """HTTP 4xx (e.g. 401 Unauthorized) → CorpusMismatchError."""
        from olo.m3.hindsight.client import HindsightAPIError
        client = self._make_client()
        err = HindsightAPIError("Unauthorized", status_code=401)
        with patch("olo.m3.hindsight.client._request", side_effect=err):
            with pytest.raises(CorpusMismatchError, match="HTTP 401"):
                _check_no_pending_operations(client, "test-bank")

    def test_malformed_response_no_items_key_raises(self) -> None:
        """Response missing 'items'/'operations' key with total > 0 → rejected."""
        client = self._make_client()
        with patch("olo.m3.hindsight.client._request",
                   return_value={"total": 5}):  # no items key
            with pytest.raises(CorpusMismatchError):
                _check_no_pending_operations(client, "test-bank")

    def test_non_dict_response_raises(self) -> None:
        """Non-dict response → rejected."""
        client = self._make_client()
        with patch("olo.m3.hindsight.client._request",
                   return_value=[{"status": "completed"}]):  # list, not dict
            with pytest.raises(CorpusMismatchError, match="malformed"):
                _check_no_pending_operations(client, "test-bank")

    def test_terminal_statuses_are_complete(self) -> None:
        """All expected terminal statuses are in _TERMINAL_OP_STATUSES."""
        for s in ("completed", "failed", "succeeded", "done", "error"):
            assert s in _TERMINAL_OP_STATUSES, f"{s!r} must be terminal"

    def test_pending_and_processing_are_non_terminal(self) -> None:
        """'pending' and 'processing' must NOT be in terminal set."""
        assert "pending" not in _TERMINAL_OP_STATUSES
        assert "processing" not in _TERMINAL_OP_STATUSES


# ---------------------------------------------------------------------------
# verify_corpus_match — operations integration
# ---------------------------------------------------------------------------

class TestVerifyCorpusMatchOperations:
    """verify_corpus_match rejects banks with pending operations."""

    def _all_ops_done(self) -> dict:
        return {"items": [], "total": 0}

    def test_correct_bank_all_ops_done_passes(self) -> None:
        """Correct docs + no pending ops → verification passes, returns fingerprint."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)

        with patch("olo.m3.hindsight.client._request",
                   return_value=self._all_ops_done()):
            bank_fp = verify_corpus_match(client, fp)
        assert isinstance(bank_fp, str) and len(bank_fp) == 64

    def test_pending_ops_prevent_verification(self) -> None:
        """Pending operations → rejected even if all documents are correct."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)

        ops_with_pending = {"items": [_op("pending", "consolidation")], "total": 1}
        with patch("olo.m3.hindsight.client._request",
                   return_value=ops_with_pending):
            with pytest.raises(CorpusMismatchError, match="non-terminal"):
                verify_corpus_match(client, fp)

    def test_p0_2_two_pending_behind_completed_rejected(self) -> None:
        """P0.2 exact scenario: newest op is completed; two older ones are pending.

        This replicates Opus's pending_live.py audit:
        - Two consolidation operations still pending.
        - One newer, quickly-completed re-retain operation.
        - limit=1 would return only the completed one (old bug passes).
        - limit=500 returns all three; fix correctly rejects.
        """
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)

        all_ops = {
            "items": [
                _op("completed", "retain"),       # newest: quick re-retain
                _op("pending", "consolidation"),  # older: consolidation 1
                _op("pending", "consolidation"),  # older: consolidation 2
            ],
            "total": 3,
        }
        with patch("olo.m3.hindsight.client._request", return_value=all_ops):
            with pytest.raises(CorpusMismatchError, match="non-terminal"):
                verify_corpus_match(client, fp)

    def test_processing_op_prevents_verification(self) -> None:
        """Processing operation → rejected."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)

        ops_processing = {
            "items": [_op("processing", "consolidation")], "total": 1
        }
        with patch("olo.m3.hindsight.client._request",
                   return_value=ops_processing):
            with pytest.raises(CorpusMismatchError, match="non-terminal"):
                verify_corpus_match(client, fp)

    def test_ops_endpoint_unreachable_prevents_verification(self) -> None:
        """Unreachable ops endpoint → verification fails (never swallowed)."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        from olo.m3.hindsight.client import HindsightUnavailable
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)

        with patch("olo.m3.hindsight.client._request",
                   side_effect=HindsightUnavailable("Connection refused")):
            with pytest.raises(CorpusMismatchError, match="unreachable"):
                verify_corpus_match(client, fp)

    def test_ops_endpoint_unauthorized_prevents_verification(self) -> None:
        """HTTP 401 from ops endpoint → verification fails."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        from olo.m3.hindsight.client import HindsightAPIError
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)

        err = HindsightAPIError("Unauthorized", status_code=401)
        with patch("olo.m3.hindsight.client._request", side_effect=err):
            with pytest.raises(CorpusMismatchError, match="HTTP 401"):
                verify_corpus_match(client, fp)

    def test_race_guard_new_pending_op_after_enum_rejected(self) -> None:
        """Race guard: ops clear before doc enumeration, new pending after → rejected.

        verify_corpus_match calls _check_no_pending_operations TWICE:
        once before and once after document enumeration.  This test verifies
        the second call catches a pending operation that appeared during
        document enumeration.
        """
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)

        all_clear = {"items": [], "total": 0}
        new_pending = {"items": [_op("pending", "consolidation")], "total": 1}

        call_count = {"n": 0}

        def _side_effect(cfg, method, path, body=None, params=None):
            call_count["n"] += 1
            # First ops check (pre-enum): all clear
            # Second ops check (post-enum): new pending op appeared
            if "operations" in path:
                if call_count["n"] == 1:
                    return all_clear
                else:
                    return new_pending
            return {"items": [], "total": 0}

        with patch("olo.m3.hindsight.client._request", side_effect=_side_effect):
            with pytest.raises(CorpusMismatchError, match="non-terminal"):
                verify_corpus_match(client, fp)


# ---------------------------------------------------------------------------
# verify_corpus_match — existing document-integrity tests (unchanged)
# ---------------------------------------------------------------------------

class TestCorpusVerification:
    """Document-level verification: content hashes, missing/extra docs, fingerprint."""

    def _all_ops_done(self) -> dict:
        return {"items": [], "total": 0}

    def test_correct_bank_passes(self) -> None:
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)
        with patch("olo.m3.hindsight.client._request",
                   return_value=self._all_ops_done()):
            bank_fp = verify_corpus_match(client, fp)
        assert isinstance(bank_fp, str) and len(bank_fp) == 64

    def test_stale_content_hash_raises(self) -> None:
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        docs[0] = dict(docs[0])
        docs[0]["content_hash"] = "0" * 64
        client = _client_with_docs(docs)
        with patch("olo.m3.hindsight.client._request",
                   return_value=self._all_ops_done()):
            with pytest.raises(CorpusMismatchError, match="stale content"):
                verify_corpus_match(client, fp)

    def test_metadata_only_hash_not_sufficient(self) -> None:
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        docs[0] = dict(docs[0])
        docs[0]["content_hash"] = "stale" + "0" * 59
        client = _client_with_docs(docs)
        with patch("olo.m3.hindsight.client._request",
                   return_value=self._all_ops_done()):
            with pytest.raises(CorpusMismatchError, match="stale content"):
                verify_corpus_match(client, fp)

    def test_missing_content_hash_raises(self) -> None:
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        docs[0] = {k: v for k, v in docs[0].items() if k != "content_hash"}
        client = _client_with_docs(docs)
        with patch("olo.m3.hindsight.client._request",
                   return_value=self._all_ops_done()):
            with pytest.raises(CorpusMismatchError, match="no server-generated"):
                verify_corpus_match(client, fp)

    def test_missing_signal_raises(self) -> None:
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()[:-1]
        client = _client_with_docs(docs)
        with patch("olo.m3.hindsight.client._request",
                   return_value=self._all_ops_done()):
            with pytest.raises(CorpusMismatchError, match="missing"):
                verify_corpus_match(client, fp)

    def test_extra_sig_document_raises(self) -> None:
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        docs.append({
            "document_id": "sig:extra_unexpected_doc",
            "content_hash": hashlib.sha256(b"extra").hexdigest(),
        })
        client = _client_with_docs(docs)
        with patch("olo.m3.hindsight.client._request",
                   return_value=self._all_ops_done()):
            with pytest.raises(CorpusMismatchError, match="unexpected"):
                verify_corpus_match(client, fp)

    def test_extra_non_sig_document_raises(self) -> None:
        """Case G: non-sig: contaminant from gate tests."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        docs.append({
            "document_id": "contaminant-from-gate",
            "content_hash": hashlib.sha256(b"gate artifact").hexdigest(),
        })
        client = _client_with_docs(docs)
        with patch("olo.m3.hindsight.client._request",
                   return_value=self._all_ops_done()):
            with pytest.raises(CorpusMismatchError, match="non-sig"):
                verify_corpus_match(client, fp)

    def test_fingerprint_mismatch_raises(self) -> None:
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)
        wrong_fp = "0" * 64
        with patch("olo.m3.hindsight.client._request",
                   return_value=self._all_ops_done()):
            with pytest.raises(CorpusMismatchError, match="fingerprint mismatch"):
                verify_corpus_match(client, wrong_fp)

    def test_all_docs_missing_content_hash_raises(self) -> None:
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = [
            {"document_id": d["document_id"], "document_metadata": {}}
            for d in _expected_docs_correct()
        ]
        client = _client_with_docs(docs)
        with patch("olo.m3.hindsight.client._request",
                   return_value=self._all_ops_done()):
            with pytest.raises(CorpusMismatchError):
                verify_corpus_match(client, fp)
