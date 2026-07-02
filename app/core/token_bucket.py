"""
Token Bucket rate limiting algorithm.

Core idea:
- A bucket holds up to `capacity` tokens.
- Tokens refill continuously at `refill_rate` tokens per second.
- Each request consumes 1 token.
- If no tokens are available, the request is rejected.

This lets bursts of traffic through (up to `capacity`), while enforcing
a steady average rate over time (`refill_rate`).

This class is intentionally infrastructure-free: no HTTP, no Redis.
We want to prove the algorithm is correct before we add distributed
complexity on top of it (that comes in Step 3+).
"""

import time


class TokenBucket:
    def __init__(self, capacity: float, refill_rate: float, initial_tokens: float | None = None):
        """
        capacity:      max tokens the bucket can hold (also the max burst size)
        refill_rate:   tokens added per second
        initial_tokens: starting token count (defaults to full bucket)
        """
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be positive")

        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity if initial_tokens is None else initial_tokens
        self.last_refill_time = time.monotonic()

    def _refill(self) -> None:
        """Add tokens based on how much time has elapsed since the last refill."""
        now = time.monotonic()
        elapsed = now - self.last_refill_time
        tokens_to_add = elapsed * self.refill_rate

        self.tokens = min(self.capacity, self.tokens + tokens_to_add)
        self.last_refill_time = now

    def allow_request(self, tokens_requested: float = 1.0) -> bool:
        """
        Attempt to consume `tokens_requested` tokens.
        Returns True if allowed (tokens were consumed), False if rejected.
        """
        self._refill()

        if self.tokens >= tokens_requested:
            self.tokens -= tokens_requested
            return True

        return False

    def available_tokens(self) -> float:
        """Return current token count after applying refill (useful for headers/debugging)."""
        self._refill()
        return self.tokens
