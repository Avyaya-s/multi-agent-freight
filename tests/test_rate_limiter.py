import pytest

from freight_sim.rate_limiter import RateLimitBudgetExhausted, RateLimiter


class FakeClock:
    """Deterministic clock/sleep pair for testing the limiter without real time."""

    def __init__(self):
        self.now = 0.0
        self.total_slept = 0.0

    def now_fn(self) -> float:
        return self.now

    def sleep_fn(self, seconds: float) -> None:
        self.total_slept += seconds
        self.now += seconds


def test_allows_calls_within_all_limits():
    clock = FakeClock()
    limiter = RateLimiter(requests_per_minute=10, requests_per_day=100, tokens_per_minute=1000)
    for _ in range(5):
        waited = limiter.wait_if_needed(100, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn)
        assert waited == 0.0
        limiter.record_request(100, now_fn=clock.now_fn)


def test_waits_when_rpm_exceeded():
    clock = FakeClock()
    limiter = RateLimiter(requests_per_minute=2, requests_per_day=100, tokens_per_minute=10_000)
    for _ in range(2):
        limiter.wait_if_needed(10, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn)
        limiter.record_request(10, now_fn=clock.now_fn)

    waited = limiter.wait_if_needed(10, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn)
    assert waited > 0.0  # had to wait for the oldest request to age out of the 60s window
    assert clock.now >= 60.0


def test_waits_when_tpm_exceeded():
    clock = FakeClock()
    limiter = RateLimiter(requests_per_minute=100, requests_per_day=1000, tokens_per_minute=150)
    limiter.wait_if_needed(100, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn)
    limiter.record_request(100, now_fn=clock.now_fn)

    # 100 + 100 > 150 TPM -> must wait for the first request's tokens to age out
    waited = limiter.wait_if_needed(100, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn)
    assert waited > 0.0


def test_raises_when_daily_budget_exhausted():
    clock = FakeClock()
    limiter = RateLimiter(requests_per_minute=1000, requests_per_day=3, tokens_per_minute=100_000)
    for _ in range(3):
        limiter.wait_if_needed(10, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn)
        limiter.record_request(10, now_fn=clock.now_fn)
        clock.now += 61  # stay clear of the RPM window so only RPD is being tested

    with pytest.raises(RateLimitBudgetExhausted):
        limiter.wait_if_needed(10, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn)


def test_daily_budget_frees_up_after_24h():
    clock = FakeClock()
    limiter = RateLimiter(requests_per_minute=1000, requests_per_day=1, tokens_per_minute=100_000)
    limiter.wait_if_needed(10, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn)
    limiter.record_request(10, now_fn=clock.now_fn)

    clock.now += 86401  # just past the 24h window
    waited = limiter.wait_if_needed(10, sleep_fn=clock.sleep_fn, now_fn=clock.now_fn)
    assert waited == 0.0  # the old request aged out of the daily window


def test_record_headers_parses_groq_style_values():
    limiter = RateLimiter(requests_per_minute=30, requests_per_day=1000, tokens_per_minute=8000)
    status = limiter.record_headers(
        {
            "x-ratelimit-remaining-requests": "29",
            "x-ratelimit-remaining-tokens": "7999",
            "x-ratelimit-reset-requests": "2s",
            "x-ratelimit-reset-tokens": "7500ms",
        }
    )
    assert status.remaining_requests == 29.0
    assert status.remaining_tokens == 7999.0
    assert status.reset_requests_seconds == 2.0
    assert status.reset_tokens_seconds == 7.5
    assert limiter.last_status is status


def test_record_headers_tolerates_missing_or_malformed_values():
    limiter = RateLimiter(requests_per_minute=30, requests_per_day=1000, tokens_per_minute=8000)
    status = limiter.record_headers({})
    assert status.remaining_requests is None
    assert status.reset_requests_seconds is None
