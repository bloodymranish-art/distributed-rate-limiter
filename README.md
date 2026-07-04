# Distributed Rate Limiter (Python / FastAPI)

A production-style distributed rate limiter service, using Redis for shared
state across instances and Lua scripting for atomic check-and-increment
operations. Built step by step to demonstrate the core distributed-systems
challenge in rate limiting: coordinating state across multiple servers
without race conditions.

## Status: Steps 0-8 complete

- [x] Step 0 — Project skeleton, FastAPI app, Docker setup
- [x] Step 1 — In-memory Token Bucket algorithm + unit tests
- [x] Step 2 — Middleware wrapping (429, rate-limit headers)
- [x] Step 3 — Naive Redis-backed state (and the race condition it causes)
- [x] Step 4 — Atomic fix via Redis Lua scripting
- [x] Step 5 — Multi-dimensional limits (per-user/IP/API-key/endpoint config)
- [x] Step 6 — Sliding Window Counter algorithm, swappable via strategy pattern
- [x] Step 7 — Fail-open vs fail-closed behavior
- [x] Step 8 — Load testing + benchmark report (latency, memory, accuracy)
- [ ] Step 9 — Observability / metrics endpoint

## Why this exists

Local, in-process rate limiting breaks the moment you have more than one
server. If a client's requests get distributed across 5 app servers behind a
load balancer, each server only sees a fraction of that client's traffic —
so limits that look correct locally are silently wrong globally. This
project builds a rate limiter that stays correct under that condition.

## The race condition (Step 3 finding)

Moving bucket state into Redis with a naive GET-then-SET pattern
(`app/core/redis_token_bucket_naive.py`) reintroduces a bug that looks
subtle on paper but is dramatic in practice.

With a bucket capacity of 1 token, firing 20 concurrent requests should
allow **exactly 1** through. Measured results from
`tests/demonstrate_race_condition.py`:

```
Trial 1: 16 requests allowed out of 20  -> RACE CONDITION
Trial 2: 20 requests allowed out of 20  -> RACE CONDITION
Trial 3: 20 requests allowed out of 20  -> RACE CONDITION
Trial 4: 20 requests allowed out of 20  -> RACE CONDITION
Trial 5: 20 requests allowed out of 20  -> RACE CONDITION
```

**Why this happens:** each request does a separate `GET` (read current
token count) and `SET` (write new token count) as two distinct Redis
calls. Between one request's GET and its SET, other concurrent requests
can also GET the same (stale) token count, and each one independently
concludes "there's a token available" - because none of them have
written back yet. All of them proceed to allow the request. The bucket
never actually saw "0 tokens" in time to reject anyone.

**The fix (Step 4):** move the read-check-write logic into a single
Redis Lua script, executed with `EVAL`. Redis runs Lua scripts
atomically and single-threaded, so no other client can interleave a
GET between another client's GET and SET - the whole
read-modify-write becomes one indivisible operation.

**Verified fix.** Running the identical 20-concurrent-request test
(`tests/verify_atomic_fix.py`) against `RedisTokenBucketAtomic`
(`app/core/redis_token_bucket_atomic.py`) instead:

```
Trial 1: 1 requests allowed out of 20  -> correct
Trial 2: 1 requests allowed out of 20  -> correct
Trial 3: 1 requests allowed out of 20  -> correct
Trial 4: 1 requests allowed out of 20  -> correct
Trial 5: 1 requests allowed out of 20  -> correct
```

Exactly 1 allowed, every trial, no exceptions - versus up to 20/20
allowed with the naive version. The service's middleware
(`app/middleware/rate_limit.py`) now uses this atomic bucket by
default.

## Multi-dimensional limits (Step 5)

Rate limits are now config-driven (`config/rate_limit_config.json`) and
apply along two independent dimensions:

**Per-endpoint:** `/api/resource` (Token Bucket, capacity=5) and
`/api/search` (Sliding Window, limit=10 per 5s) have independently
configured limits. Exhausting one endpoint's bucket doesn't affect the
other - verified by exhausting `/api/resource` (5 requests, 6th
returns 429) and immediately getting a `200` from `/api/search`.

**Per-identity:** clients are identified by API key (`X-API-Key`
header) if present, otherwise by IP. An API-key client and an
anonymous IP client are tracked as separate identities even on the
same endpoint from the same machine - verified by exhausting the
anonymous IP bucket on `/api/resource`, then immediately getting a
`200` from a request with `X-API-Key: test-key-123` on that same
endpoint.

Adding a new endpoint-specific limit means editing the JSON config,
not touching middleware code - mirrors how real API gateways (Kong,
Envoy, AWS API Gateway) configure per-route limits without
redeploying code.

## Two algorithms, swappable via config (Step 6)

`/api/resource` uses Token Bucket; `/api/search` uses Sliding Window
Counter (`app/core/sliding_window_counter.py`) - chosen purely by
`config/rate_limit_config.json`, with zero if/else branching in the
middleware itself (`app/core/strategy.py` implements the strategy
pattern that makes this possible).

**Why Sliding Window Counter matters:** it's the algorithm most
production systems actually use, because it fixes Fixed Window
Counter's flaw (up to 2x the limit right at a window boundary) at O(1)
memory cost - unlike Sliding Window Log, which is perfectly accurate
but stores every request timestamp.

**Verified correctness under concurrency**
(`tests/verify_sliding_window.py`), same rigor as the Token Bucket
atomicity fix in Step 4 - 30 concurrent requests against a limit of 5:

```
Trial 1: 5 requests allowed out of 30  -> correct
Trial 2: 5 requests allowed out of 30  -> correct
Trial 3: 5 requests allowed out of 30  -> correct
Trial 4: 5 requests allowed out of 30  -> correct
Trial 5: 5 requests allowed out of 30  -> correct
```

