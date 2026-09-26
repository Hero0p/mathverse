"""Retry classification: permanent client errors must not be retried, since
retrying only burns API quota and delays the caller's fallback path."""
import pytest

from app.services import resilience
from app.services.resilience import (
    CircuitOpenError,
    call_with_resilience,
    http_status_of,
    is_retryable,
)


class _GenaiStyleError(Exception):
    """Mirrors google.genai APIError, which carries an int `.code`."""

    def __init__(self, code):
        super().__init__(f"{code} error")
        self.code = code


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class _RequestsStyleError(Exception):
    """Mirrors requests.HTTPError, which carries `.response.status_code`."""

    def __init__(self, status_code):
        super().__init__(f"{status_code} error")
        self.response = _Response(status_code)


@pytest.fixture(autouse=True)
def _reset_breakers():
    resilience._breakers.clear()
    yield
    resilience._breakers.clear()


def test_http_status_of_genai_style():
    assert http_status_of(_GenaiStyleError(404)) == 404


def test_http_status_of_requests_style():
    assert http_status_of(_RequestsStyleError(402)) == 402


def test_http_status_of_returns_none_for_transport_errors():
    assert http_status_of(ConnectionResetError("connection reset")) is None


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
def test_permanent_client_errors_are_not_retryable(status):
    assert is_retryable(_GenaiStyleError(status)) is False


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_errors_are_retryable(status):
    assert is_retryable(_GenaiStyleError(status)) is True


def test_transport_errors_with_no_status_are_retryable():
    assert is_retryable(TimeoutError("timed out")) is True


def test_per_minute_rate_limit_is_retryable():
    exc = _GenaiStyleError(429)
    exc.args = ("429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerMinutePerProject",)
    assert is_retryable(exc) is True


def test_exhausted_daily_quota_is_not_retryable():
    """Backing off a few seconds cannot fix a per-day quota; retrying just
    burns two more calls against an already-empty budget."""
    exc = _GenaiStyleError(429)
    exc.args = (
        "429 RESOURCE_EXHAUSTED quotaId: "
        "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
    )
    assert is_retryable(exc) is False


def test_daily_quota_error_is_attempted_only_once():
    calls = []

    def _quota_exhausted():
        calls.append(1)
        exc = _GenaiStyleError(429)
        exc.args = ("429 quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier",)
        raise exc

    with pytest.raises(_GenaiStyleError):
        call_with_resilience(
            _quota_exhausted, service="test-daily-quota", max_attempts=3, base_delay=0
        )
    assert len(calls) == 1


def test_permanent_error_is_attempted_exactly_once():
    """The bug this guards: a 404 for a retired model name previously
    consumed all 3 attempts (and 3x the quota) before failing."""
    calls = []

    def _always_404():
        calls.append(1)
        raise _GenaiStyleError(404)

    with pytest.raises(_GenaiStyleError):
        call_with_resilience(_always_404, service="test-permanent", max_attempts=3, base_delay=0)

    assert len(calls) == 1


def test_transient_error_exhausts_all_attempts():
    calls = []

    def _always_503():
        calls.append(1)
        raise _GenaiStyleError(503)

    with pytest.raises(_GenaiStyleError):
        call_with_resilience(_always_503, service="test-transient", max_attempts=3, base_delay=0)

    assert len(calls) == 3


def test_retry_succeeds_after_transient_failure():
    calls = []

    def _flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _GenaiStyleError(503)
        return "recovered"

    result = call_with_resilience(_flaky, service="test-flaky", max_attempts=3, base_delay=0)
    assert result == "recovered"
    assert len(calls) == 3


def test_success_resets_breaker_failure_count():
    breaker = resilience.get_breaker("test-reset")
    breaker.record_failure()
    breaker.record_failure()
    call_with_resilience(lambda: "ok", service="test-reset", base_delay=0)
    assert breaker._failures == 0


def test_circuit_opens_after_threshold_and_fails_fast():
    breaker = resilience.get_breaker("test-open")
    for _ in range(breaker.failure_threshold):
        breaker.record_failure()

    assert breaker.is_open() is True
    with pytest.raises(CircuitOpenError):
        call_with_resilience(lambda: "never runs", service="test-open", base_delay=0)
