"""Tests for the Polymarket client wrapper.

Tests mock the SDK to verify:
- Conversions between domain types and SDK types
- Rate limiting integration
- FOK mapping for market orders
- Pagination handling
- WebSocket message parsing
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import reset_settings
from src.core.types import (
    MarketOrderRequest,
    OrderBook,
    OrderRequest,
    OrderType,
    PriceLevel,
    Side,
)
from src.polymarket.client import PolymarketClient, _parse_market, _parse_orderbook
from src.polymarket.exceptions import ClobConnectionError, ClobOrderError
from src.polymarket.rate_limiter import RateLimiter
from src.polymarket.ws import _parse_book_message


@pytest.fixture(autouse=True)
def _clean_settings() -> None:
    reset_settings()


@pytest.fixture()
def mock_sdk() -> MagicMock:
    """Create a mock ClobClient SDK."""
    sdk = MagicMock()
    sdk.get_order_book.return_value = {
        "bids": [
            {"price": "0.60", "size": "100"},
            {"price": "0.55", "size": "200"},
        ],
        "asks": [
            {"price": "0.65", "size": "150"},
            {"price": "0.70", "size": "250"},
        ],
    }
    sdk.get_markets.return_value = {
        "data": [
            {
                "condition_id": "0xabc",
                "question": "Will X happen?",
                "tokens": [{"token_id": "tok1"}],
                "active": True,
                "closed": False,
            }
        ],
        "next_cursor": "",
    }
    sdk.get_market.return_value = {
        "condition_id": "0xabc",
        "question": "Will X happen?",
        "tokens": [{"token_id": "tok1"}],
        "active": True,
        "closed": False,
    }
    sdk.get_midpoint.return_value = {"mid": "0.625"}
    sdk.get_spread.return_value = {"spread": "0.05"}
    sdk.create_order.return_value = {"signed": True}
    sdk.post_order.return_value = {"orderID": "order-123", "success": True}
    sdk.cancel.return_value = {"success": True}
    sdk.cancel_all.return_value = {"canceled": ["order-1", "order-2"]}
    sdk.get_orders.return_value = [{"id": "order-1"}]
    sdk.get_trades.return_value = [{"id": "trade-1"}]
    return sdk


@pytest.fixture()
def fast_limiter() -> RateLimiter:
    """Rate limiter that doesn't actually throttle in tests."""
    return RateLimiter(burst_per_sec=10000, sustained_per_sec=10000)


@pytest.fixture()
async def client(mock_sdk: MagicMock, fast_limiter: RateLimiter) -> PolymarketClient:
    """Create a PolymarketClient with mocked SDK."""
    with patch("src.polymarket.client.ClobClient", return_value=mock_sdk):
        c = PolymarketClient(
            host="https://test.polymarket.com",
            api_key="key",
            api_secret="secret",
            api_passphrase="pass",
            private_key="0xdeadbeef",
            chain_id=137,
            rate_limiter=fast_limiter,
        )
        await c.connect()
        yield c  # type: ignore[misc]
        await c.close()


class TestParseOrderbook:
    """Test raw SDK response → OrderBook conversion."""

    def test_parses_bids_and_asks(self) -> None:
        raw = {
            "bids": [{"price": "0.55", "size": "200"}, {"price": "0.60", "size": "100"}],
            "asks": [{"price": "0.70", "size": "250"}, {"price": "0.65", "size": "150"}],
        }
        book = _parse_orderbook("tok1", raw)

        assert book.token_id == "tok1"
        # Bids sorted descending
        assert book.bids[0].price == Decimal("0.60")
        assert book.bids[1].price == Decimal("0.55")
        # Asks sorted ascending
        assert book.asks[0].price == Decimal("0.65")
        assert book.asks[1].price == Decimal("0.70")

    def test_computed_properties(self) -> None:
        book = OrderBook(
            token_id="tok1",
            bids=[PriceLevel(price=Decimal("0.60"), size=Decimal("100"))],
            asks=[PriceLevel(price=Decimal("0.65"), size=Decimal("150"))],
        )
        assert book.best_bid == Decimal("0.60")
        assert book.best_ask == Decimal("0.65")
        assert book.spread == Decimal("0.05")
        expected_depth = Decimal("0.60") * Decimal("100") + Decimal("0.65") * Decimal("150")
        assert book.depth_usd == expected_depth

    def test_empty_book(self) -> None:
        book = _parse_orderbook("tok1", {"bids": [], "asks": []})
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.spread is None
        assert book.depth_usd == Decimal(0)


