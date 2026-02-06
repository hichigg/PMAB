"""WebSocket subscription for Polymarket order book updates."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import structlog
import websockets
from websockets.asyncio.client import ClientConnection

from src.core.types import OrderBook, PriceLevel
from src.polymarket.exceptions import ClobWebSocketError

logger = structlog.stdlib.get_logger()

# Type alias for the callback that receives order book updates
OrderBookCallback = Callable[[OrderBook], Awaitable[None] | None]


def _parse_book_message(token_id: str, data: dict[str, Any]) -> OrderBook:
    """Parse a WebSocket book message into an OrderBook."""
    bids = [
        PriceLevel(price=Decimal(str(b["price"])), size=Decimal(str(b["size"])))
        for b in data.get("bids", [])
    ]
    asks = [
        PriceLevel(price=Decimal(str(a["price"])), size=Decimal(str(a["size"])))
        for a in data.get("asks", [])
    ]
    # Bids descending by price, asks ascending
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)

    return OrderBook(
        token_id=token_id,
        bids=bids,
        asks=asks,
        timestamp=time.time(),
    )


class OrderBookSubscription:
    """Manages a single WebSocket subscription for an order book.

    Connects to the Polymarket WS endpoint, subscribes to a token,
    and dispatches book snapshots to a callback. Includes PING keepalive
    and auto-reconnect with exponential backoff.
    """

    PING_INTERVAL = 10.0  # seconds
    RECONNECT_BASE = 1.0  # initial backoff
    RECONNECT_CAP = 30.0  # max backoff

    def __init__(
        self,
        ws_url: str,
        token_id: str,
        callback: OrderBookCallback,
    ) -> None:
        self.ws_url = ws_url
        self.token_id = token_id
        self.callback = callback
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._reconnect_delay = self.RECONNECT_BASE

    async def start(self) -> None:
        """Start the subscription loop in a background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the subscription and close the WebSocket."""
        self._running = False
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        """Main loop: connect, subscribe, listen, reconnect on failure."""
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception:
                if not self._running:
                    break
                logger.warning(
                    "ws_reconnecting",
                    token_id=self.token_id,
                    delay=self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self.RECONNECT_CAP
                )

    async def _connect_and_listen(self) -> None:
        """Establish connection, subscribe, and process messages."""
        try:
            self._ws = await websockets.connect(self.ws_url)
        except Exception as exc:
            raise ClobWebSocketError(f"Failed to connect to {self.ws_url}") from exc

        # Reset backoff on successful connection
        self._reconnect_delay = self.RECONNECT_BASE

        try:
            # Subscribe to the token's order book
            subscribe_msg = json.dumps({
                "assets_ids": [self.token_id],
                "type": "market",
            })
            await self._ws.send(subscribe_msg)

            # Start keepalive pinger
            ping_task = asyncio.create_task(self._ping_loop())

            try:
                async for raw_msg in self._ws:
                    if not self._running:
                        break
                    await self._handle_message(raw_msg)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
        finally:
            if self._ws is not None:
                await self._ws.close()
                self._ws = None

    async def _ping_loop(self) -> None:
        """Send PING frames at regular intervals to keep the connection alive."""
        while self._running and self._ws is not None:
            try:
                await asyncio.sleep(self.PING_INTERVAL)
                if self._ws is not None:
                    await self._ws.ping()
            except asyncio.CancelledError:
                break
            except Exception:
                break

    async def _handle_message(self, raw: str | bytes) -> None:
        """Parse and dispatch a WebSocket message."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("ws_invalid_json", raw=str(raw)[:200])
            return

        # The WS sends different event types; we only care about book snapshots
        if not isinstance(data, list):
            data = [data]

        for event in data:
            event_type = event.get("event_type", "")
            if event_type == "book":
                book = _parse_book_message(self.token_id, event)
                result = self.callback(book)
                if asyncio.iscoroutine(result):
                    await result
