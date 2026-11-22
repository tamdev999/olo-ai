"""Unit tests for olo.m3.hindsight.client retry and resilience behaviour."""
from __future__ import annotations
import errno
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from olo.m3.hindsight.client import (
    HindsightAPIError,
    HindsightConfig,
    HindsightUnavailable,
    _MAX_ATTEMPTS,
    _jittered_delay,
    _request,
)


def _cfg() -> HindsightConfig:
    return HindsightConfig(host="127.0.0.1", port=19999, timeout_s=5.0)


def _conn_reset():
    err = OSError(errno.ECONNRESET, "Connection reset by peer")
    return urllib.error.URLError(reason=err)


def _conn_refused():
    err = OSError(errno.ECONNREFUSED, "Connection refused")
    return urllib.error.URLError(reason=err)


def _ok_response(body=None):
    import json
    m = MagicMock()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=False)
    m.read.return_value = json.dumps(body or {"ok": True}).encode()
    return m


class TestJitteredDelay:
    def test_increases_with_attempt(self):
        avg0 = sum(_jittered_delay(0) for _ in range(100)) / 100
        avg1 = sum(_jittered_delay(1) for _ in range(100)) / 100
        assert avg0 < avg1

    def test_bounded_below_zero(self):
        for _ in range(50):
            assert _jittered_delay(0) >= 0.0

    def test_max_per_attempt(self):
        for _ in range(50):
            assert _jittered_delay(0) <= 0.75
            assert _jittered_delay(1) <= 1.5


class TestRetryOnEconnreset:
    def test_get_retries_and_succeeds(self):
        ok = _ok_response({"status": "healthy"})
        with patch("hermes_cli.urllib_security.open_credentialed_url",
                   side_effect=[_conn_reset(), ok]) as mock_open, \
             patch("time.sleep") as mock_sleep:
            result = _request(_cfg(), "GET", "/health")
        assert result == {"status": "healthy"}
        assert mock_open.call_count == 2
        assert mock_sleep.call_count == 1
        assert mock_sleep.call_args[0][0] < 1.0

    def test_post_recall_retries(self):
        ok = _ok_response({"results": []})
        with patch("hermes_cli.urllib_security.open_credentialed_url",
                   side_effect=[_conn_reset(), ok]), \
             patch("time.sleep"):
            result = _request(_cfg(), "POST", "/v1/default/banks/b/memories/recall",
                               body={"query": "test", "max_tokens": 1000})
        assert result["results"] == []

    def test_post_with_operation_id_retries(self):
        ok = _ok_response({"operation_id": "abc"})
        body = {"items": [{}], "operation_id": "abc-123", "async": True}
        with patch("hermes_cli.urllib_security.open_credentialed_url",
                   side_effect=[_conn_reset(), ok]), \
             patch("time.sleep"):
            result = _request(_cfg(), "POST", "/v1/default/banks/b/memories", body=body)
        assert "operation_id" in result

    def test_post_without_operation_id_never_retries(self):
        with patch("hermes_cli.urllib_security.open_credentialed_url",
                   side_effect=[_conn_reset()]) as mock_open, \
             patch("time.sleep") as mock_sleep:
            with pytest.raises(HindsightUnavailable) as exc_info:
                _request(_cfg(), "POST", "/v1/default/banks",
                         body={"bank_id": "new-bank"})
        assert mock_open.call_count == 1
        assert mock_sleep.call_count == 0
        assert exc_info.value.retry_count == 0

    def test_delete_never_retries(self):
        with patch("hermes_cli.urllib_security.open_credentialed_url",
                   side_effect=[_conn_reset()]) as mock_open, \
             patch("time.sleep") as mock_sleep:
            with pytest.raises(HindsightUnavailable):
                _request(_cfg(), "DELETE", "/v1/default/banks/b")
        assert mock_open.call_count == 1
        assert mock_sleep.call_count == 0

    def test_exhausted_retries_raises_with_count(self):
        with patch("hermes_cli.urllib_security.open_credentialed_url",
                   side_effect=[_conn_reset()] * _MAX_ATTEMPTS), \
             patch("time.sleep"):
            with pytest.raises(HindsightUnavailable) as exc_info:
                _request(_cfg(), "GET", "/health")
        exc = exc_info.value
        assert exc.retry_count == _MAX_ATTEMPTS - 1
        assert exc.original_error is not None

    def test_original_error_preserved(self):
        first = _conn_reset()
        with patch("hermes_cli.urllib_security.open_credentialed_url",
                   side_effect=[first, _conn_refused(), _conn_reset()]), \
             patch("time.sleep"):
            with pytest.raises(HindsightUnavailable) as exc_info:
                _request(_cfg(), "GET", "/health")
        assert exc_info.value.original_error is first


