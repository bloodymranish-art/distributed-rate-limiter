"""
Proves the Lua-script fix (RedisTokenBucketAtomic) resolves the race
condition demonstrated in demonstrate_race_condition.py.

Same setup: capacity=1, 20 concurrent requests, should allow exactly 1.
This time, it should actually BE exactly 1 every trial - because Redis
executes the Lua script atomically, no request can read stale state.

Run with: python tests/verify_atomic_fix.py
"""

import sys
import os
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import redis
from app.core.redis_token_bucket_atomic import RedisTokenBucketAtomic

NUM_CONCURRENT_REQUESTS = 20
CLIENT_ID = "atomic-fix-verify-client"


def reset_bucket(r: redis.Redis):
    r.delete(f"ratelimit:atomic:{CLIENT_ID}")


def run_trial(r: redis.Redis) -> int:
    reset_bucket(r)
    bucket = RedisTokenBucketAtomic(r)

    results = []
    lock = threading.Lock()

    def worker():
        allowed, _remaining = bucket.allow_request(CLIENT_ID, capacity=1, refill_rate=0.001)
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

    print(f"Firing {NUM_CONCURRENT_REQUESTS} concurrent requests at the ATOMIC bucket, capacity=1.")
    print("Expected: exactly 1 allowed, every single trial.\n")

    num_trials = 5
    all_correct = True
    for trial in range(1, num_trials + 1):
        allowed_count = run_trial(r)
        status = "correct" if allowed_count == 1 else "STILL BROKEN"
        if allowed_count != 1:
            all_correct = False
        print(f"Trial {trial}: {allowed_count} requests allowed out of {NUM_CONCURRENT_REQUESTS}  -> {status}")

    print()
    if all_correct:
        print("All trials correct. The Lua script closed the race condition.")
    else:
        print("Something is still wrong - investigate before moving on.")
