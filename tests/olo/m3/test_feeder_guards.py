"""Tests for feeder.py defensive guards:
- validate_item_strategy() must raise FeedError on unknown strategy names.
- Known strategies ("structured-no-llm", "prose") are accepted.
- Empty strategy (bank default) is accepted.
- The guard prevents silent fallback to DeepSeek extraction.

Opus audit: "Named retention strategies must be validated before retain.
Unknown strategy names must fail explicitly."
"""
from __future__ import annotations
import pytest
from olo.m3.feeder import FeedError, validate_item_strategy


BANK_ID = "test-bank"


class TestValidateItemStrategy:
    def test_known_structured_no_llm_accepted(self) -> None:
        item = {"document_id": "sig:test", "strategy": "structured-no-llm"}
        # Must not raise
        validate_item_strategy(item, BANK_ID)

    def test_known_prose_accepted(self) -> None:
        item = {"document_id": "sig:test", "strategy": "prose"}
        validate_item_strategy(item, BANK_ID)

    def test_empty_strategy_accepted(self) -> None:
        """Empty strategy means bank default (not a named strategy)."""
        item = {"document_id": "sig:test", "strategy": ""}
        validate_item_strategy(item, BANK_ID)

    def test_missing_strategy_accepted(self) -> None:
        """No strategy key at all means bank default."""
        item = {"document_id": "sig:test"}
        validate_item_strategy(item, BANK_ID)

    def test_none_strategy_accepted(self) -> None:
        """None strategy means bank default."""
        item = {"document_id": "sig:test", "strategy": None}
        validate_item_strategy(item, BANK_ID)

    def test_unknown_strategy_raises(self) -> None:
        """Unknown strategy must fail explicitly, not silently fall back."""
        item = {"document_id": "sig:test", "strategy": "structured"}  # legacy, unknown
        with pytest.raises(FeedError, match="unrecognised strategy"):
            validate_item_strategy(item, BANK_ID)

    def test_arbitrary_unknown_strategy_raises(self) -> None:
        item = {"document_id": "sig:test", "strategy": "some-random-strategy"}
        with pytest.raises(FeedError, match="unrecognised strategy"):
            validate_item_strategy(item, BANK_ID)

    def test_error_message_includes_strategy_name(self) -> None:
        """Error must name the bad strategy so the developer can fix it."""
        item = {"document_id": "sig:doc1", "strategy": "bad-strategy-xyz"}
        with pytest.raises(FeedError) as exc_info:
            validate_item_strategy(item, BANK_ID)
        assert "bad-strategy-xyz" in str(exc_info.value)

    def test_error_message_includes_known_strategies(self) -> None:
        """Error must list valid strategies so developer knows what to use."""
        item = {"document_id": "sig:doc1", "strategy": "oops"}
        with pytest.raises(FeedError) as exc_info:
            validate_item_strategy(item, BANK_ID)
        # Should mention at least one known strategy
        assert "structured-no-llm" in str(exc_info.value) or "Known" in str(exc_info.value)

    def test_error_message_includes_bank_id(self) -> None:
        item = {"document_id": "sig:test", "strategy": "unknown"}
        with pytest.raises(FeedError) as exc_info:
            validate_item_strategy(item, BANK_ID)
        assert BANK_ID in str(exc_info.value)
