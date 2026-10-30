"""Per-account serialised send queue with conservative rate limiting.

Signal aggressively flags numbers that send like bulk tools. Every send for
an account goes through one FIFO queue with a minimum gap between messages
and a per-minute cap, regardless of how many API clients are connected.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


class SendQueue:
    def __init__(self, min_interval: float = 1.0, burst_per_minute: int = 20) -> None:
        self.min_interval = min_interval
        self.burst_per_minute = burst_per_minute
        self._locks: dict[str, asyncio.Lock] = {}
        self._sent_at: dict[str, deque[float]] = {}

    async def run(self, account: str, job: Callable[[], Awaitable[T]]) -> T:
        lock = self._locks.setdefault(account, asyncio.Lock())
        async with lock:
            await self._throttle(account)
            result = await job()
            self._sent_at.setdefault(account, deque()).append(time.monotonic())
            return result

    async def _throttle(self, account: str) -> None:
        history = self._sent_at.setdefault(account, deque())
        while True:
            now = time.monotonic()
            while history and now - history[0] > 60:
                history.popleft()
            wait = 0.0
            if history:
                wait = max(wait, self.min_interval - (now - history[-1]))
            if len(history) >= self.burst_per_minute:
                wait = max(wait, 60 - (now - history[0]))
            if wait <= 0:
                return
            await asyncio.sleep(wait)


async def identity(value: Any) -> Any:  # helper for tests
    return value
