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
    _SUCCESSFUL_OP_STATUSES,
    _PENDING_OP_STATUSES,
    _OPS_PAGE_SIZE,
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
    """Build the operations endpoint response dict for patching.
    
    Uses "operations" key to match the actual Hindsight 1.2.1 response schema.
    """
    return {"operations": ops, "total": len(ops)}


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
    """Unit tests for _check_no_pending_operations (P0.2 final corrections)."""

    def _make_client(self) -> HindsightClient:
        cfg = HindsightConfig(host="127.0.0.1", port=19999)
        return HindsightClient(cfg)

    # ── Page size limit ────────────────────────────────────────────────────

    def test_page_size_is_at_most_100(self) -> None:
        """_OPS_PAGE_SIZE must be ≤100 (Hindsight 1.2.1 returns HTTP 422 for >100)."""
        assert _OPS_PAGE_SIZE <= 100, (
            f"_OPS_PAGE_SIZE={_OPS_PAGE_SIZE} exceeds the pinned server limit of 100; "
            "requests for >100 return HTTP 422."
        )

    # ── Basic terminal/non-terminal classification ─────────────────────────

    def test_empty_ops_list_passes(self) -> None:
        """Zero operations (new bank, never retained) — passes immediately."""
        client = self._make_client()
        with patch("olo.m3.hindsight.client._request",
                   return_value={"operations": [], "total": 0}):
            count = _check_no_pending_operations(client, "test-bank")
        assert count == 0

    def test_all_completed_ops_pass(self) -> None:
        """All operations in known-successful status — passes."""
        client = self._make_client()
        ops = [_op("completed"), _op("succeeded"), _op("done")]
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            count = _check_no_pending_operations(client, "test-bank")
        assert count == 3

    def test_pending_op_raises(self) -> None:
        """A single pending operation causes rejection."""
        client = self._make_client()
        ops = [_op("completed"), _op("pending"), _op("completed")]
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            with pytest.raises(CorpusMismatchError, match="pending"):
                _check_no_pending_operations(client, "test-bank")

    def test_processing_op_raises(self) -> None:
        """A processing operation causes rejection."""
        client = self._make_client()
        ops = [_op("completed"), _op("processing", "retain")]
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            with pytest.raises(CorpusMismatchError, match="pending"):
                _check_no_pending_operations(client, "test-bank")

    def test_p0_2_root_cause_two_pending_plus_newer_completed(self) -> None:
        """P0.2 root cause: two pending ops hidden behind a newer completed one.

        Replicates pending_live2.py P2: a newer completed re-retain is at
        offset 0; two older pending consolidation operations are on the same page.
        With limit=100, all three are visible and the verifier correctly rejects.
        """
        client = self._make_client()
        ops = [
            _op("completed", "retain"),          # newest
            _op("pending", "consolidation"),
            _op("pending", "consolidation"),
        ]
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            with pytest.raises(CorpusMismatchError, match="pending"):
                _check_no_pending_operations(client, "test-bank")

    # ── Failed operations ──────────────────────────────────────────────────

    def test_failed_op_raises(self) -> None:
        """P5: A 'failed' consolidation operation causes rejection.

        A failed consolidation can leave observations incomplete.
        The verifier conservatively blocks certification.
        """
        client = self._make_client()
        ops = [_op("failed", "consolidation"), _op("completed")]
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            with pytest.raises(CorpusMismatchError, match="failed"):
                _check_no_pending_operations(client, "test-bank")

    def test_error_op_raises(self) -> None:
        """An 'error' status operation causes rejection."""
        client = self._make_client()
        ops = [_op("error", "consolidation"), _op("completed")]
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            with pytest.raises(CorpusMismatchError, match="failed"):
                _check_no_pending_operations(client, "test-bank")

    def test_failed_message_mentions_limitation(self) -> None:
        """Failed op error message must mention the LIMITATION of the check."""
        client = self._make_client()
        ops = [_op("failed", "consolidation"), _op("completed")]
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            try:
                _check_no_pending_operations(client, "test-bank")
                assert False, "Should have raised"
            except CorpusMismatchError as exc:
                assert "LIMITATION" in str(exc), (
                    "Failed-op rejection must document the LIMITATION so operators "
                    "understand why a repaired bank may still be rejected."
                )

    def test_failed_message_distinct_from_pending_message(self) -> None:
        """Failed and pending ops produce distinct error messages."""
        client = self._make_client()
        failed_ops = [_op("failed"), _op("completed")]
        pending_ops = [_op("pending"), _op("completed")]

        failed_msg = ""
        pending_msg = ""

        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(failed_ops)):
            try:
                _check_no_pending_operations(client, "test-bank")
            except CorpusMismatchError as e:
                failed_msg = str(e)

        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(pending_ops)):
            try:
                _check_no_pending_operations(client, "test-bank")
            except CorpusMismatchError as e:
                pending_msg = str(e)

        assert "LIMITATION" in failed_msg, "Failed msg must include LIMITATION"
        assert "LIMITATION" not in pending_msg, "Pending msg must not include LIMITATION"
        assert failed_msg != pending_msg, "Messages must differ"

    # ── Unknown statuses ───────────────────────────────────────────────────

    def test_unknown_status_raises(self) -> None:
        """Unknown status (not in successful set) → fail closed."""
        client = self._make_client()
        ops = [_op("queued"), _op("completed")]
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            with pytest.raises(CorpusMismatchError):
                _check_no_pending_operations(client, "test-bank")

    def test_missing_status_raises(self) -> None:
        """Operation with no status key → fail closed."""
        client = self._make_client()
        ops = [{"task_type": "retain", "id": "op-nostatus"}]
        with patch("olo.m3.hindsight.client._request",
                   return_value=_ops_response(ops)):
            with pytest.raises(CorpusMismatchError):
                _check_no_pending_operations(client, "test-bank")

    # ── Pagination correctness (P3/P4 scenarios) ───────────────────────────

    def test_correct_bank_260_ops_passes(self) -> None:
        """P3: >100 completed ops across 3 pages — must PASS.

        With _OPS_PAGE_SIZE=100, three pages (100+100+60) are fetched.
        All 260 are 'completed' → passes.
        """
        client = self._make_client()
        all_ops = [_op("completed") for _ in range(260)]

        def mock_request(cfg, method, path, body=None, params=None):
            offset = int((params or {}).get("offset", "0"))
            limit = int((params or {}).get("limit", "100"))
            page = all_ops[offset:offset + limit]
            return {"operations": page, "total": 260}

        with patch("olo.m3.hindsight.client._request", side_effect=mock_request):
            count = _check_no_pending_operations(client, "test-bank")
        assert count == 260

    def test_pending_on_second_page_rejects(self) -> None:
        """P4: pending op at offset>100 (behind newer completed ops) → REJECTED.

        Replicates pending_live2.py P4: a pending consolidation is older than
        >100 newer completed quick-ops and requires pagination to detect.
        """
        client = self._make_client()
        page1 = [_op("completed") for _ in range(100)]
        page2 = [_op("completed") for _ in range(30)] + [_op("pending", "consolidation")]

        def mock_request(cfg, method, path, body=None, params=None):
            offset = int((params or {}).get("offset", "0"))
            if offset == 0:
                return {"operations": page1, "total": 131}
            return {"operations": page2, "total": 131}

        with patch("olo.m3.hindsight.client._request", side_effect=mock_request):
            with pytest.raises(CorpusMismatchError, match="pending"):
                _check_no_pending_operations(client, "test-bank")

    def test_failed_on_second_page_rejects(self) -> None:
        """Failed op discovered on page 2 via pagination → REJECTED."""
        client = self._make_client()
        page1 = [_op("completed") for _ in range(100)]
        page2 = [_op("failed", "consolidation"), _op("completed")]

        def mock_request(cfg, method, path, body=None, params=None):
            offset = int((params or {}).get("offset", "0"))
            if offset == 0:
                return {"operations": page1, "total": 102}
            return {"operations": page2, "total": 102}

        with patch("olo.m3.hindsight.client._request", side_effect=mock_request):
            with pytest.raises(CorpusMismatchError, match="failed"):
                _check_no_pending_operations(client, "test-bank")

    # ── API error handling ─────────────────────────────────────────────────

    def test_unreachable_endpoint_raises(self) -> None:
        """Network error → CorpusMismatchError, never swallowed."""
        from olo.m3.hindsight.client import HindsightUnavailable
        client = self._make_client()
        with patch("olo.m3.hindsight.client._request",
                   side_effect=HindsightUnavailable("Connection refused")):
            with pytest.raises(CorpusMismatchError, match="unreachable"):
                _check_no_pending_operations(client, "test-bank")

    def test_http_422_raises(self) -> None:
        """HTTP 422 (from server rejecting limit>100) → CorpusMismatchError."""
        from olo.m3.hindsight.client import HindsightAPIError
        client = self._make_client()
        err = HindsightAPIError("Unprocessable Entity", status_code=422)
        with patch("olo.m3.hindsight.client._request", side_effect=err):
            with pytest.raises(CorpusMismatchError, match="HTTP 422"):
                _check_no_pending_operations(client, "test-bank")

    def test_http_401_raises(self) -> None:
        """HTTP 401 Unauthorized → CorpusMismatchError."""
        from olo.m3.hindsight.client import HindsightAPIError
        client = self._make_client()
        err = HindsightAPIError("Unauthorized", status_code=401)
        with patch("olo.m3.hindsight.client._request", side_effect=err):
            with pytest.raises(CorpusMismatchError, match="HTTP 401"):
                _check_no_pending_operations(client, "test-bank")

    def test_malformed_response_no_ops_key_raises(self) -> None:
        """Response missing 'operations'/'items' key with total > 0 → rejected."""
        client = self._make_client()
        with patch("olo.m3.hindsight.client._request",
                   return_value={"total": 5}):
            with pytest.raises(CorpusMismatchError):
                _check_no_pending_operations(client, "test-bank")

    def test_non_dict_response_raises(self) -> None:
        """Non-dict response → rejected."""
        client = self._make_client()
        with patch("olo.m3.hindsight.client._request",
                   return_value=[{"status": "completed"}]):
            with pytest.raises(CorpusMismatchError, match="malformed"):
                _check_no_pending_operations(client, "test-bank")

    def test_empty_ops_key_present_passes(self) -> None:
        """'operations' key present but empty list → passes."""
        client = self._make_client()
        with patch("olo.m3.hindsight.client._request",
                   return_value={"operations": [], "total": 0}):
            count = _check_no_pending_operations(client, "test-bank")
        assert count == 0

    # ── Status set constants ───────────────────────────────────────────────

    def test_successful_statuses_set(self) -> None:
        """Successful statuses are completed, succeeded, done."""
        for s in ("completed", "succeeded", "done"):
            assert s in _SUCCESSFUL_OP_STATUSES

    def test_failed_not_in_successful_statuses(self) -> None:
        """'failed' and 'error' are NOT in successful set."""
        assert "failed" not in _SUCCESSFUL_OP_STATUSES
        assert "error" not in _SUCCESSFUL_OP_STATUSES

    def test_pending_and_processing_in_pending_set(self) -> None:
        """'pending' and 'processing' are in _PENDING_OP_STATUSES."""
        assert "pending" in _PENDING_OP_STATUSES
        assert "processing" in _PENDING_OP_STATUSES


