"""
Redis-backed Token Bucket - ATOMIC version (Step 4).

Fixes the race condition from Step 3 (see redis_token_bucket_naive.py)
by moving the entire read-check-write sequence into a single Redis Lua
script, executed via EVAL.

Why this works: Redis executes Lua scripts atomically and single-threaded.
While one client's script is running, no other client's command (including
another EVAL) can interleave. So the "GET tokens, compute, decide, SET
tokens" sequence becomes one indivisible unit - there is no window for a
second request to read stale data, because nothing else can run until
this script finishes.

This is the same technique production rate limiters (and things like
distributed locks) use to get atomicity without needing Redis transactions
(MULTI/EXEC) or external locking.
"""

import time

import redis


# KEYS[1] = the Redis key for this client's bucket
# ARGV[1] = capacity
# ARGV[2] = refill_rate (tokens per second)
# ARGV[3] = current unix timestamp (passed in from Python, not read via Lua's
#           os.time(), since we want sub-second precision and consistent time
#           across the whole script execution)
#
# Returns: {allowed (1 or 0), tokens_remaining} so callers can set accurate
# rate-limit headers without a second round trip.
TOKEN_BUCKET_LUA_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local state = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(state[1])
local last_refill = tonumber(state[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
end

-- refill based on elapsed time
local elapsed = now - last_refill
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
if tokens >= 1 then
    allowed = 1
    tokens = tokens - 1
end

redis.call('HSET', key, 'tokens', tokens, 'last_refill', now)
-- Let the key expire on its own if the client goes quiet, so we don't
-- accumulate infinite keys in Redis for one-off clients.
redis.call('EXPIRE', key, 3600)

return {allowed, tostring(tokens)}
"""


class RedisTokenBucketAtomic:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        # Registering the script once and calling it by its SHA is faster
        # than sending the full script text on every call.
        self._script = self.redis.register_script(TOKEN_BUCKET_LUA_SCRIPT)

    def _key(self, bucket_id: str) -> str:
        return f"ratelimit:atomic:{bucket_id}"

    def allow_request(self, bucket_id: str, capacity: float, refill_rate: float) -> tuple[bool, float]:
        """
        bucket_id: uniquely identifies this bucket - as of Step 5, this
        combines client identity (IP or API key) AND endpoint, since
        different endpoints now have independent limits for the same
        client (see app/middleware/rate_limit.py).

        Returns (allowed, tokens_remaining).
        """
        now = time.time()
        result = self._script(
            keys=[self._key(bucket_id)],
            args=[capacity, refill_rate, now],
        )
        allowed, tokens_remaining = result
        return bool(allowed), float(tokens_remaining)
