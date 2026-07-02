"""
Demonstrates the race condition in RedisTokenBucketNaive.

We set capacity=1 (bucket starts with exactly 1 token) and fire many
concurrent requests at it from multiple threads, simulating multiple
app server instances all hitting the same Redis-backed bucket at once.

With capacity=1, AT MOST 1 request should be allowed. If the naive
GET-then-SET approach is broken, we'll see more than 1 allowed - proving
the race condition concretely, with a real count, not just theory.

Run with: python tests/demonstrate_race_condition.py
"""

import sys
import os
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import redis
from app.core.redis_token_bucket_naive import RedisTokenBucketNaive

NUM_CONCURRENT_REQUESTS = 20
CLIENT_ID = "race-condition-demo-client"


def reset_bucket(r: redis.Redis):
    r.delete(f"ratelimit:naive:{CLIENT_ID}")


def run_trial(r: redis.Redis) -> int:
    """Fire NUM_CONCURRENT_REQUESTS at a bucket with capacity=1.
    Returns how many were allowed (should be 1, will likely be more)."""
    reset_bucket(r)
    bucket = RedisTokenBucketNaive(r, capacity=1, refill_rate=0.001)  # refill_rate tiny, irrelevant here

    results = []
    lock = threading.Lock()

    def worker():
        allowed = bucket.allow_request(CLIENT_ID)
        with lock:
            results.append(allowed)

    threads = [threading.Thread(target=worker) for _ in range(NUM_CONCURRENT_REQUESTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return sum(results)


if __name__ == "__main__":
    r = redis.Redis(host="localhost", port=6379, decode_responses=False)

    print(f"Firing {NUM_CONCURRENT_REQUESTS} concurrent requests at a bucket with capacity=1.")
    print("Expected: exactly 1 allowed. Anything more proves the race condition.\n")

    num_trials = 5
    for trial in range(1, num_trials + 1):
        allowed_count = run_trial(r)
        status = "correct" if allowed_count == 1 else "RACE CONDITION - should be 1"
        print(f"Trial {trial}: {allowed_count} requests allowed out of {NUM_CONCURRENT_REQUESTS}  -> {status}")
