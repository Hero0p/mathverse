"""Shared retry-with-backoff and circuit-breaker wrapper for outbound API
calls (Gemini, ElevenLabs). Every external call in the codebase goes through
`call_with_resilience` so timeouts/retries/circuit-breaking aren't
reimplemented per service.
"""
import logging
import time
from dataclasses import dataclass, field
from threading import Lock

logger = logging.getLogger("mathverse.resilience")


@dataclass
class CircuitBreaker:
    """A simple per-service circuit breaker: after `failure_threshold`
    consecutive failures, the circuit opens for `reset_after_sec` and calls
    fail fast instead of hammering a downed dependency."""

    name: str
    failure_threshold: int = 5
    reset_after_sec: float = 60.0
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _lock: Lock = field(default_factory=Lock, init=False)

    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self.reset_after_sec:
                # Half-open: allow one probe through.
                self._opened_at = None
                self._failures = 0
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()
                logger.warning("circuit '%s' opened after %d failures", self.name, self._failures)


class CircuitOpenError(RuntimeError):
    pass


# 408 Request Timeout and 429 Too Many Requests are the only 4xx worth
# retrying; every other 4xx is a permanent client-side problem (bad model
# name, revoked key, unpaid account) that will fail identically on retry.
RETRYABLE_CLIENT_STATUSES = {408, 429}


def http_status_of(exc: Exception) -> int | None:
    """Best-effort HTTP status extraction across the exception shapes this
    codebase actually sees: google-genai APIError (`.code`), requests
    HTTPError (`.response.status_code`), and anything exposing
    `.status_code` directly. Returns None for non-HTTP failures such as
    connection resets and DNS errors."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code

    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status

    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status

    return None


def _is_exhausted_daily_quota(exc: Exception) -> bool:
    """A 429 for a per-DAY quota won't clear on a backoff of seconds.

    Gemini reports both kinds as 429; only the per-minute one is worth
    retrying. The quota id distinguishes them, e.g.
    'GenerateRequestsPerDayPerProjectPerModel-FreeTier'.
    """
    text = str(exc)
    return "PerDay" in text or "per day" in text.lower()


def is_retryable(exc: Exception) -> bool:
    """Whether `exc` is worth another attempt. Transport-level failures with
    no HTTP status (timeouts, dropped connections) are retryable; HTTP
    failures are retryable only if 5xx, 408, or 429 -- except a 429 for an
    exhausted daily quota, which no amount of backoff will fix."""
    status = http_status_of(exc)
    if status is None:
        return True
    if status >= 500:
        return True
    if status == 429:
        return not _is_exhausted_daily_quota(exc)
    return status in RETRYABLE_CLIENT_STATUSES


_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, **kwargs) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name=name, **kwargs)
    return _breakers[name]


def call_with_resilience(
    fn,
    *args,
    service: str,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    job_id: str | int | None = None,
    **kwargs,
):
    """Call fn(*args, **kwargs) with exponential backoff retry and a circuit
    breaker keyed by `service`. Raises CircuitOpenError if the breaker is open."""
    breaker = get_breaker(service)
    if breaker.is_open():
        raise CircuitOpenError(f"circuit for '{service}' is open; failing fast")

    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn(*args, **kwargs)
            breaker.record_success()
            return result
        except Exception as exc:  # noqa: BLE001 - classified by is_retryable below
            last_exc = exc
            breaker.record_failure()
            retryable = is_retryable(exc)
            logger.warning(
                "job=%s service=%s attempt=%d/%d failed (%s): %s",
                job_id, service, attempt, max_attempts,
                "retrying" if retryable and attempt < max_attempts else "giving up",
                exc,
            )
            if not retryable:
                # A permanent client error (bad model name, revoked key,
                # unpaid account) will fail identically every time -- retrying
                # only burns quota and delays the caller's fallback path.
                break
            if attempt < max_attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
    raise last_exc
