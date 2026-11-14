"""Retry policy for Gemini API calls (Phase 2).

Exponential backoff with full jitter, per the GPT proposal recorded in
CONS-20261105-008: the delay before retry number n (1-based) is uniform
in [2**(n-1), 2**n] seconds, i.e. 1-2s, 2-4s, 4-8s, 8-16s, 16-32s, with
at most 5 attempts. A Retry-After value returned by the API wins over
the computed delay.

Only transient failures are retried: HTTP 429 (rate limit) and
500/502/503/504. Client errors (400, 401, 403, 404) fail fast.
Exceptions without a readable status code are retried only when they
look like a transient network failure (connect/timeout); anything else
fails fast, so a bad key or a bad request is never retried five times.
"""
import random
import re
import time

MAX_ATTEMPTS = 5
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
NON_RETRYABLE_STATUS = {400, 401, 403, 404}
_DELAY_RANGES_S = [(1, 2), (2, 4), (4, 8), (8, 16), (16, 32)]

# F1 (M4 P0-02, CONS-180 P2-02): the regex that reads a status out of
# a message must only match the client's own formatted form
# ("app returned HTTP 503 ...") — a bare number, or "HTTP 503" in an
# arbitrary message, is not a status (IncompleteRead(409 bytes read)
# is a byte count, not a 409).
_HTTP_STATUS_RE = re.compile(
    r"^app returned HTTP\s+(400|401|403|404|408|409|410|413|422|429|431|500|502|503|504)\b")
_RETRY_AFTER_RE = re.compile(r"retry-after[:=]\s*(\d+)", re.IGNORECASE)
_TRANSIENT_NETWORK_TYPES = {
    "ConnectError",
    "TimeoutException",
    "ReadTimeout",
    "ConnectTimeout",
    "RemoteProtocolError",
}


def extract_status(error):
    """Best-effort HTTP status code from an exception, or None."""
    code = getattr(error, "code", None)
    if isinstance(code, int):
        return code
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    # F1 (M4 P0-02): a TransportError's http_status is authoritative
    # when it is an int. When it is None the failure was not an HTTP
    # response at all, so only the client's own formatted form
    # ("app returned HTTP 503 ...") may supply a status from the
    # message — a bare number (IncompleteRead(409 bytes read) is a
    # byte count, not a 409) must never be misclassified.
    if hasattr(error, "http_status"):
        http_status = error.http_status
        if isinstance(http_status, int):
            return http_status
    match = _HTTP_STATUS_RE.search(str(error))
    return int(match.group(1)) if match else None


def extract_retry_after_s(error):
    """Retry-After in seconds, from headers or the message, or None."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get("retry-after")
    except Exception:
        value = None
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    match = _RETRY_AFTER_RE.search(str(error))
    return float(match.group(1)) if match else None


def classify_error(error):
    """Return (retryable, status, retry_after_s) for an exception."""
    status = extract_status(error)
    if status is not None:
        if status in RETRYABLE_STATUS:
            return True, status, extract_retry_after_s(error)
        return False, status, None
    if type(error).__name__ in _TRANSIENT_NETWORK_TYPES:
        return True, None, None
    return False, None, None


def backoff_delay_s(attempt, retry_after_s=None, base_delay_s=1):
    """Jittered delay before retry number `attempt` (1-based)."""
    if retry_after_s is not None and retry_after_s > 0:
        return retry_after_s
    low, high = _DELAY_RANGES_S[min(attempt - 1, len(_DELAY_RANGES_S) - 1)]
    return random.uniform(low * base_delay_s, high * base_delay_s)


def call_with_retry(fn, max_attempts=MAX_ATTEMPTS, on_retry=None, base_delay_s=1):
    """Call fn(), retrying transient failures with jittered backoff.

    Returns fn()'s result. Raises the last error when it is not
    retryable or attempts are exhausted. on_retry(attempt, error, delay,
    status) is called before each sleep, for logging; `attempt` is the
    1-based number of the failed attempt.
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_error = e
            retryable, status, retry_after = classify_error(e)
            if not retryable or attempt == max_attempts:
                raise
            delay = backoff_delay_s(attempt, retry_after, base_delay_s)
            if on_retry is not None:
                on_retry(attempt, e, delay, status)
            time.sleep(delay)
    raise last_error  # Unreachable; keeps the control flow explicit.