class TestNoRetryOnAuthErrors:
    def _http_error(self, code):
        return urllib.error.HTTPError(
            url="http://127.0.0.1:19999/health", code=code,
            msg=f"HTTP {code}", hdrs=MagicMock(),
            fp=MagicMock(read=MagicMock(return_value=b"")),
        )

    @pytest.mark.parametrize("code", [401, 403, 400, 422, 404, 409])
    def test_http_error_never_retried(self, code):
        with patch("hermes_cli.urllib_security.open_credentialed_url",
                   side_effect=[self._http_error(code)]) as mock_open, \
             patch("time.sleep") as mock_sleep:
            with pytest.raises(HindsightAPIError) as exc_info:
                _request(_cfg(), "GET", "/v1/default/banks/b/memories/recall")
        assert mock_open.call_count == 1
        assert mock_sleep.call_count == 0
        assert exc_info.value.status_code == code


class TestConnRefused:
    def test_get_retries_conn_refused(self):
        ok = _ok_response()
        with patch("hermes_cli.urllib_security.open_credentialed_url",
                   side_effect=[_conn_refused(), ok]), \
             patch("time.sleep"):
            result = _request(_cfg(), "GET", "/health")
        assert result == {"ok": True}


class TestTimeoutRetry:
    def test_timeout_retried_for_get(self):
        ok = _ok_response()
        with patch("hermes_cli.urllib_security.open_credentialed_url",
                   side_effect=[TimeoutError("timed out"), ok]), \
             patch("time.sleep") as mock_sleep:
            result = _request(_cfg(), "GET", "/health")
        assert result == {"ok": True}
        assert mock_sleep.call_count == 1

    def test_timeout_not_retried_for_non_idempotent_post(self):
        body = {"bank_id": "new"}
        with patch("hermes_cli.urllib_security.open_credentialed_url",
                   side_effect=[TimeoutError("timed out")]) as mock_open, \
             patch("time.sleep") as mock_sleep:
            with pytest.raises(HindsightUnavailable):
                _request(_cfg(), "POST", "/v1/default/banks", body=body)
        assert mock_open.call_count == 1
        assert mock_sleep.call_count == 0


class TestBackoffBounded:
    def test_total_sleep_bounded(self):
        sleep_times = []
        with patch("hermes_cli.urllib_security.open_credentialed_url",
                   side_effect=[_conn_reset()] * _MAX_ATTEMPTS), \
             patch("time.sleep", side_effect=lambda s: sleep_times.append(s)):
            with pytest.raises(HindsightUnavailable):
                _request(_cfg(), "GET", "/health")
        assert len(sleep_times) == _MAX_ATTEMPTS - 1
        assert sum(sleep_times) < 4.0


class TestHindsightConfigValidation:
    def test_external_host_rejected(self):
        with pytest.raises(ValueError, match="loopback"):
            HindsightConfig(host="1.2.3.4", port=18800)

    def test_localhost_accepted(self):
        cfg = HindsightConfig(host="localhost", port=18800)
        assert cfg.host == "localhost"