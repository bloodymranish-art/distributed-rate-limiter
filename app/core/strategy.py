"""
Strategy pattern for rate limiting algorithms.

The middleware shouldn't need to know whether it's calling Token Bucket
or Sliding Window Counter - it just calls `strategy.allow_request(bucket_id)`
and gets back (allowed, info_for_headers). Each concrete strategy wraps
one algorithm and adapts its specific parameters (capacity/refill_rate for
Token Bucket, limit/window_size for Sliding Window) behind this one interface.

This is what makes the algorithm choice a config decision (Step 5's JSON
file) rather than a code change - adding a third algorithm later just means
adding one more adapter class here.
"""

from abc import ABC, abstractmethod

import redis

from app.core.redis_token_bucket_atomic import RedisTokenBucketAtomic
from app.core.sliding_window_counter import SlidingWindowCounter


class RateLimitStrategy(ABC):
    @abstractmethod
    def allow_request(self, bucket_id: str) -> tuple[bool, dict]:
        """
        Returns (allowed, info) where info is a dict of values useful for
        response headers (e.g. {"remaining": 3, "limit": 5}).
        """
        raise NotImplementedError


class TokenBucketStrategy(RateLimitStrategy):
    def __init__(self, redis_client: redis.Redis, capacity: float, refill_rate: float):
        self.bucket = RedisTokenBucketAtomic(redis_client)
        self.capacity = capacity
        self.refill_rate = refill_rate

    def allow_request(self, bucket_id: str) -> tuple[bool, dict]:
        allowed, remaining = self.bucket.allow_request(
            bucket_id, capacity=self.capacity, refill_rate=self.refill_rate
        )
        return allowed, {
            "limit": self.capacity,
            "remaining": remaining,
            "refill_rate": self.refill_rate,
        }


class SlidingWindowStrategy(RateLimitStrategy):
    def __init__(self, redis_client: redis.Redis, limit: float, window_size_seconds: float):
        self.counter = SlidingWindowCounter(redis_client)
        self.limit = limit
        self.window_size_seconds = window_size_seconds

    def allow_request(self, bucket_id: str) -> tuple[bool, dict]:
        allowed, estimated_count = self.counter.allow_request(
            bucket_id, limit=self.limit, window_size_seconds=self.window_size_seconds
        )
        remaining = max(0, self.limit - estimated_count)
        return allowed, {
            "limit": self.limit,
            "remaining": remaining,
            "window_size_seconds": self.window_size_seconds,
        }


def build_strategy(redis_client: redis.Redis, algorithm: str, params: dict) -> RateLimitStrategy:
    """Factory: turns a config rule's algorithm name + params into a strategy instance."""
    if algorithm == "token_bucket":
        return TokenBucketStrategy(
            redis_client, capacity=params["capacity"], refill_rate=params["refill_rate"]
        )
    elif algorithm == "sliding_window":
        return SlidingWindowStrategy(
            redis_client, limit=params["limit"], window_size_seconds=params["window_size_seconds"]
        )
    else:
        raise ValueError(f"Unknown rate limiting algorithm: {algorithm}")
