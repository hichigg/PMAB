"""Async wrapper around the synchronous py-clob-client SDK."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import TracebackType
from typing import Any

import structlog
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.clob_types import OrderType as SdkOrderType

from src.core.config import get_settings
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
from src.polymarket.exceptions import (
    ClobConnectionError,
    ClobOrderError,
    ClobRateLimitError,
)
from src.polymarket.market_params import MarketParams, MarketParamsCache
from src.polymarket.order_pool import PreSignedOrderPool
from src.polymarket.presigner import OrderPreSigner, PreSignedOrder
from src.polymarket.rate_limiter import RateLimiter
from src.polymarket.ws import OrderBookCallback, OrderBookSubscription

logger = structlog.stdlib.get_logger()

# Mapping from our OrderType to SDK's OrderType string
_ORDER_TYPE_MAP: dict[OrderType, str] = {
    OrderType.GTC: SdkOrderType.GTC,
    OrderType.FOK: SdkOrderType.FOK,
    OrderType.GTD: SdkOrderType.GTD,
}


def _parse_orderbook(token_id: str, raw: Any) -> OrderBook:
    """Convert SDK order book response to our OrderBook type.

    The SDK may return a dict *or* an ``OrderBookSummary`` object,
    so we handle both accessor patterns.
    """
    if isinstance(raw, dict):
        raw_bids = raw.get("bids", [])
        raw_asks = raw.get("asks", [])
    else:
        raw_bids = getattr(raw, "bids", None) or []
        raw_asks = getattr(raw, "asks", None) or []

    def _level(entry: Any) -> PriceLevel:
        if isinstance(entry, dict):
            return PriceLevel(
                price=Decimal(str(entry["price"])),
                size=Decimal(str(entry["size"])),
            )
        return PriceLevel(
            price=Decimal(str(getattr(entry, "price", 0))),
            size=Decimal(str(getattr(entry, "size", 0))),
        )

    bids = [_level(b) for b in raw_bids]
    asks = [_level(a) for a in raw_asks]
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)
    return OrderBook(token_id=token_id, bids=bids, asks=asks)


def _parse_market(raw: dict[str, Any]) -> MarketInfo:
    """Convert SDK market response to our MarketInfo type.

    The Polymarket API may return ``null`` for optional fields,
    so we coalesce to safe defaults.
    """
    return MarketInfo(
        condition_id=raw.get("condition_id") or "",
        question=raw.get("question") or "",
        description=raw.get("description") or "",
        tokens=raw.get("tokens") or [],
        active=raw.get("active", True),
        closed=raw.get("closed", False),
        accepting_orders=raw.get("accepting_orders", True),
        flagged=raw.get("flagged", False),
        end_date_iso=raw.get("end_date_iso") or "",
        tags=raw.get("tags") or [],
        raw=raw,
    )


def _parse_order_response(raw: Any) -> OrderResponse:
    """Parse SDK post_order response into an OrderResponse."""
    order_id = ""
    success = False
    if isinstance(raw, dict):
        order_id = str(raw.get("orderID", raw.get("id", "")))
        success = raw.get("success", bool(order_id))
    elif isinstance(raw, str):
        order_id = raw
        success = True
    return OrderResponse(
        order_id=order_id,
        success=success,
        raw=raw if isinstance(raw, dict) else {"response": raw},
    )


class PolymarketClient:
    """Async wrapper around the synchronous py-clob-client.

    Usage::

        async with PolymarketClient() as client:
            book = await client.get_orderbook(token_id)
            resp = await client.place_order(order_req)
    """

    def __init__(
        self,
        host: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        api_passphrase: str | None = None,
        private_key: str | None = None,
        chain_id: int | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        settings = get_settings()
        pm = settings.polymarket
        rl = settings.rate_limit

        self._host = host or pm.host
        self._api_key = api_key or pm.api_key
        self._api_secret = api_secret or pm.api_secret.get_secret_value()
        self._api_passphrase = api_passphrase or pm.api_passphrase.get_secret_value()
        self._private_key = private_key or pm.private_key.get_secret_value()
        self._chain_id = chain_id or pm.chain_id
        self._ws_url = pm.ws_url

        self._rate_limiter = rate_limiter or RateLimiter(
            burst_per_sec=rl.burst_per_sec,
            sustained_per_sec=rl.sustained_per_sec,
        )

        self._sdk: ClobClient | None = None
        self._ws_subscriptions: dict[str, OrderBookSubscription] = {}
        self._params_cache = MarketParamsCache(ttl_secs=300.0)
        self._presigner: OrderPreSigner | None = None
        self._order_pool: PreSignedOrderPool | None = None

    async def connect(self) -> None:
        """Initialize the underlying SDK client."""
        try:
            self._sdk = await asyncio.to_thread(
                ClobClient,
                self._host,
                key=self._private_key,
                chain_id=self._chain_id,
                creds={
                    "api_key": self._api_key,
                    "api_secret": self._api_secret,
                    "api_passphrase": self._api_passphrase,
                },
            )
        except Exception as exc:
            raise ClobConnectionError(f"Failed to initialize CLOB client: {exc}") from exc

        self._presigner = OrderPreSigner(self._sdk)
        self._order_pool = PreSignedOrderPool(
            presigner=self._presigner,
            params_cache=self._params_cache,
            sdk=self._sdk,
        )
        logger.info("clob_client_connected", host=self._host)

    async def close(self) -> None:
        """Clean up resources — close WS subscriptions and order pool."""
        if self._order_pool is not None:
            await self._order_pool.stop_refresh_loop()
            self._order_pool = None
        self._presigner = None
        self._params_cache.clear()
        for sub in list(self._ws_subscriptions.values()):
            await sub.stop()
        self._ws_subscriptions.clear()
        self._sdk = None
        logger.info("clob_client_closed")

    async def __aenter__(self) -> PolymarketClient:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def sdk(self) -> ClobClient:
        """Access the underlying SDK client, raising if not connected."""
        if self._sdk is None:
            raise ClobConnectionError("Client not connected. Call connect() first.")
        return self._sdk

    # ── Market Data ──────────────────────────────────────────────

    async def get_markets(self, next_cursor: str = "") -> tuple[list[MarketInfo], str]:
        """Fetch a page of markets.

        Returns:
            Tuple of (markets, next_cursor). Empty cursor means no more pages.
        """
        raw = await asyncio.to_thread(self.sdk.get_markets, next_cursor=next_cursor)
        markets = [_parse_market(m) for m in raw.get("data", [])]
        cursor = raw.get("next_cursor", "")
        return markets, cursor

    async def get_all_markets(self, max_pages: int = 50) -> list[MarketInfo]:
        """Fetch all markets with pagination."""
        all_markets: list[MarketInfo] = []
        cursor = ""
        for _ in range(max_pages):
            markets, cursor = await self.get_markets(next_cursor=cursor)
            all_markets.extend(markets)
            if not cursor or cursor == "LTE":
                break
        return all_markets

    async def get_market(self, condition_id: str) -> MarketInfo:
        """Fetch a single market by condition ID."""
        raw = await asyncio.to_thread(self.sdk.get_market, condition_id)
        return _parse_market(raw)

    async def get_orderbook(self, token_id: str) -> OrderBook:
        """Fetch the current order book for a token."""
        try:
            raw = await asyncio.to_thread(self.sdk.get_order_book, token_id)
        except Exception as exc:
            if "404" in str(exc) or "No orderbook" in str(exc):
                return OrderBook(token_id=token_id)
            raise
        return _parse_orderbook(token_id, raw)

    async def get_orderbooks(self, token_ids: list[str]) -> list[OrderBook]:
        """Fetch order books for multiple tokens concurrently."""
        tasks = [self.get_orderbook(tid) for tid in token_ids]
        return list(await asyncio.gather(*tasks))

    async def get_midpoint(self, token_id: str) -> Decimal | None:
        """Get the midpoint price for a token."""
        raw = await asyncio.to_thread(self.sdk.get_midpoint, token_id)
        mid = raw.get("mid")
        if mid is not None:
            return Decimal(str(mid))
        return None

    async def get_spread(self, token_id: str) -> Decimal | None:
        """Get the spread for a token."""
        raw = await asyncio.to_thread(self.sdk.get_spread, token_id)
        spread = raw.get("spread")
        if spread is not None:
            return Decimal(str(spread))
        return None

    # ── WebSocket Subscriptions ──────────────────────────────────

    async def subscribe_orderbook(
        self, token_id: str, callback: OrderBookCallback
    ) -> OrderBookSubscription:
        """Subscribe to real-time order book updates for a token."""
        if token_id in self._ws_subscriptions:
            await self._ws_subscriptions[token_id].stop()

        sub = OrderBookSubscription(
            ws_url=self._ws_url,
            token_id=token_id,
            callback=callback,
        )
        await sub.start()
        self._ws_subscriptions[token_id] = sub
        logger.info("ws_subscribed", token_id=token_id)
        return sub

    async def unsubscribe_orderbook(self, token_id: str) -> None:
        """Unsubscribe from order book updates for a token."""
        sub = self._ws_subscriptions.pop(token_id, None)
        if sub is not None:
            await sub.stop()
            logger.info("ws_unsubscribed", token_id=token_id)

    # ── Order Management ─────────────────────────────────────────

    async def place_order(self, req: OrderRequest) -> OrderResponse:
        """Place a limit order, respecting rate limits."""
        await self._rate_limiter.acquire()

        sdk_order_type = _ORDER_TYPE_MAP.get(req.order_type, SdkOrderType.GTC)

        order_args = OrderArgs(
            token_id=req.token_id,
            price=float(req.price),
            size=float(req.size),
            side=req.side.value,
        )

        try:
            signed = await asyncio.to_thread(self.sdk.create_order, order_args)
            raw = await asyncio.to_thread(
                self.sdk.post_order, signed, sdk_order_type
            )
        except Exception as exc:
            error_msg = str(exc).lower()
            if "rate" in error_msg and "limit" in error_msg:
                raise ClobRateLimitError(str(exc)) from exc
            raise ClobOrderError(f"Order failed: {exc}") from exc

        return _parse_order_response(raw)

    async def place_market_order(self, req: MarketOrderRequest) -> OrderResponse:
        """Place a market order (FOK) — fills immediately or cancels."""
        # Determine a worst-case price if none provided
        worst_price = req.worst_price
        if worst_price is None:
            worst_price = Decimal("0.99") if req.side == Side.BUY else Decimal("0.01")

        order_req = OrderRequest(
            token_id=req.token_id,
            side=req.side,
            price=worst_price,
            size=req.size,
            order_type=OrderType.FOK,
        )
        return await self.place_order(order_req)

    async def cancel_order(self, order_id: str) -> CancelResponse:
        """Cancel a single order by ID."""
        await self._rate_limiter.acquire()
        try:
            raw = await asyncio.to_thread(self.sdk.cancel, order_id)
        except Exception as exc:
            raise ClobOrderError(f"Cancel failed: {exc}") from exc

        success = False
        if isinstance(raw, dict):
            success = raw.get("success", False) or raw.get("canceled", False)
        raw_dict = raw if isinstance(raw, dict) else {"response": raw}
        return CancelResponse(order_id=order_id, success=success, raw=raw_dict)

    async def cancel_orders(self, order_ids: list[str]) -> list[CancelResponse]:
        """Cancel multiple orders concurrently."""
        tasks = [self.cancel_order(oid) for oid in order_ids]
        return list(await asyncio.gather(*tasks))

    async def cancel_all(self) -> list[CancelResponse]:
        """Cancel all open orders."""
        await self._rate_limiter.acquire()
        try:
            raw = await asyncio.to_thread(self.sdk.cancel_all)
        except Exception as exc:
            raise ClobOrderError(f"Cancel all failed: {exc}") from exc

        if isinstance(raw, dict):
            canceled = raw.get("canceled", [])
            return [
                CancelResponse(order_id=oid, success=True, raw=raw)
                for oid in canceled
            ]
        return []

    # ── Pre-signing ───────────────────────────────────────────────

    @property
    def params_cache(self) -> MarketParamsCache:
        """Access the market parameters cache."""
        return self._params_cache

    @property
    def order_pool(self) -> PreSignedOrderPool:
        """Access the pre-signed order pool."""
        if self._order_pool is None:
            raise ClobConnectionError(
                "Client not connected. Call connect() first."
            )
        return self._order_pool

    async def get_market_params(self, token_id: str) -> MarketParams:
        """Fetch (or return cached) market parameters for a token."""
        return await self._params_cache.get(token_id, self.sdk)

    async def warm_market_params(
        self, token_ids: list[str]
    ) -> dict[str, MarketParams]:
        """Pre-fetch market parameters for multiple tokens."""
        return await self._params_cache.warm(token_ids, self.sdk)

    async def presign_order(self, req: OrderRequest) -> PreSignedOrder:
        """Sign an order without posting it.

        Resolves market params from cache (fetching if needed),
        then signs via the SDK's OrderBuilder.
        """
        if self._presigner is None:
            raise ClobConnectionError(
                "Client not connected. Call connect() first."
            )
        params = await self.get_market_params(req.token_id)
        return await self._presigner.presign(req, params)

    async def presign_batch(
        self, requests: list[OrderRequest]
    ) -> list[PreSignedOrder]:
        """Sign multiple orders concurrently without posting."""
        if self._presigner is None:
            raise ClobConnectionError(
                "Client not connected. Call connect() first."
            )
        token_ids = list({req.token_id for req in requests})
        params_results = await self.warm_market_params(token_ids)
        return await self._presigner.presign_batch(
            requests, params_results
        )

    async def post_presigned(
        self, presigned: PreSignedOrder
    ) -> OrderResponse:
        """Post a previously signed order — no signing delay, just HTTP POST."""
        await self._rate_limiter.acquire()

        sdk_order_type = _ORDER_TYPE_MAP.get(
            presigned.order_type, SdkOrderType.GTC
        )

        try:
            raw = await asyncio.to_thread(
                self.sdk.post_order, presigned.signed_order, sdk_order_type
            )
        except Exception as exc:
            error_msg = str(exc).lower()
            if "rate" in error_msg and "limit" in error_msg:
                raise ClobRateLimitError(str(exc)) from exc
            raise ClobOrderError(
                f"Post presigned order failed: {exc}"
            ) from exc

        return _parse_order_response(raw)

    # ── Order/Trade Queries ──────────────────────────────────────

    async def get_orders(self) -> list[dict[str, Any]]:
        """Fetch open orders."""
        raw: Any = await asyncio.to_thread(self.sdk.get_orders)
        if isinstance(raw, list):
            return list(raw)
        if isinstance(raw, dict):
            return list(raw.get("data", []))
        return []

    async def get_trades(self) -> list[dict[str, Any]]:
        """Fetch recent trades."""
        raw: Any = await asyncio.to_thread(self.sdk.get_trades)
        if isinstance(raw, list):
            return list(raw)
        if isinstance(raw, dict):
            return list(raw.get("data", []))
        return []
