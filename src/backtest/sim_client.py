"""Simulated Polymarket client for backtesting.

Fills orders from historical orderbook data instead of hitting the real API.
Supports configurable fill probability and slippage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from src.core.types import (
    CancelResponse,
    MarketInfo,
    MarketOrderRequest,
    OrderBook,
    OrderRequest,
    OrderResponse,
    OrderType,
    PriceLevel,
    Side,
)


@dataclass
class FillRecord:
    """Record of a simulated fill for post-hoc analysis."""

    token_id: str
    side: str
    requested_price: Decimal
    requested_size: Decimal
    fill_price: Decimal
    fill_size: Decimal
    success: bool
    timestamp: float
    slippage: Decimal = Decimal(0)


class SimulatedClient:
    """Drop-in replacement for PolymarketClient during backtests.

    Maintains a dictionary of orderbook snapshots that can be updated
    per-event.  Order placement simulates fills by walking the book.

    Usage::

        client = SimulatedClient(fill_probability=1.0, slippage_bps=5)
        client.set_orderbooks({"tok_1": OrderBook(...)})

        # Used by ArbEngine just like the real client:
        resp = await client.place_order(req)
    """

    def __init__(
        self,
        fill_probability: float = 1.0,
        slippage_bps: int = 0,
    ) -> None:
        self._books: dict[str, OrderBook] = {}
        self._markets: dict[str, MarketInfo] = {}
        self._fill_probability = fill_probability
        self._slippage_bps = slippage_bps
        self._fills: list[FillRecord] = []
        self._order_counter = 0
        self._time: float = 0.0

    # ── State management ────────────────────────────────────────

    def set_orderbooks(self, books: dict[str, OrderBook]) -> None:
        """Update the available orderbook snapshots."""
        self._books.update(books)

    def set_markets(self, markets: dict[str, MarketInfo]) -> None:
        """Set available market info."""
        self._markets.update(markets)

    def set_time(self, ts: float) -> None:
        """Set the simulated clock (used for fill timestamps)."""
        self._time = ts

    @property
    def fills(self) -> list[FillRecord]:
        """All simulated fills (for analysis)."""
        return list(self._fills)

    # ── Market Data (stub implementations) ──────────────────────

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> SimulatedClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def get_markets(
        self, next_cursor: str = "",
    ) -> tuple[list[MarketInfo], str]:
        return list(self._markets.values()), ""

    async def get_all_markets(self, max_pages: int = 50) -> list[MarketInfo]:
        return list(self._markets.values())

    async def get_market(self, condition_id: str) -> MarketInfo:
        return self._markets.get(condition_id, MarketInfo(condition_id=condition_id))

    async def get_orderbook(self, token_id: str) -> OrderBook:
        return self._books.get(
            token_id, OrderBook(token_id=token_id),
        )

    async def get_orderbooks(self, token_ids: list[str]) -> list[OrderBook]:
        return [await self.get_orderbook(tid) for tid in token_ids]

    async def get_midpoint(self, token_id: str) -> Decimal | None:
        book = self._books.get(token_id)
        if book and book.best_bid is not None and book.best_ask is not None:
            return (book.best_bid + book.best_ask) / 2
        return None

    async def get_spread(self, token_id: str) -> Decimal | None:
        book = self._books.get(token_id)
        if book:
            return book.spread
        return None

    async def subscribe_orderbook(
        self, token_id: str, callback: Any,
    ) -> None:
        pass

    async def unsubscribe_orderbook(self, token_id: str) -> None:
        pass

    # ── Order Execution (simulated) ─────────────────────────────

    async def place_order(self, req: OrderRequest) -> OrderResponse:
        """Simulate order execution by walking the orderbook."""
        return self._simulate_fill(
            token_id=req.token_id,
            side=req.side,
            price=req.price,
            size=req.size,
            order_type=req.order_type,
        )

    async def place_market_order(self, req: MarketOrderRequest) -> OrderResponse:
        """Simulate a market (FOK) order."""
        worst_price = req.worst_price
        if worst_price is None:
            worst_price = Decimal("0.99") if req.side == Side.BUY else Decimal("0.01")
        return self._simulate_fill(
            token_id=req.token_id,
            side=req.side,
            price=worst_price,
            size=req.size,
            order_type=OrderType.FOK,
        )

    async def cancel_order(self, order_id: str) -> CancelResponse:
        return CancelResponse(order_id=order_id, success=True)

    async def cancel_orders(self, order_ids: list[str]) -> list[CancelResponse]:
        return [CancelResponse(order_id=oid, success=True) for oid in order_ids]

    async def cancel_all(self) -> list[CancelResponse]:
        return []

    # ── Fill simulation ─────────────────────────────────────────

    def _simulate_fill(
        self,
        token_id: str,
        side: Side,
        price: Decimal,
        size: Decimal,
        order_type: OrderType = OrderType.FOK,
    ) -> OrderResponse:
        """Walk the orderbook to simulate a fill.

        - BUY orders consume asks (lowest first).
        - SELL orders consume bids (highest first).
        - FOK orders must fill completely or fail.
        - Slippage is applied to the fill price.
        """
        self._order_counter += 1
        order_id = f"sim_{self._order_counter}"
        now = self._time or time.time()

        # Check fill probability (simulate random failures)
        if self._fill_probability < 1.0:
            import hashlib
            # Deterministic "randomness" from order details for reproducibility
            seed = hashlib.md5(
                f"{token_id}{side}{price}{size}{self._order_counter}".encode(),
            ).hexdigest()
            if int(seed[:8], 16) / 0xFFFFFFFF > self._fill_probability:
                self._fills.append(FillRecord(
                    token_id=token_id,
                    side=side.value,
                    requested_price=price,
                    requested_size=size,
                    fill_price=Decimal(0),
                    fill_size=Decimal(0),
                    success=False,
                    timestamp=now,
                ))
                return OrderResponse(order_id=order_id, success=False, raw={})

        book = self._books.get(token_id)
        if book is None:
            self._fills.append(FillRecord(
                token_id=token_id,
                side=side.value,
                requested_price=price,
                requested_size=size,
                fill_price=Decimal(0),
                fill_size=Decimal(0),
                success=False,
                timestamp=now,
            ))
            return OrderResponse(order_id=order_id, success=False, raw={})

        # Walk the book
        if side == Side.BUY:
            levels = list(book.asks)  # ascending price
        else:
            levels = list(book.bids)  # descending price

        filled_size = Decimal(0)
        total_cost = Decimal(0)

        for level in levels:
            if side == Side.BUY and level.price > price:
                break  # can't fill above limit
            if side == Side.SELL and level.price < price:
                break  # can't fill below limit

            available = level.size
            take = min(available, size - filled_size)
            filled_size += take
            total_cost += take * level.price

            if filled_size >= size:
                break

        # FOK: must fill completely
        if order_type == OrderType.FOK and filled_size < size:
            self._fills.append(FillRecord(
                token_id=token_id,
                side=side.value,
                requested_price=price,
                requested_size=size,
                fill_price=Decimal(0),
                fill_size=Decimal(0),
                success=False,
                timestamp=now,
            ))
            return OrderResponse(order_id=order_id, success=False, raw={})

        if filled_size <= 0:
            self._fills.append(FillRecord(
                token_id=token_id,
                side=side.value,
                requested_price=price,
                requested_size=size,
                fill_price=Decimal(0),
                fill_size=Decimal(0),
                success=False,
                timestamp=now,
            ))
            return OrderResponse(order_id=order_id, success=False, raw={})

        # Calculate average fill price
        avg_fill_price = total_cost / filled_size

        # Apply slippage
        slippage = avg_fill_price * Decimal(self._slippage_bps) / Decimal(10000)
        if side == Side.BUY:
            avg_fill_price += slippage  # worse fill for buyer
        else:
            avg_fill_price -= slippage  # worse fill for seller

        self._fills.append(FillRecord(
            token_id=token_id,
            side=side.value,
            requested_price=price,
            requested_size=size,
            fill_price=avg_fill_price,
            fill_size=filled_size,
            success=True,
            timestamp=now,
            slippage=slippage,
        ))

        return OrderResponse(
            order_id=order_id,
            success=True,
            raw={"fill_price": str(avg_fill_price), "fill_size": str(filled_size)},
        )
