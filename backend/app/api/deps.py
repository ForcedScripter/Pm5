from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import read_token
from app.database.session import get_db
from app.models import User


async def current_user(authorization: str = Header(default=""), db: AsyncSession = Depends(get_db)) -> User:
    token = authorization.removeprefix("Bearer ").strip()
    uid = read_token(token) if token else None
    if not uid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    try:
        key = uuid.UUID(uid)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated") from None
    user = await db.get(User, key)
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    return user


class RateLimit:
    """Sliding-window limiter per client IP (in-process; fine for a single self-hosted node)."""

    def __init__(self, limit: int, per_seconds: int):
        self.limit, self.per = limit, per_seconds
        self.hits: dict[str, deque] = defaultdict(deque)

    def __call__(self, request: Request):
        key = request.client.host if request.client else "?"
        q, now = self.hits[key], time.monotonic()
        while q and now - q[0] > self.per:
            q.popleft()
        if len(q) >= self.limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests, slow down")
        q.append(now)


login_limit = RateLimit(10, 300)
sensitive_limit = RateLimit(20, 60)
