"""
Verifies SlidingWindowCounter stays correct under concurrent requests,
same rigor as verify_atomic_fix.py did for Token Bucket.

Fires 30 concurrent requests at a window with limit=5. Expect exactly 5
allowed, since the Lua script makes each check-and-increment atomic.

Run with: python tests/verify_sliding_window.py
"""

import sys
import os
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import redis
from app.core.sliding_window_counter import SlidingWindowCounter

NUM_CONCURRENT_REQUESTS = 30
LIMIT = 5
WINDOW_SECONDS = 5
CLIENT_ID = "sliding-window-verify-client"


def reset_keys(r: redis.Redis):
    for key in r.scan_iter(f"ratelimit:sliding:{CLIENT_ID}:*"):
        r.delete(key)


def run_trial(r: redis.Redis) -> int:
    reset_keys(r)
    counter = SlidingWindowCounter(r)

    results = []
    lock = threading.Lock()

    def worker():
        allowed, _count = counter.allow_request(CLIENT_ID, limit=LIMIT, window_size_seconds=WINDOW_SECONDS)
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

    print(f"Firing {NUM_CONCURRENT_REQUESTS} concurrent requests at Sliding Window, limit={LIMIT}.")
    print("Expected: exactly 5 allowed, every trial.\n")

    num_trials = 5
    all_correct = True
    for trial in range(1, num_trials + 1):
        allowed_count = run_trial(r)
        status = "correct" if allowed_count == LIMIT else "INCORRECT"
        if allowed_count != LIMIT:
            all_correct = False
        print(f"Trial {trial}: {allowed_count} requests allowed out of {NUM_CONCURRENT_REQUESTS}  -> {status}")

    print()
    print("All trials correct." if all_correct else "Something is wrong - investigate.")
