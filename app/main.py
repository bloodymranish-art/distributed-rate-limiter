"""
Distributed Rate Limiter - main entry point.

Two protected endpoints with independently configured limits
(see config/rate_limit_config.json):
  /api/resource -> capacity=5,  refill_rate=1  (tight, for demoing bursts)
  /api/search   -> capacity=20, refill_rate=5  (looser, simulates a
                                                 cheaper/higher-volume route)
"""

from fastapi import FastAPI

from app.middleware.rate_limit import RateLimitMiddleware

app = FastAPI(title="Distributed Rate Limiter", version="0.1.0")

app.add_middleware(RateLimitMiddleware)


@app.get("/ping")
def ping():
    """Basic health check - confirms the service is up and responding."""
    return {"status": "ok", "service": "rate-limiter"}


@app.get("/api/resource")
def protected_resource():
    """Tightly limited endpoint - see config/rate_limit_config.json."""
    return {"message": "This endpoint is rate limited (tight limit)."}


@app.get("/api/search")
def protected_search():
    """More loosely limited endpoint - independent bucket from /api/resource."""
    return {"message": "This endpoint is rate limited (looser limit)."}
