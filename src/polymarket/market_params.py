"""Async cache for per-token market parameters (tick_size, neg_risk, fee_rate_bps)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog
from pydantic import BaseModel

logger = structlog.stdlib.get_logger()

# TickSize is Literal["0.1", "0.01", "0.001", "0.0001"] in the SDK,
# but we store it as str to avoid importing the SDK type everywhere.
TickSize = str


class MarketParams(BaseModel):
    """Cached market parameters for a single token."""

    token_id: str
    tick_size: str  # "0.1", "0.01", "0.001", or "0.0001"
    neg_risk: bool
    fee_rate_bps: int
    fetched_at: float  # time.monotonic() when fetched

    def is_stale(self, max_age_secs: float) -> bool:
        """Check if this cache entry has exceeded its TTL."""
        return (time.monotonic() - self.fetched_at) > max_age_secs


class MarketParamsCache:
    """Thread-safe async cache for MarketParams, keyed by token_id.

    Fetches tick_size, neg_risk, and fee_rate_bps from the SDK on cache miss.
    Refreshes entries older than ``ttl_secs``.
    """

    DEFAULT_TTL_SECS = 300.0  # 5 minutes

    def __init__(self, ttl_secs: float = DEFAULT_TTL_SECS) -> None:
        self._cache: dict[str, MarketParams] = {}
        self._ttl_secs = ttl_secs
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def _get_lock(self, token_id: str) -> asyncio.Lock:
        """Get or create a per-token lock to avoid duplicate fetches."""
        async with self._global_lock:
            if token_id not in self._locks:
                self._locks[token_id] = asyncio.Lock()
            return self._locks[token_id]

    async def get(
        self,
        token_id: str,
        sdk: Any,
        force_refresh: bool = False,
    ) -> MarketParams:
        """Get market params for a token, fetching from SDK on miss or stale."""
        # Fast path: cache hit and not stale
        if not force_refresh and token_id in self._cache:
            entry = self._cache[token_id]
            if not entry.is_stale(self._ttl_secs):
                return entry

        lock = await self._get_lock(token_id)
        async with lock:
            # Double-check after acquiring lock
            if not force_refresh and token_id in self._cache:
                entry = self._cache[token_id]
                if not entry.is_stale(self._ttl_secs):
                    return entry

            # Fetch all three params concurrently
            tick_size, neg_risk, fee_rate_bps = await asyncio.gather(
                asyncio.to_thread(sdk.get_tick_size, token_id),
                asyncio.to_thread(sdk.get_neg_risk, token_id),
                asyncio.to_thread(sdk.get_fee_rate_bps, token_id),
            )

            params = MarketParams(
                token_id=token_id,
                tick_size=str(tick_size),
                neg_risk=bool(neg_risk),
                fee_rate_bps=int(fee_rate_bps),
                fetched_at=time.monotonic(),
            )
            self._cache[token_id] = params
            logger.debug(
                "market_params_cached",
                token_id=token_id,
                tick_size=tick_size,
                neg_risk=neg_risk,
                fee_rate_bps=fee_rate_bps,
            )
            return params

    async def warm(
        self, token_ids: list[str], sdk: Any
    ) -> dict[str, MarketParams]:
        """Pre-fetch params for multiple tokens concurrently."""
        tasks = [self.get(tid, sdk) for tid in token_ids]
        results = await asyncio.gather(*tasks)
        return {r.token_id: r for r in results}

    def invalidate(self, token_id: str) -> None:
        """Remove a token from the cache."""
        self._cache.pop(token_id, None)

    def clear(self) -> None:
        """Clear the entire cache."""
        self._cache.clear()

    def __contains__(self, token_id: str) -> bool:
        return token_id in self._cache

    def __len__(self) -> int:
        return len(self._cache)
