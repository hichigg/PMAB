"""Async dual token-bucket rate limiter for CLOB API."""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """A simple token bucket that refills at a fixed rate."""

    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self._last_refill = now

    def try_acquire(self) -> bool:
        """Try to consume one token. Returns True if successful."""
        self._refill()
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def time_until_available(self) -> float:
        """Seconds until at least one token is available."""
        self._refill()
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.rate


class RateLimiter:
    """Dual token-bucket rate limiter: burst + sustained.

    Both buckets must have tokens for a request to proceed.
    ``acquire()`` blocks (async) until both are satisfied.
    """

    def __init__(self, burst_per_sec: int = 500, sustained_per_sec: int = 60) -> None:
        self._burst = TokenBucket(rate=float(burst_per_sec), capacity=float(burst_per_sec))
        self._sustained = TokenBucket(
            rate=float(sustained_per_sec), capacity=float(sustained_per_sec)
        )

    async def acquire(self) -> None:
        """Wait until both buckets allow a request, then consume one token from each."""
        while True:
            if self._burst.try_acquire():
                if self._sustained.try_acquire():
                    return
                # Undo burst consumption since sustained wasn't available
                self._burst.tokens = min(self._burst.capacity, self._burst.tokens + 1.0)

            # Wait for the longer of the two delays
            burst_wait = self._burst.time_until_available()
            sustained_wait = self._sustained.time_until_available()
            await asyncio.sleep(max(burst_wait, sustained_wait, 0.001))