**Verified live, side by side, through the actual HTTP server:**
`/api/resource` (Token Bucket, capacity=5) allowed exactly 5 before
returning 429; `/api/search` (Sliding Window, limit=10 per 5s)
allowed exactly 10, with `X-RateLimit-Remaining` counting down
accurately on both, and `X-RateLimit-Algorithm` confirming which
algorithm handled each request.

## Fail-open vs fail-closed (Step 7)

Each endpoint's config specifies a `fail_mode`: what should happen if
Redis itself is unreachable.

- `fail_mode: "open"` - let requests through if Redis is down.
  Prioritizes availability. Used on `/api/resource`.
- `fail_mode: "closed"` - reject requests (503) if Redis is down.
  Prioritizes strict enforcement, at the cost of availability. Used
  on `/api/search`.

Both were verified against a **real Redis outage** (not simulated):
Redis was killed mid-session, then both endpoints were hit, then
Redis was restarted.

```
Redis killed.

GET /api/resource (fail_mode=open):
  200 OK, X-RateLimit-Status: degraded-fail-open
  -> request succeeded despite Redis being down, and said so explicitly

GET /api/search (fail_mode=closed):
  503 Service Unavailable, X-RateLimit-Status: degraded-fail-closed
  -> request rejected rather than risk unprotected traffic

Redis restarted.

GET /api/search again:
  200 OK, back to normal
  -> recovered automatically, no restart or manual reset needed
```

A degraded response is never silent - both paths set an
`X-RateLimit-Status` header so a caller (or an on-call engineer) can
tell protection is currently down, rather than just seeing normal
-looking traffic during an outage.

## Benchmark report (Step 8)

Measured with `tests/benchmark.py` against the live HTTP service (not
the algorithms in isolation), covering latency, accuracy under
concurrency, and Redis memory cost. Run yourself with:

```bash
python tests/benchmark.py
```

*(Requires the server and Redis running. Numbers below are from a
single local run - absolute latency is hardware/environment
dependent, but the comparison between algorithms and the correctness
results are the meaningful takeaways.)*

### Latency (100 requests, concurrency=20)

| Algorithm       | Endpoint       | p50    | p95    | p99     | min   | max     |
|-----------------|----------------|--------|--------|---------|-------|---------|
| Token Bucket    | /api/resource  | 31.8ms | 97.4ms | 131.2ms | 9.8ms | 144.0ms |
| Sliding Window  | /api/search    | 31.4ms | 99.8ms | 121.1ms | 10.7ms| 190.9ms |

Both algorithms perform comparably - expected, since in both cases the
dominant cost is one round trip executing a Redis Lua script, not the
O(1) arithmetic each script does. The algorithm choice is a
correctness/memory tradeoff (see below), not a latency one.

### Accuracy under concurrency (50 concurrent requests, fresh bucket)

| Algorithm       | Endpoint       | Expected allowed | Actual allowed | Result  |
|-----------------|----------------|-------------------|-----------------|---------|
| Token Bucket    | /api/resource  | 5                 | 5               | CORRECT |
| Sliding Window  | /api/search    | 10                | 10              | CORRECT |

Both hold exactly at their configured limit under real concurrent HTTP
load - not just in the isolated unit tests from Steps 4 and 6, but
through the full HTTP + middleware + Redis path.

### Redis memory footprint (per client)

| Algorithm       | Storage                          | Bytes per client            |
|-----------------|-----------------------------------|------------------------------|
| Token Bucket    | 1 Redis hash key                 | 136 bytes                   |
| Sliding Window  | 2 window keys (current+previous) | ~176 bytes (88 bytes each)  |

Sliding Window costs slightly more memory per client (needs two window
counters instead of one bucket), which is the real-world price of its
better boundary-accuracy - a concrete number to cite instead of a
hand-wavy "more memory."

## Stack

- **Language:** Python 3.12
- **Web framework:** FastAPI + Uvicorn
- **Shared state:** Redis (introduced in Step 3)
- **Testing:** pytest + httpx
- **Containerization:** Docker + Docker Compose

## Running locally (without Docker)

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

Then in another terminal:
```bash
curl http://localhost:8080/ping
curl http://localhost:8080/api/resource
```

Or open http://localhost:8080/docs for interactive API docs (free with FastAPI).

## Running with Docker Compose

```bash
docker compose up --build
```

This starts both the service (port 8080) and Redis (port 6379).

## Project layout

```
rate-limiter-py/
├── requirements.txt
├── docker-compose.yml           # App + Redis
├── docker/Dockerfile             # Service container
├── config/
│   └── rate_limit_config.json    # Per-endpoint algorithm + limits + fail_mode
├── app/
│   ├── main.py                   # Entry point, route definitions
│   ├── middleware/
│   │   └── rate_limit.py         # Ties config + strategy + client ID together
│   └── core/
│       ├── token_bucket.py               # Step 1: in-memory algorithm
│       ├── redis_token_bucket_naive.py   # Step 3: deliberately unsafe (teaching artifact)
│       ├── redis_token_bucket_atomic.py  # Step 4: atomic fix via Lua
│       ├── sliding_window_counter.py     # Step 6: second algorithm
│       ├── strategy.py                   # Step 6: strategy pattern
│       └── rate_limit_config.py          # Step 5: config loader
└── tests/
    ├── test_token_bucket.py           # Step 1: unit tests
    ├── demonstrate_race_condition.py  # Step 3: proves the bug
    ├── verify_atomic_fix.py           # Step 4: proves the fix
    ├── verify_sliding_window.py       # Step 6: concurrency test
    └── benchmark.py                   # Step 8: latency/accuracy/memory report
```