class TestParseMarket:
    """Test raw SDK response → MarketInfo conversion."""

    def test_parses_market_fields(self) -> None:
        raw = {
            "condition_id": "0xabc",
            "question": "Will X happen?",
            "tokens": [{"token_id": "tok1"}],
            "active": True,
            "closed": False,
            "tags": ["politics"],
        }
        market = _parse_market(raw)
        assert market.condition_id == "0xabc"
        assert market.question == "Will X happen?"
        assert market.active is True
        assert market.tags == ["politics"]
        assert market.raw == raw


class TestClientOrderBook:
    """Test order book retrieval via client."""

    async def test_get_orderbook(self, client: PolymarketClient, mock_sdk: MagicMock) -> None:
        book = await client.get_orderbook("tok1")
        assert book.token_id == "tok1"
        assert len(book.bids) == 2
        assert len(book.asks) == 2
        mock_sdk.get_order_book.assert_called_once_with("tok1")

    async def test_get_orderbooks_concurrent(
        self, client: PolymarketClient, mock_sdk: MagicMock
    ) -> None:
        books = await client.get_orderbooks(["tok1", "tok2"])
        assert len(books) == 2
        assert mock_sdk.get_order_book.call_count == 2

    async def test_get_midpoint(self, client: PolymarketClient) -> None:
        mid = await client.get_midpoint("tok1")
        assert mid == Decimal("0.625")

    async def test_get_spread(self, client: PolymarketClient) -> None:
        spread = await client.get_spread("tok1")
        assert spread == Decimal("0.05")


class TestClientMarkets:
    """Test market retrieval via client."""

    async def test_get_markets(self, client: PolymarketClient) -> None:
        markets, cursor = await client.get_markets()
        assert len(markets) == 1
        assert markets[0].condition_id == "0xabc"
        assert cursor == ""

    async def test_get_market(self, client: PolymarketClient) -> None:
        market = await client.get_market("0xabc")
        assert market.condition_id == "0xabc"

    async def test_get_all_markets_pagination(
        self, client: PolymarketClient, mock_sdk: MagicMock
    ) -> None:
        # First page returns data and a cursor, second page returns no cursor
        mock_sdk.get_markets.side_effect = [
            {
                "data": [{"condition_id": "0x1", "question": "Q1"}],
                "next_cursor": "cursor1",
            },
            {
                "data": [{"condition_id": "0x2", "question": "Q2"}],
                "next_cursor": "LTE",
            },
        ]
        markets = await client.get_all_markets()
        assert len(markets) == 2
        assert markets[0].condition_id == "0x1"
        assert markets[1].condition_id == "0x2"


class TestClientOrders:
    """Test order placement and cancellation."""

    async def test_place_order(self, client: PolymarketClient, mock_sdk: MagicMock) -> None:
        req = OrderRequest(
            token_id="tok1",
            side=Side.BUY,
            price=Decimal("0.60"),
            size=Decimal("100"),
            order_type=OrderType.GTC,
        )
        resp = await client.place_order(req)
        assert resp.success is True
        assert resp.order_id == "order-123"
        mock_sdk.create_order.assert_called_once()
        mock_sdk.post_order.assert_called_once()

    async def test_place_market_order_uses_fok(
        self, client: PolymarketClient, mock_sdk: MagicMock
    ) -> None:
        req = MarketOrderRequest(
            token_id="tok1",
            side=Side.BUY,
            size=Decimal("100"),
        )
        resp = await client.place_market_order(req)
        assert resp.success is True
        # Verify FOK was passed to post_order (second positional arg)
        post_call_args = mock_sdk.post_order.call_args
        assert post_call_args is not None
        assert post_call_args[0][1] == "FOK"

    async def test_place_market_order_with_worst_price(
        self, client: PolymarketClient, mock_sdk: MagicMock
    ) -> None:
        req = MarketOrderRequest(
            token_id="tok1",
            side=Side.SELL,
            size=Decimal("50"),
            worst_price=Decimal("0.55"),
        )
        resp = await client.place_market_order(req)
        assert resp.success is True

    async def test_cancel_order(self, client: PolymarketClient, mock_sdk: MagicMock) -> None:
        resp = await client.cancel_order("order-1")
        assert resp.success is True
        assert resp.order_id == "order-1"
        mock_sdk.cancel.assert_called_once_with("order-1")

    async def test_cancel_all(self, client: PolymarketClient) -> None:
        resps = await client.cancel_all()
        assert len(resps) == 2
        assert all(r.success for r in resps)

    async def test_order_failure_raises(
        self, client: PolymarketClient, mock_sdk: MagicMock
    ) -> None:
        mock_sdk.create_order.side_effect = RuntimeError("API error")
        req = OrderRequest(
            token_id="tok1",
            side=Side.BUY,
            price=Decimal("0.60"),
            size=Decimal("100"),
        )
        with pytest.raises(ClobOrderError, match="Order failed"):
            await client.place_order(req)


