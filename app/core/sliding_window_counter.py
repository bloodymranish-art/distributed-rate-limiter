"""
Sliding Window Counter rate limiting algorithm (Redis-backed, atomic).

The idea: split time into fixed-size windows (e.g. 1 second each). Track
a request count for the CURRENT window and the PREVIOUS window. To decide
whether to allow a request, estimate the count over the trailing period by
weighting the previous window's count by how much of it still overlaps the
current sliding view:

    estimated_count = previous_window_count * overlap_fraction
                     + current_window_count

where overlap_fraction shrinks from 1.0 (window boundary) to 0.0 (window
fully elapsed) as time moves through the current window.

Why this over the alternatives:
  - Fixed Window Counter: simplest, but allows up to 2x the limit right at
    a window boundary (e.g. limit=100/min, 100 requests at 0:59, another
    100 at 1:00 - 200 requests in 2 seconds, still "within limits" by the
    naive per-window count).
  - Sliding Window Log: perfectly accurate (stores every request timestamp),
    but memory cost scales with request volume, not with the limit.
  - Sliding Window Counter: fixes the boundary-burst flaw of Fixed Window
    using O(1) storage (just two counters), at the cost of being an
    approximation rather than perfectly exact. This is the tradeoff most
    production systems accept - which is why it's the algorithm most
    system design interviews expect you to land on.

Atomicity: just like the Token Bucket, the read-count-write sequence
happens inside one Lua script, so concurrent requests can't race each
other into an incorrect shared count.
"""

import time

import redis


# KEYS[1] = base key for this bucket (we derive current/previous window
#           sub-keys from it)
# ARGV[1] = limit (max requests allowed per window)
# ARGV[2] = window_size_seconds
# ARGV[3] = current unix timestamp
#
# Returns: {allowed (1 or 0), estimated_count (rounded)}
SLIDING_WINDOW_LUA_SCRIPT = """
local base_key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_size = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local current_window_id = math.floor(now / window_size)
local previous_window_id = current_window_id - 1

local current_key = base_key .. ":" .. current_window_id
local previous_key = base_key .. ":" .. previous_window_id

local current_count = tonumber(redis.call('GET', current_key)) or 0
local previous_count = tonumber(redis.call('GET', previous_key)) or 0

-- How far are we into the current window, as a fraction (0.0 to 1.0)?
local elapsed_in_current = now - (current_window_id * window_size)
local position_in_window = elapsed_in_current / window_size

-- The previous window's weight shrinks as we move through the current one.
local previous_weight = 1.0 - position_in_window
local estimated_count = (previous_count * previous_weight) + current_count

local allowed = 0
if estimated_count < limit then
    allowed = 1
    redis.call('INCR', current_key)
    -- Expire the window key after 2x window_size, so old windows clean
    -- themselves up rather than accumulating in Redis forever.
    redis.call('EXPIRE', current_key, window_size * 2)
end

return {allowed, tostring(estimated_count)}
"""


class SlidingWindowCounter:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self._script = self.redis.register_script(SLIDING_WINDOW_LUA_SCRIPT)

    def _key(self, bucket_id: str) -> str:
        return f"ratelimit:sliding:{bucket_id}"

    def allow_request(self, bucket_id: str, limit: float, window_size_seconds: float) -> tuple[bool, float]:
        """
        limit: max requests allowed per window_size_seconds
        Returns (allowed, estimated_count_this_window).
        """
        now = time.time()
        result = self._script(
            keys=[self._key(bucket_id)],
            args=[limit, window_size_seconds, now],
        )
        allowed, estimated_count = result
        return bool(allowed), float(estimated_count)
