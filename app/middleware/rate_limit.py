"""
Rate limiting middleware for FastAPI.

As of Step 5, this supports multi-dimensional limits:
  - Per-endpoint limits, loaded from config/rate_limit_config.json
  - Per-client identity: an API key (X-API-Key header) if present,
    otherwise falls back to client IP

Each (client, endpoint) pair gets its own independent bucket - so a
client hitting /api/resource and /api/search doesn't share one limit
across both, and an API-key client is tracked separately from an
anonymous IP-based client even if they happen to share an IP (e.g.
behind NAT or a shared office network).
"""

import math

import redis
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.redis_token_bucket_atomic import RedisTokenBucketAtomic
from app.core.rate_limit_config import RateLimitConfig, load_default_config


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        config: RateLimitConfig | None = None,
        redis_host: str = "localhost",
        redis_port: int = 6379,
    ):
        super().__init__(app)
        self.config = config or load_default_config()

        redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=False)
        self.bucket = RedisTokenBucketAtomic(redis_client)

    def _get_client_id(self, request: Request) -> str:
        """
        Prefer an API key if the client sent one - API-key clients get
        tracked as a stable identity regardless of IP, which matters for
        clients behind NAT, mobile networks, or proxies where IP isn't
        a reliable per-client signal. Falls back to IP for anonymous
        traffic.
        """
        api_key = request.headers.get("X-API-Key")
        if api_key:
            return f"apikey:{api_key}"

        if request.client:
            return f"ip:{request.client.host}"

        return "ip:unknown"

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Only rate limit API endpoints. /ping and /docs stay open.
        if not path.startswith("/api/"):
            return await call_next(request)

        rule = self.config.resolve(path)
        client_id = self._get_client_id(request)

        # Bucket is keyed by BOTH client and endpoint, so limits on
        # different endpoints don't share state for the same client.
        bucket_id = f"{client_id}:{path}"

        allowed, remaining = self.bucket.allow_request(
            bucket_id, capacity=rule.capacity, refill_rate=rule.refill_rate
        )

        if not allowed:
            tokens_needed = 1 - remaining
            retry_after = math.ceil(tokens_needed / rule.refill_rate) if rule.refill_rate > 0 else 1

            return JSONResponse(
                status_code=429,
                content={
                    "error": "Too Many Requests",
                    "message": f"Rate limit exceeded for {path}. Try again in {retry_after} second(s).",
                },
                headers={
                    "X-RateLimit-Limit": str(rule.capacity),
                    "X-RateLimit-Remaining": "0",
                    "Retry-After": str(retry_after),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(rule.capacity)
        response.headers["X-RateLimit-Remaining"] = str(math.floor(remaining))
        return response
