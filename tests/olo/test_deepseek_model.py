"""DeepSeek model migration: deepseek-flash in non-thinking mode, one price
basis, reasoning tokens never left uncounted.  No network: the OpenAI client
is replaced by a recorder."""
from __future__ import annotations

import sys
import types

import pytest

from olo import extractor
from olo.extractor import (
    DEFAULT_DEEPSEEK_MODEL,
    Budget,
    _cache_key,
    _deepseek_model_id,
    make_deepseek_model_fn,
)
from olo.m3.hindsight import budget as hs_budget
from olo.m3.hindsight.budget import parse_stats


class _Recorder:
    """Stands in for openai.OpenAI; records each request, returns *usage*."""

    calls: list[dict] = []

    def __init__(self, usage):
        self._usage = usage

    def __call__(self, **client_kwargs):
        rec, usage = self, self._usage

        class _Completions:
            @staticmethod
            def create(**kw):
                rec.calls.append(kw)
                msg = types.SimpleNamespace(content='{"event_type": null}')
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)

        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))


def _fake_openai(monkeypatch, usage) -> _Recorder:
    rec = _Recorder(usage)
    rec.calls = []
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=rec))
    return rec


def _usage(prompt, completion, reasoning=None):
    details = None if reasoning is None else types.SimpleNamespace(reasoning_tokens=reasoning)
    return types.SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                                 completion_tokens_details=details)


def test_requests_deepseek_flash_with_thinking_disabled(monkeypatch) -> None:
    rec = _fake_openai(monkeypatch, _usage(100, 20, 0))
    fn = make_deepseek_model_fn(api_key="sk-test-not-real")
    fn("prompt")
    (call,) = rec.calls
    assert DEFAULT_DEEPSEEK_MODEL == "deepseek-flash"
    assert call["model"] == "deepseek-flash"
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    assert call["response_format"] == {"type": "json_object"}
    assert call["temperature"] == 0 and call["max_tokens"] == 400


def test_reasoning_tokens_are_charged_as_output(monkeypatch) -> None:
    _fake_openai(monkeypatch, _usage(100, 20, 300))
    _, inp, out = make_deepseek_model_fn(api_key="sk-test-not-real")("prompt")
    assert (inp, out) == (100, 320)
    b = Budget()
    b.charge_reply((inp, out))
    assert b.cost_usd == pytest.approx(100 * 0.30 / 1e6 + 320 * 1.20 / 1e6)


def test_model_identity_names_model_and_mode() -> None:
    fn = make_deepseek_model_fn(api_key="sk-test-not-real")
    assert fn.model_id == ("deepseek:deepseek-flash@https://api.deepseek.com/v1:thinking=disabled"
                           ":temperature=0:max_tokens=400")
    assert extractor._default_model_fn.model_id == fn.model_id


def test_cached_answers_of_the_retired_model_are_not_reused() -> None:
    old = "deepseek:deepseek-chat@https://api.deepseek.com/v1:temperature=0:max_tokens=400"
    new = _deepseek_model_id(DEFAULT_DEEPSEEK_MODEL, "https://api.deepseek.com/v1", 0, 400)
    assert _cache_key("p", old) != _cache_key("p", new)


def test_one_price_basis_at_current_peak_list_prices() -> None:
    assert (hs_budget.COST_INPUT_PER_M, hs_budget.COST_OUTPUT_PER_M) == (0.30, 1.20)
    assert (extractor._DEEPSEEK_INPUT_PER_M, extractor._DEEPSEEK_OUTPUT_PER_M) == (0.30, 1.20)


def test_usage_guard_counts_hindsight_reasoning_tokens_as_output() -> None:
    snap = parse_stats({"buckets": [{"tokens": {"input": 1000, "output": 10, "thoughts": 500}},
                                    {"tokens": {"input": 0, "output": 5}}]})
    assert (snap.input_tokens, snap.output_tokens) == (1000, 515)
    assert snap.cost_usd() == pytest.approx(1000 * 0.30 / 1e6 + 515 * 1.20 / 1e6)