# ---------------------------------------------------------------------------
# verify_corpus_match — operations integration
# ---------------------------------------------------------------------------

class TestVerifyCorpusMatchOperations:
    """verify_corpus_match rejects banks with pending operations."""

    def _all_ops_done(self) -> dict:
        return {"operations": [], "total": 0}

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

        ops_with_pending = {"operations": [_op("pending", "consolidation")], "total": 1}
        with patch("olo.m3.hindsight.client._request",
                   return_value=ops_with_pending):
            with pytest.raises(CorpusMismatchError, match="pending"):
                verify_corpus_match(client, fp)

    def test_p0_2_two_pending_behind_completed_rejected(self) -> None:
        """P0.2 exact scenario: newest op is completed; two older ones are pending.

        Replicates Opus's pending_live2.py P2 scenario.
        """
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)

        all_ops = {
            "operations": [
                _op("completed", "retain"),
                _op("pending", "consolidation"),
                _op("pending", "consolidation"),
            ],
            "total": 3,
        }
        with patch("olo.m3.hindsight.client._request", return_value=all_ops):
            with pytest.raises(CorpusMismatchError, match="pending"):
                verify_corpus_match(client, fp)

    def test_processing_op_prevents_verification(self) -> None:
        """Processing operation → rejected."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)

        ops_processing = {
            "operations": [_op("processing", "consolidation")], "total": 1
        }
        with patch("olo.m3.hindsight.client._request",
                   return_value=ops_processing):
            with pytest.raises(CorpusMismatchError, match="pending"):
                verify_corpus_match(client, fp)

    def test_failed_op_prevents_verification(self) -> None:
        """P5: failed consolidation → rejected (conservative rule)."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)

        ops_failed = {"operations": [_op("failed", "consolidation")], "total": 1}
        with patch("olo.m3.hindsight.client._request", return_value=ops_failed):
            with pytest.raises(CorpusMismatchError, match="failed"):
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
        """Race guard: ops clear before doc enumeration, new pending after → rejected."""
        from olo.m3.synthetic.generator import corpus_fingerprint, generate_timeline
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        docs = _expected_docs_correct()
        client = _client_with_docs(docs)

        all_clear = {"operations": [], "total": 0}
        new_pending = {"operations": [_op("pending", "consolidation")], "total": 1}

        call_count = {"n": 0}

        def _side_effect(cfg, method, path, body=None, params=None):
            call_count["n"] += 1
            if "operations" in path:
                if call_count["n"] == 1:
                    return all_clear
                else:
                    return new_pending
            return {"operations": [], "total": 0}

        with patch("olo.m3.hindsight.client._request", side_effect=_side_effect):
            with pytest.raises(CorpusMismatchError, match="pending"):
                verify_corpus_match(client, fp)


# ---------------------------------------------------------------------------
# verify_corpus_match — existing document-integrity tests (unchanged)
# ---------------------------------------------------------------------------

class TestCorpusVerification:
    """Document-level verification: content hashes, missing/extra docs, fingerprint."""

    def _all_ops_done(self) -> dict:
        return {"operations": [], "total": 0}

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
