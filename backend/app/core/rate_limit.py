"""Per-client rate limiting.
Two limits, both counted per client address:

* `RATE_LIMIT_DEFAULT` applies to every request, through
  `DefaultRateLimitMiddleware` below.
* `UPLOAD_RATE_LIMIT`, a tighter one, applies to the routes that accept
  files, through `@limiter.limit(upload_rate_limit)` on each of them.

Why the default limit is enforced by a middleware written here rather than
by slowapi's own `SlowAPIMiddleware`: that middleware finds the route for a
request by scanning `app.routes` for an object with an `endpoint`
attribute. The FastAPI version this project pins keeps an included router
as a single opaque entry there, so the scan finds nothing and the
middleware exempts every request without saying so. The route decorators
do not depend on that lookup and work as documented.

Limits are read from settings when a request is checked, not when this
module is imported, so they can be changed per environment.

Counters live in this process's memory. With several worker processes
each keeps its own count, which makes the effective limit a multiple of
the configured one; give the limiter a shared `storage_uri` before relying
on these numbers in a multi-worker deployment.
"""

from limits import parse
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import get_settings


def default_rate_limit() -> str:
    return get_settings().rate_limit_default


def upload_rate_limit() -> str:
    return get_settings().upload_rate_limit


limiter = Limiter(key_func=get_remote_address)


def rate_limited_response(limit_description: str) -> JSONResponse:
    """The 429 body, in the same shape as every other API error."""
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "code": "rate_limited",
                "message": (
                    f"Too many requests ({limit_description}). Wait a moment and try again."
                ),
            }
        },
    )


class DefaultRateLimitMiddleware:
    """Applies `RATE_LIMIT_DEFAULT` to every HTTP request, counted per
    client address, method and path. Shares the limiter's storage, so
    `limiter.enabled` and `limiter.reset()` govern it too.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not limiter.enabled:
            await self.app(scope, receive, send)
            return

        item = parse(default_rate_limit())
        client = get_remote_address(Request(scope))
        if limiter.limiter.hit(item, "default", client, scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return

        await rate_limited_response(str(item))(scope, receive, send)
