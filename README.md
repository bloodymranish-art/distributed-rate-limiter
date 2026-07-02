# Distributed Rate Limiter (Python / FastAPI)

A production-style distributed rate limiter service, using Redis for shared
state across instances and Lua scripting for atomic check-and-increment
operations. Built step by step to demonstrate the core distributed-systems
challenge in rate limiting: coordinating state across multiple servers
without race conditions.

## Status: Step 0 complete

- [x] Step 0 — Project skeleton, FastAPI app, Docker setup
- [x] Step 1 — In-memory Token Bucket algorithm + unit tests
- [x] Step 2 — Middleware wrapping (429, rate-limit headers)
- [x] Step 3 — Naive Redis-backed state (and the race condition it causes)
- [x] Step 4 — Atomic fix via Redis Lua scripting
- [x] Step 5 — Multi-dimensional limits (per-user/IP/API-key/endpoint config)
- [ ] Step 6 — Sliding Window Counter algorithm, swappable via strategy pattern
- [ ] Step 7 — Fail-open vs fail-closed behavior
- [ ] Step 8 — Load testing + benchmark report (latency, memory, accuracy)
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

**Per-endpoint:** `/api/resource` (capacity=5) and `/api/search`
(capacity=20) have independently configured limits. Exhausting one
endpoint's bucket doesn't affect the other - verified by exhausting
`/api/resource` (5 requests, 6th returns 429) and immediately getting
a `200` from `/api/search`.

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
├── docker-compose.yml     # App + Redis
├── docker/Dockerfile       # Service container
├── app/
│   ├── __init__.py
│   └── main.py             # Entry point
└── tests/                  # Unit tests
```