class TestClientQueries:
    """Test order and trade queries."""

    async def test_get_orders(self, client: PolymarketClient) -> None:
        orders = await client.get_orders()
        assert len(orders) == 1
        assert orders[0]["id"] == "order-1"

    async def test_get_trades(self, client: PolymarketClient) -> None:
        trades = await client.get_trades()
        assert len(trades) == 1
        assert trades[0]["id"] == "trade-1"


class TestClientLifecycle:
    """Test client connection lifecycle."""

    async def test_connect_failure_raises(self) -> None:
        reset_settings()
        with patch(
            "src.polymarket.client.ClobClient",
            side_effect=RuntimeError("Connection refused"),
        ):
            c = PolymarketClient(
                host="https://bad.host",
                private_key="0x0",
                rate_limiter=RateLimiter(burst_per_sec=10000, sustained_per_sec=10000),
            )
            with pytest.raises(ClobConnectionError, match="Failed to initialize"):
                await c.connect()

    async def test_sdk_access_before_connect_raises(self) -> None:
        reset_settings()
        c = PolymarketClient(
            host="https://test.host",
            private_key="0x0",
            rate_limiter=RateLimiter(burst_per_sec=10000, sustained_per_sec=10000),
        )
        with pytest.raises(ClobConnectionError, match="not connected"):
            _ = c.sdk

    async def test_async_context_manager(
        self, mock_sdk: MagicMock, fast_limiter: RateLimiter
    ) -> None:
        with patch("src.polymarket.client.ClobClient", return_value=mock_sdk):
            async with PolymarketClient(
                host="https://test.polymarket.com",
                private_key="0xdeadbeef",
                rate_limiter=fast_limiter,
            ) as c:
                assert c.sdk is not None
            # After exiting, SDK is None
            assert c._sdk is None


class TestRateLimiter:
    """Test the rate limiter integration."""

    async def test_rate_limiter_is_called(
        self, client: PolymarketClient, mock_sdk: MagicMock
    ) -> None:
        mock_acquire = AsyncMock()
        client._rate_limiter.acquire = mock_acquire  # type: ignore[method-assign]
        req = OrderRequest(
            token_id="tok1",
            side=Side.BUY,
            price=Decimal("0.60"),
            size=Decimal("100"),
        )
        await client.place_order(req)
        mock_acquire.assert_awaited_once()


class TestWsMessageParsing:
    """Test WebSocket message parsing into OrderBook."""

    def test_parse_book_message(self) -> None:
        data = {
            "event_type": "book",
            "bids": [
                {"price": "0.55", "size": "200"},
                {"price": "0.60", "size": "100"},
            ],
            "asks": [
                {"price": "0.70", "size": "250"},
                {"price": "0.65", "size": "150"},
            ],
        }
        book = _parse_book_message("tok1", data)
        assert book.token_id == "tok1"
        # Bids sorted descending
        assert book.bids[0].price == Decimal("0.60")
        # Asks sorted ascending
        assert book.asks[0].price == Decimal("0.65")

    def test_parse_empty_book_message(self) -> None:
        book = _parse_book_message("tok1", {"event_type": "book"})
        assert book.bids == []
        assert book.asks == []

    def test_parse_preserves_decimal_precision(self) -> None:
        data = {
            "event_type": "book",
            "bids": [{"price": "0.123456789", "size": "1000.50"}],
            "asks": [],
        }
        book = _parse_book_message("tok1", data)
        assert book.bids[0].price == Decimal("0.123456789")
        assert book.bids[0].size == Decimal("1000.50")
