"""
General-purpose Redis-backed rate limiter for FastAPI routes.

Fixed-window counter: each key gets `limit` requests per `window_seconds`,
then 429s with a Retry-After header until the window rolls over.

Usage:

    from plugfit.app.utils.rate_limit import rate_limit
    from plugfit.app.routes.auth import get_current_user

    @router.post(
        "/servers/{server_id}/eval",
        dependencies=[Depends(rate_limit(
            lambda request, tenant: f"eval:{tenant.id}:{request.path_params['server_id']}",
            limit=3,
            window_seconds=3600,
        ))],
    )
    async def trigger_eval(...): ...

The key_func receives (request, tenant) and returns the bucket key — build
it however suits the route (per-user, per-user-per-resource, per-IP, etc).
"""

from __future__ import annotations

import logging
from typing import Callable

from fastapi import Depends, HTTPException, Request
from redis.asyncio import Redis

from plugfit.app.config import settings
from plugfit.app.models.models import User
from plugfit.app.routes.auth.dependencies import get_current_user

log = logging.getLogger("plugfit.rate_limit")

_redis: Redis | None = None

# Atomically increments the counter and (only on first hit) sets its expiry,
# so a burst of concurrent requests can't each reset the window.
_FIXED_WINDOW_SCRIPT = """
local current = redis.call("INCR", KEYS[1])
if current == 1 then
    redis.call("EXPIRE", KEYS[1], ARGV[1])
end
local ttl = redis.call("TTL", KEYS[1])
return {current, ttl}
"""


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def rate_limit(
    key_func: Callable[[Request, User], str],
    limit: int,
    window_seconds: int,
):
    """
    Build a FastAPI dependency that rate-limits by whatever key `key_func` returns.

    Fails open (logs a warning, lets the request through) if Redis is
    unreachable — a rate limiter should never be the reason the app is down.
    """

    async def _dependency(
        request: Request,
        tenant: User = Depends(get_current_user),
    ) -> None:
        key = f"ratelimit:{key_func(request, tenant)}"

        try:
            redis = get_redis()
            current, ttl = await redis.eval(
                _FIXED_WINDOW_SCRIPT, 1, key, window_seconds
            )
        except Exception:
            log.warning("Rate limiter unavailable — allowing request for %s", key)
            return

        if current > limit:
            retry_after = ttl if ttl > 0 else window_seconds
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Rate limit exceeded: {limit} requests per "
                    f"{window_seconds}s. Try again in {retry_after}s."
                ),
                headers={"Retry-After": str(retry_after)},
            )

    return _dependency
