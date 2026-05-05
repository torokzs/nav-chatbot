import asyncio
from collections import defaultdict, deque
from time import monotonic

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, get_settings

_REQUEST_LOGS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LIMIT_LOCK = asyncio.Lock()


def _resolve_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def enforce_rate_limit(
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> None:
    window_start = monotonic() - 60.0
    client_ip = _resolve_client_ip(request)

    async with _RATE_LIMIT_LOCK:
        requests = _REQUEST_LOGS[client_ip]
        while requests and requests[0] <= window_start:
            requests.popleft()

        if len(requests) >= settings.rate_limit_rpm:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please try again later.",
            )

        requests.append(monotonic())
