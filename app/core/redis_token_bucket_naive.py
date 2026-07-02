"""
Redis-backed Token Bucket - NAIVE version (Step 3).

This intentionally uses separate GET and SET calls to Redis, which is
NOT atomic. Under concurrent requests from multiple app instances (or
even multiple threads in one instance), this creates a race condition:

    Thread A: GET tokens -> reads 1
    Thread B: GET tokens -> reads 1   (before A has written back)
    Thread A: allows request, SET tokens -> 0
    Thread B: allows request, SET tokens -> 0   (should have been rejected!)

Both threads believed they had the last token and let their request through.
The bucket should have only allowed ONE of them.

This file exists to be measured and then fixed in Step 4 (Redis Lua atomic
script). Do not use this version in anything resembling production - it's
here to make the race condition concrete and visible, not to hide it.
"""

import time

import redis


class RedisTokenBucketNaive:
    def __init__(self, redis_client: redis.Redis, capacity: float, refill_rate: float):
        self.redis = redis_client
        self.capacity = capacity
        self.refill_rate = refill_rate

    def _key(self, client_id: str) -> str:
        return f"ratelimit:naive:{client_id}"

    def allow_request(self, client_id: str) -> bool:
        key = self._key(client_id)
        now = time.time()

        # --- READ ---
        state = self.redis.hgetall(key)

        if state:
            tokens = float(state[b"tokens"])
            last_refill = float(state[b"last_refill"])
        else:
            tokens = self.capacity
            last_refill = now

        # --- COMPUTE (in Python, not in Redis - this is the unsafe part) ---
        elapsed = now - last_refill
        tokens = min(self.capacity, tokens + elapsed * self.refill_rate)

        if tokens >= 1:
            allowed = True
            tokens -= 1
        else:
            allowed = False

        # Simulate realistic network/processing latency between read and write,
        # which widens the race window - in production this gap is smaller but
        # still very real under high concurrency.
        time.sleep(0.005)

        # --- WRITE (separate call - the race window is between READ and here) ---
        self.redis.hset(key, mapping={"tokens": tokens, "last_refill": now})

        return allowed
