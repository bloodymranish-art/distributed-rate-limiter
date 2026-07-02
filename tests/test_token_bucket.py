"""
Unit tests for TokenBucket.

Run with: pytest tests/test_token_bucket.py -v

Each function starting with `test_` is a separate test case.
pytest finds them automatically and reports pass/fail for each.
"""

import time
import sys
import os

# Allow importing from app/ without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.token_bucket import TokenBucket


def test_starts_full():
    """A fresh bucket should start at full capacity."""
    bucket = TokenBucket(capacity=10, refill_rate=1)
    assert bucket.available_tokens() == 10


def test_allows_request_when_tokens_available():
    bucket = TokenBucket(capacity=5, refill_rate=1)
    assert bucket.allow_request() is True


def test_consumes_a_token_per_request():
    bucket = TokenBucket(capacity=5, refill_rate=1)
    bucket.allow_request()
    # 1 token consumed, roughly 4 should remain (allowing tiny time-based refill)
    assert 3.9 <= bucket.available_tokens() <= 4.1


def test_rejects_when_bucket_empty():
    """If we drain all tokens immediately, the next request should be rejected."""
    bucket = TokenBucket(capacity=3, refill_rate=0.001)  # refill_rate tiny so it won't top up mid-test
    assert bucket.allow_request() is True
    assert bucket.allow_request() is True
    assert bucket.allow_request() is True
    # Bucket should now be empty (or nearly, minus negligible refill)
    assert bucket.allow_request() is False


def test_allows_burst_up_to_capacity():
    """Token bucket's key feature: it should allow a burst up to `capacity`,
    not just a steady trickle."""
    bucket = TokenBucket(capacity=10, refill_rate=0.001)
    results = [bucket.allow_request() for _ in range(10)]
    assert all(results), "All 10 requests within capacity should be allowed"
    # The 11th request should fail since the bucket is now empty
    assert bucket.allow_request() is False


def test_refills_over_time():
    """After waiting, tokens should regenerate according to refill_rate."""
    bucket = TokenBucket(capacity=5, refill_rate=10, initial_tokens=0)  # 10 tokens/sec
    assert bucket.allow_request() is False  # starts empty

    time.sleep(0.2)  # ~2 tokens should have refilled (10 tokens/sec * 0.2s)
    tokens = bucket.available_tokens()
    assert 1.5 <= tokens <= 2.5, f"expected ~2 tokens after 0.2s, got {tokens}"


def test_does_not_refill_past_capacity():
    """Tokens should never exceed the bucket's capacity, even after a long wait."""
    bucket = TokenBucket(capacity=5, refill_rate=100, initial_tokens=0)
    time.sleep(0.5)  # would add ~50 tokens if uncapped
    assert bucket.available_tokens() == 5


def test_boundary_exact_zero_tokens():
    """Edge case: exactly 0 tokens remaining should reject, not allow."""
    bucket = TokenBucket(capacity=1, refill_rate=0.0001, initial_tokens=0)
    assert bucket.allow_request() is False


def test_invalid_capacity_raises():
    """Constructing with capacity <= 0 should fail fast with a clear error."""
    try:
        TokenBucket(capacity=0, refill_rate=1)
        assert False, "Expected ValueError for capacity=0"
    except ValueError:
        pass


def test_invalid_refill_rate_raises():
    try:
        TokenBucket(capacity=5, refill_rate=-1)
        assert False, "Expected ValueError for negative refill_rate"
    except ValueError:
        pass
