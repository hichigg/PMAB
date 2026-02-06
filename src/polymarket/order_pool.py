"""Pool of pre-signed orders keyed by (token_id, side, price) with auto-refresh."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import structlog

from src.core.types import Side
from src.polymarket.market_params import MarketParamsCache
from src.polymarket.presigner import OrderPreSigner, PreSignedOrder

logger = structlog.stdlib.get_logger()

# Type alias for the pool key
PoolKey = tuple[str, str, str]  # (token_id, side, price_str)


def _make_key(token_id: str, side: Side, price: Decimal) -> PoolKey:
    """Create a normalized pool key."""
    return (token_id, side.value, str(price))


class PreSignedOrderPool:
    """Manages a pool of pre-signed orders, keyed by (token_id, side, price).

    Provides O(1) lookup of ready-to-post orders. Runs an optional background
    task that evicts expired orders and refreshes those approaching expiration.
    """

    def __init__(
        self,
        presigner: OrderPreSigner,
        params_cache: MarketParamsCache,
        sdk: Any,
        refresh_interval_secs: float = 10.0,
        staleness_threshold_secs: float = 60.0,
    ) -> None:
        self._presigner = presigner
        self._params_cache = params_cache
        self._sdk = sdk
        self._pool: dict[PoolKey, PreSignedOrder] = {}
        self._refresh_interval = refresh_interval_secs
        self._staleness_threshold = staleness_threshold_secs
        self._refresh_task: asyncio.Task[None] | None = None
        self._running = False

    # ── Pool CRUD ───────────────────────────────────────────────

    def add(self, order: PreSignedOrder) -> None:
        """Add a pre-signed order, replacing any existing at the same key."""
        key = _make_key(
            order.request.token_id, order.request.side, order.request.price
        )
        self._pool[key] = order

    def get(
        self, token_id: str, side: Side, price: Decimal
    ) -> PreSignedOrder | None:
        """Retrieve a pre-signed order. Returns None if expired/stale."""
        key = _make_key(token_id, side, price)
        order = self._pool.get(key)
        if order is None:
            return None
        if order.is_expired or order.is_stale:
            self._pool.pop(key, None)
            return None
        return order

    def pop(
        self, token_id: str, side: Side, price: Decimal
    ) -> PreSignedOrder | None:
        """Retrieve and remove a pre-signed order (hot-path for posting)."""
        key = _make_key(token_id, side, price)
        order = self._pool.pop(key, None)
        if order is None:
            return None
        if order.is_expired or order.is_stale:
            return None
        return order

    def get_best(self, token_id: str, side: Side) -> PreSignedOrder | None:
        """Get the best-priced valid order for a token+side.

        BUY: highest price. SELL: lowest price.
        """
        candidates: list[PreSignedOrder] = []
        for key, order in self._pool.items():
            if key[0] == token_id and key[1] == side.value:
                if not order.is_expired and not order.is_stale:
                    candidates.append(order)

        if not candidates:
            return None

        if side == Side.BUY:
            return max(candidates, key=lambda o: o.request.price)
        return min(candidates, key=lambda o: o.request.price)

    def remove(self, token_id: str, side: Side, price: Decimal) -> bool:
        """Remove a specific order. Returns True if found."""
        key = _make_key(token_id, side, price)
        return self._pool.pop(key, None) is not None

    def clear_token(self, token_id: str) -> int:
        """Remove all orders for a given token. Returns count removed."""
        to_remove = [k for k in self._pool if k[0] == token_id]
        for k in to_remove:
            del self._pool[k]
        return len(to_remove)

    def clear(self) -> None:
        """Remove all orders from the pool."""
        self._pool.clear()

    def clear_expired(self) -> int:
        """Remove all expired or stale orders. Returns count removed."""
        to_remove = [
            k for k, v in self._pool.items() if v.is_expired or v.is_stale
        ]
        for k in to_remove:
            del self._pool[k]
        if to_remove:
            logger.debug("pool_cleared_expired", count=len(to_remove))
        return len(to_remove)

    @property
    def size(self) -> int:
        """Number of orders currently in the pool."""
        return len(self._pool)

    def keys(self) -> list[PoolKey]:
        """Return all pool keys."""
        return list(self._pool.keys())

    # ── Background Refresh ──────────────────────────────────────

    async def start_refresh_loop(self) -> None:
        """Start the background task that evicts expired and refreshes orders."""
        if self._running:
            return
        self._running = True
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info("pool_refresh_started", interval=self._refresh_interval)

    async def stop_refresh_loop(self) -> None:
        """Stop the background refresh task."""
        self._running = False
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
        logger.info("pool_refresh_stopped")

    async def _refresh_loop(self) -> None:
        """Periodically evict expired and re-sign approaching-stale orders."""
        while self._running:
            try:
                await asyncio.sleep(self._refresh_interval)
                self.clear_expired()
                await self._refresh_approaching_stale()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("pool_refresh_error")

    async def _refresh_approaching_stale(self) -> None:
        """Re-sign orders that will become stale before the next cycle."""
        threshold = self._staleness_threshold + self._refresh_interval
        to_refresh: list[tuple[PoolKey, PreSignedOrder]] = []

        for key, order in self._pool.items():
            if order.expiration_ts == 0:
                continue
            remaining = order.expiration_ts - time.time()
            if 0 < remaining < threshold:
                to_refresh.append((key, order))

        if not to_refresh:
            return

        logger.debug("pool_refreshing_orders", count=len(to_refresh))

        for key, old_order in to_refresh:
            try:
                params = await self._params_cache.get(
                    old_order.request.token_id, self._sdk
                )
                new_order = await self._presigner.presign(
                    old_order.request, params
                )
                self._pool[key] = new_order
            except Exception:
                logger.warning(
                    "pool_refresh_order_failed",
                    token_id=old_order.request.token_id,
                    exc_info=True,
                )
