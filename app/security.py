"""Who may talk to Daedalus, and how often."""

from __future__ import annotations

import time
from collections import defaultdict, deque

MAX_MESSAGE = 8000


def is_allowed(user_id: int | None, allowed: frozenset[int]) -> bool:
    """Only listed Telegram user ids; an empty list means nobody."""
    return user_id is not None and user_id in allowed


class RateLimiter:
    """Sliding window: at most `limit` requests per `window` seconds per user."""

    def __init__(self, limit: int, window: float = 3600, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.hits: dict[int, deque] = defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        now, hits = self.clock(), self.hits[user_id]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


def clean_input(text: str | None) -> str | None:
    """Strip, reject empty or oversized messages."""
    text = (text or "").strip()
    if not text or len(text) > MAX_MESSAGE:
        return None
    return text
