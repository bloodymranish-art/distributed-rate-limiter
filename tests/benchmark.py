"""
Benchmark suite for the rate limiter: latency, accuracy under concurrency,
and Redis memory footprint, for both Token Bucket and Sliding Window
Counter.

This is what turns "I built a rate limiter" into "I built a rate limiter
and measured exactly when to use which algorithm" - the actual senior
engineering skill, not just implementing what a tutorial shows.

Requires the service to be running (uvicorn app.main:app --port 8080)
and Redis/Memurai reachable.

Run with: python tests/benchmark.py
"""

import asyncio
import statistics
import sys
import time

import httpx
import redis

BASE_URL = "http://localhost:8080"
REDIS_HOST = "localhost"
REDIS_PORT = 6379


def percentile(data: list[float], p: float) -> float:
    data = sorted(data)
    k = (len(data) - 1) * (p / 100)
    f = int(k)
    c = min(f + 1, len(data) - 1)
    if f == c:
        return data[f]
    # Linear interpolation between the two nearest ranks.
    weight = k - f
    return data[f] + weight * (data[c] - data[f])


async def fire_request(client: httpx.AsyncClient, url: str, headers: dict) -> tuple[int, float]:
    start = time.perf_counter()
    resp = await client.get(url, headers=headers)
    elapsed_ms = (time.perf_counter() - start) * 1000
    return resp.status_code, elapsed_ms


async def latency_benchmark(endpoint: str, num_requests: int, concurrency: int, api_key: str) -> dict:
    """
    Fires num_requests total, `concurrency` at a time, using a unique
    API key so we're measuring the algorithm's raw overhead, not getting
    blocked by the rate limit itself (capacity is generous for this key's
    fresh bucket, but we still expect SOME 429s once we exceed it -
    which is fine, we're measuring latency of the check itself either way).
    """
    latencies = []
    status_counts: dict[int, int] = {}

    async with httpx.AsyncClient(timeout=10.0) as client:
        headers = {"X-API-Key": api_key}
        sem = asyncio.Semaphore(concurrency)

        async def bounded_request():
            async with sem:
                status, elapsed_ms = await fire_request(client, f"{BASE_URL}{endpoint}", headers)
                latencies.append(elapsed_ms)
                status_counts[status] = status_counts.get(status, 0) + 1

        await asyncio.gather(*[bounded_request() for _ in range(num_requests)])

    return {
        "p50_ms": round(percentile(latencies, 50), 2),
        "p95_ms": round(percentile(latencies, 95), 2),
        "p99_ms": round(percentile(latencies, 99), 2),
        "min_ms": round(min(latencies), 2),
        "max_ms": round(max(latencies), 2),
        "status_counts": status_counts,
    }


async def accuracy_benchmark(endpoint: str, num_requests: int, expected_allowed: int, api_key: str) -> dict:
    """
    Fires num_requests concurrently at a FRESH bucket (unique api_key) and
    checks whether exactly expected_allowed got through - proving
    correctness under concurrency at a larger scale than the per-algorithm
    unit tests (which used 20-30 requests; this uses more).
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        headers = {"X-API-Key": api_key}
        results = await asyncio.gather(*[
            fire_request(client, f"{BASE_URL}{endpoint}", headers) for _ in range(num_requests)
        ])

    allowed_count = sum(1 for status, _ in results if status == 200)
    return {
        "num_requests": num_requests,
        "expected_allowed": expected_allowed,
        "actual_allowed": allowed_count,
        "correct": allowed_count == expected_allowed,
    }


def memory_benchmark(r: redis.Redis) -> dict:
    """Measures actual Redis memory cost per bucket for each algorithm."""
    # Token bucket: one hash key
    tb_key = "ratelimit:atomic:memcheck-tb"
    r.delete(tb_key)
    r.hset(tb_key, mapping={"tokens": 5.0, "last_refill": time.time()})
    tb_bytes = r.memory_usage(tb_key)

    # Sliding window: one key per window (current + previous exist after 2 requests)
    sw_key_current = "ratelimit:sliding:memcheck-sw:999999"
    r.delete(sw_key_current)
    r.set(sw_key_current, 1)
    sw_bytes_per_window = r.memory_usage(sw_key_current)

    return {
        "token_bucket_bytes_per_client": tb_bytes,
        "sliding_window_bytes_per_window": sw_bytes_per_window,
        "sliding_window_bytes_per_client_worst_case": sw_bytes_per_window * 2,  # current + previous window
    }


async def main():
    print("=" * 70)
    print("RATE LIMITER BENCHMARK REPORT")
    print("=" * 70)

    # Check the server is actually reachable before running anything
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{BASE_URL}/ping")
            r.raise_for_status()
    except Exception as e:
        print(f"\nERROR: could not reach {BASE_URL}/ping - is the server running?")
        print(f"Details: {e}")
        sys.exit(1)

    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=False)

    # --- Latency ---
    print("\n--- LATENCY (100 requests, concurrency=20) ---\n")
    for endpoint, algo_name in [("/api/resource", "Token Bucket"), ("/api/search", "Sliding Window")]:
        result = await latency_benchmark(endpoint, num_requests=100, concurrency=20, api_key=f"bench-latency-{algo_name}")
        print(f"{algo_name} ({endpoint}):")
        print(f"  p50: {result['p50_ms']}ms   p95: {result['p95_ms']}ms   p99: {result['p99_ms']}ms")
        print(f"  min: {result['min_ms']}ms   max: {result['max_ms']}ms")
        print(f"  status codes: {result['status_counts']}")
        print()

    # --- Accuracy under high concurrency ---
    print("--- ACCURACY UNDER CONCURRENCY ---\n")
    tb_result = await accuracy_benchmark("/api/resource", num_requests=50, expected_allowed=5, api_key="bench-accuracy-tb")
    print(f"Token Bucket (/api/resource, capacity=5): fired {tb_result['num_requests']} concurrent requests")
    print(f"  Expected allowed: {tb_result['expected_allowed']}   Actual allowed: {tb_result['actual_allowed']}   -> {'CORRECT' if tb_result['correct'] else 'INCORRECT'}")

    sw_result = await accuracy_benchmark("/api/search", num_requests=50, expected_allowed=10, api_key="bench-accuracy-sw")
    print(f"Sliding Window (/api/search, limit=10): fired {sw_result['num_requests']} concurrent requests")
    print(f"  Expected allowed: {sw_result['expected_allowed']}   Actual allowed: {sw_result['actual_allowed']}   -> {'CORRECT' if sw_result['correct'] else 'INCORRECT'}")

    # --- Memory ---
    print("\n--- REDIS MEMORY FOOTPRINT ---\n")
    mem_result = memory_benchmark(redis_client)
    print(f"Token Bucket:    {mem_result['token_bucket_bytes_per_client']} bytes per client (1 hash key)")
    print(f"Sliding Window:  {mem_result['sliding_window_bytes_per_window']} bytes per window key")
    print(f"                 ~{mem_result['sliding_window_bytes_per_client_worst_case']} bytes per client worst case (current + previous window)")

    print("\n" + "=" * 70)
    print("Done.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
