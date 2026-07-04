"""
Rate limiting middleware for FastAPI.

As of Step 7, this also handles Redis being unreachable. Each rule
specifies a fail_mode ("open" or "closed") in config:
  - fail_mode="open":   if Redis is down, let the request through.
                        Prioritizes availability over strict enforcement.
  - fail_mode="closed": if Redis is down, reject the request (503).
                        Prioritizes strict enforcement over availability.

Different endpoints can reasonably choose differently - a cheap read
endpoint might fail open (staying up matters more than perfect limits
for a few minutes), while something protecting an expensive or abusable
resource might fail closed (better to be unavailable than unprotected).
"""

import math

import redis
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.rate_limit_config import RateLimitConfig, RateLimitRule, load_default_config
from app.core.strategy import RateLimitStrategy, build_strategy


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
        # Short socket timeout so a dead Redis fails fast (within ~1s)
        # instead of hanging the request while retrying/waiting.
        self.redis_client = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=False,
            socket_connect_timeout=1,
            socket_timeout=1,
        )

        # Cache one strategy instance per distinct rule, rather than
        # rebuilding it (and re-registering its Lua script) on every request.
        self._strategy_cache: dict[str, RateLimitStrategy] = {}

    def _get_strategy(self, rule: RateLimitRule) -> RateLimitStrategy:
        cache_key = f"{rule.algorithm}:{rule.params}"
        if cache_key not in self._strategy_cache:
            self._strategy_cache[cache_key] = build_strategy(
                self.redis_client, rule.algorithm, rule.params
            )
        return self._strategy_cache[cache_key]

    def _get_client_id(self, request: Request) -> str:
        """
        Prefer an API key if the client sent one - API-key clients get
        tracked as a stable identity regardless of IP. Falls back to IP
        for anonymous traffic.
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
        strategy = self._get_strategy(rule)
        client_id = self._get_client_id(request)

        # Bucket is keyed by BOTH client and endpoint, so limits on
        # different endpoints don't share state for the same client.
        bucket_id = f"{client_id}:{path}"

        try:
            allowed, info = strategy.allow_request(bucket_id)
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
            return await self._handle_redis_failure(request, call_next, rule, path, e)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Too Many Requests",
                    "message": f"Rate limit exceeded for {path} (algorithm: {rule.algorithm}).",
                },
                headers={
                    "X-RateLimit-Limit": str(info["limit"]),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Algorithm": rule.algorithm,
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(info["limit"])
        response.headers["X-RateLimit-Remaining"] = str(math.floor(info["remaining"]))
        response.headers["X-RateLimit-Algorithm"] = rule.algorithm
        return response

    async def _handle_redis_failure(self, request: Request, call_next, rule: RateLimitRule, path: str, error: Exception):
        """
        Redis is unreachable. Apply this rule's configured fail_mode.
        """
        if rule.fail_mode == "open":
            # Let the request through, but say so explicitly via a header -
            # silently degrading protection should never be invisible.
            response = await call_next(request)
            response.headers["X-RateLimit-Status"] = "degraded-fail-open"
            return response

        # fail_mode == "closed": reject rather than risk unprotected traffic.
        return JSONResponse(
            status_code=503,
            content={
                "error": "Service Unavailable",
                "message": (
                    f"Rate limiting backend is unreachable and {path} is configured "
                    "to fail closed. Request rejected rather than risk exceeding limits."
                ),
            },
            headers={"X-RateLimit-Status": "degraded-fail-closed"},
        )
