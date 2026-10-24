"""Per-model rate limiting against Groq's real Developer-tier account limits.

Requests/day is the binding constraint here, not requests/minute -- see
CLAUDE.md. Local sliding windows handle pacing *between* calls; the server's
own `x-ratelimit-*` response headers (read after every call via
record_headers) are the source of truth for how much budget is actually
left, since local tracking can't see traffic from outside this process.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


class RateLimitBudgetExhausted(Exception):
    """The daily request budget is used up. Not worth waiting out within a
    session (the reset window is ~a day) -- callers should fail the session
    cleanly, not block."""


@dataclass
class RateLimitStatus:
    remaining_requests: float | None = None
    remaining_tokens: float | None = None
    reset_requests_seconds: float | None = None
    reset_tokens_seconds: float | None = None


def _parse_count(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_duration(value: str | None) -> float | None:
    """Groq (like OpenAI) reports reset windows as strings such as "6ms" or
    "1.5s", not bare numbers -- handle both that and a plain number
    defensively, since the exact format isn't worth guessing beyond this."""
    if value is None:
        return None
    value = value.strip()
    try:
        if value.endswith("ms"):
            return float(value[:-2]) / 1000.0
        if value.endswith("s"):
            return float(value[:-1])
        return float(value)
    except ValueError:
        return None


class RateLimiter:
    """One instance per model. Call `wait_if_needed()` before a request and
    `record_request()` / `record_headers()` after, so later calls see the
    effect of earlier ones."""

    def __init__(self, requests_per_minute: float, requests_per_day: float, tokens_per_minute: float):
        self.requests_per_minute = requests_per_minute
        self.requests_per_day = requests_per_day
        self.tokens_per_minute = tokens_per_minute

        self._minute_requests: deque[float] = deque()
        self._day_requests: deque[float] = deque()
        self._minute_tokens: deque[tuple[float, float]] = deque()  # (timestamp, token_count)

        self.last_status = RateLimitStatus()

    def _prune(self, now: float) -> None:
        while self._minute_requests and now - self._minute_requests[0] > 60:
            self._minute_requests.popleft()
        while self._day_requests and now - self._day_requests[0] > 86400:
            self._day_requests.popleft()
        while self._minute_tokens and now - self._minute_tokens[0][0] > 60:
            self._minute_tokens.popleft()

    def wait_if_needed(self, estimated_tokens: float, sleep_fn=time.sleep, now_fn=time.monotonic) -> float:
        """Blocks until a request is safe to send under the local RPM/TPM
        windows. Returns total seconds waited. Raises RateLimitBudgetExhausted
        immediately if the daily cap is already used up -- that one is not
        worth sleeping through."""
        now = now_fn()
        self._prune(now)

        if len(self._day_requests) >= self.requests_per_day:
            if self._day_requests:
                oldest_age = now - self._day_requests[0]
                detail = f"; oldest request ages out of the window in {86400 - oldest_age:.0f}s"
            else:
                # requests_per_day == 0 (or negative): exhausted by configuration,
                # not by traffic -- there's no oldest request to age out.
                detail = " (requests_per_day is 0)"
            raise RateLimitBudgetExhausted(f"Daily request budget ({self.requests_per_day:.0f}) exhausted{detail}")

        waited = 0.0
        while True:
            now = now_fn()
            self._prune(now)
            wait_for = 0.0
            if len(self._minute_requests) >= self.requests_per_minute:
                wait_for = max(wait_for, 60 - (now - self._minute_requests[0]))
            minute_token_total = sum(tokens for _, tokens in self._minute_tokens)
            if minute_token_total + estimated_tokens > self.tokens_per_minute and self._minute_tokens:
                wait_for = max(wait_for, 60 - (now - self._minute_tokens[0][0]))
            if wait_for <= 0:
                return waited
            sleep_fn(wait_for)
            waited += wait_for

    def record_request(self, actual_tokens: float, now_fn=time.monotonic) -> None:
        now = now_fn()
        self._minute_requests.append(now)
        self._day_requests.append(now)
        self._minute_tokens.append((now, actual_tokens))

    def record_headers(self, headers) -> RateLimitStatus:
        """Parse the response's x-ratelimit-* headers so remaining budget is
        visible (and, longer term, correctable against drift from local
        estimates) -- `headers` is anything with a case-insensitive `.get`,
        e.g. an httpx/openai Headers object or a plain dict."""
        status = RateLimitStatus(
            remaining_requests=_parse_count(headers.get("x-ratelimit-remaining-requests")),
            remaining_tokens=_parse_count(headers.get("x-ratelimit-remaining-tokens")),
            reset_requests_seconds=_parse_duration(headers.get("x-ratelimit-reset-requests")),
            reset_tokens_seconds=_parse_duration(headers.get("x-ratelimit-reset-tokens")),
        )
        self.last_status = status
        return status
