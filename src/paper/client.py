"""Paper trading client — reads real data, simulates order execution.

Wraps a real PolymarketClient (GET endpoints) and a SimulatedClient
(order fills).  Periodically refreshes orderbook snapshots so the
simulator uses realistic depth/pricing.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import TracebackType
from typing import Any

import structlog

from src.backtest.sim_client import SimulatedClient
from src.core.config import PaperTradingConfig, PolymarketConfig
from src.core.types import (
    CancelResponse,
    MarketInfo,
    MarketOrderRequest,
    OrderBook,
    OrderRequest,
    OrderResponse,
)
from src.polymarket.client import PolymarketClient

logger = structlog.get_logger(__name__)


class PaperTradingClient:
    """Drop-in client that reads real market data but simulates execution.

    Usage::

        client = PaperTradingClient(pm_config, paper_config)
        await client.connect()

        # Reads go to Polymarket CLOB API (GET only)
        book = await client.get_orderbook(token_id)

        # Writes go to SimulatedClient (fills against real orderbook data)
        resp = await client.place_order(req)
    """

    def __init__(
        self,
        pm_config: PolymarketConfig | None = None,
        paper_config: PaperTradingConfig | None = None,
    ) -> None:
        paper_config = paper_config or PaperTradingConfig()

        self._real_client = PolymarketClient() if pm_config is None else PolymarketClient(
            host=pm_config.host,
            api_key=pm_config.api_key,
            api_secret=pm_config.api_secret.get_secret_value(),
            api_passphrase=pm_config.api_passphrase.get_secret_value(),
            private_key=pm_config.private_key.get_secret_value(),
            chain_id=pm_config.chain_id,
        )

        self._sim = SimulatedClient(
            fill_probability=paper_config.fill_probability,
            slippage_bps=paper_config.slippage_bps,
        )

        self._refresh_interval = paper_config.orderbook_refresh_secs
        self._refresh_task: asyncio.Task[None] | None = None
        self._tracked_tokens: set[str] = set()

    @property
    def sim(self) -> SimulatedClient:
        """Access the underlying SimulatedClient (for fills inspection)."""
        return self._sim

    # ── Lifecycle ────────────────────────────────────────────────

    async def connect(self) -> None:
        await self._real_client.connect()
        logger.info("paper_client_connected")

    async def close(self) -> None:
        await self.stop_orderbook_refresh()
        await self._real_client.close()
        logger.info("paper_client_closed")

    async def __aenter__(self) -> PaperTradingClient:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ── Reads → delegate to real client ──────────────────────────

    async def get_markets(self, next_cursor: str = "") -> tuple[list[MarketInfo], str]:
        return await self._real_client.get_markets(next_cursor=next_cursor)

    async def get_all_markets(self, max_pages: int = 50) -> list[MarketInfo]:
        return await self._real_client.get_all_markets(max_pages=max_pages)

    async def get_market(self, condition_id: str) -> MarketInfo:
        return await self._real_client.get_market(condition_id)

    async def get_orderbook(self, token_id: str) -> OrderBook:
        book = await self._real_client.get_orderbook(token_id)
        # Keep the simulator in sync with real orderbook data
        self._sim.set_orderbooks({token_id: book})
        self._tracked_tokens.add(token_id)
        return book

    async def get_orderbooks(self, token_ids: list[str]) -> list[OrderBook]:
        books = await self._real_client.get_orderbooks(token_ids)
        updates = {b.token_id: b for b in books}
        self._sim.set_orderbooks(updates)
        self._tracked_tokens.update(token_ids)
        return books

    async def get_midpoint(self, token_id: str) -> Decimal | None:
        return await self._real_client.get_midpoint(token_id)

    async def get_spread(self, token_id: str) -> Decimal | None:
        return await self._real_client.get_spread(token_id)

    async def subscribe_orderbook(
        self, token_id: str, callback: Any,
    ) -> Any:
        return await self._real_client.subscribe_orderbook(token_id, callback)

    async def unsubscribe_orderbook(self, token_id: str) -> None:
        await self._real_client.unsubscribe_orderbook(token_id)

    # ── Writes → delegate to SimulatedClient ─────────────────────

    async def place_order(self, req: OrderRequest) -> OrderResponse:
        return await self._sim.place_order(req)

    async def place_market_order(self, req: MarketOrderRequest) -> OrderResponse:
        return await self._sim.place_market_order(req)

    async def cancel_order(self, order_id: str) -> CancelResponse:
        return await self._sim.cancel_order(order_id)

    async def cancel_orders(self, order_ids: list[str]) -> list[CancelResponse]:
        return await self._sim.cancel_orders(order_ids)

    async def cancel_all(self) -> list[CancelResponse]:
        return await self._sim.cancel_all()

    # ── Background orderbook refresh ─────────────────────────────

    async def start_orderbook_refresh(
        self,
        token_ids: list[str] | None = None,
        interval: float | None = None,
    ) -> None:
        """Periodically fetch real orderbooks and update SimulatedClient.

        Args:
            token_ids: Initial set of tokens to track. Additional tokens
                       are auto-tracked when get_orderbook() is called.
            interval: Override the refresh interval from config.
        """
        if token_ids:
            self._tracked_tokens.update(token_ids)

        refresh_secs = interval if interval is not None else self._refresh_interval

        if self._refresh_task is not None:
            self._refresh_task.cancel()

        self._refresh_task = asyncio.create_task(
            self._refresh_loop(refresh_secs),
        )
        logger.info(
            "orderbook_refresh_started",
            tracked_tokens=len(self._tracked_tokens),
            interval_secs=refresh_secs,
        )

    async def stop_orderbook_refresh(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None

    async def _refresh_loop(self, interval: float) -> None:
        """Background loop that refreshes tracked orderbooks."""
        while True:
            await asyncio.sleep(interval)
            if not self._tracked_tokens:
                continue

            token_list = list(self._tracked_tokens)
            try:
                books = await self._real_client.get_orderbooks(token_list)
                updates = {b.token_id: b for b in books}
                self._sim.set_orderbooks(updates)
                logger.debug(
                    "orderbooks_refreshed",
                    count=len(updates),
                )
            except Exception:
                logger.exception("orderbook_refresh_error")
