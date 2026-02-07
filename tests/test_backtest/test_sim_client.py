"""Tests for SimulatedClient — orderbook-based fill simulation."""

from __future__ import annotations

from decimal import Decimal

from src.backtest.sim_client import SimulatedClient
from src.core.types import (
    MarketInfo,
    MarketOrderRequest,
    OrderBook,
    OrderRequest,
    OrderType,
    PriceLevel,
    Side,
)


# ── Helpers ─────────────────────────────────────────────────────


def _book(
    token_id: str = "tok_1",
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
) -> OrderBook:
    bid_levels = [
        PriceLevel(price=Decimal(p), size=Decimal(s))
        for p, s in (bids or [("0.85", "500"), ("0.80", "1000")])
    ]
    ask_levels = [
        PriceLevel(price=Decimal(p), size=Decimal(s))
        for p, s in (asks or [("0.87", "500"), ("0.90", "1000")])
    ]
    return OrderBook(token_id=token_id, bids=bid_levels, asks=ask_levels)


# ── Market Data Stubs ───────────────────────────────────────────


class TestMarketData:
    async def test_get_orderbook_returns_set_book(self) -> None:
        client = SimulatedClient()
        book = _book()
        client.set_orderbooks({"tok_1": book})
        result = await client.get_orderbook("tok_1")
        assert result.token_id == "tok_1"
        assert result.best_bid == Decimal("0.85")

    async def test_get_orderbook_empty(self) -> None:
        client = SimulatedClient()
        result = await client.get_orderbook("tok_missing")
        assert result.token_id == "tok_missing"
        assert result.bids == []

    async def test_get_orderbooks(self) -> None:
        client = SimulatedClient()
        client.set_orderbooks({
            "tok_1": _book("tok_1"),
            "tok_2": _book("tok_2"),
        })
        results = await client.get_orderbooks(["tok_1", "tok_2"])
        assert len(results) == 2

    async def test_get_markets(self) -> None:
        client = SimulatedClient()
        client.set_markets({"cond_1": MarketInfo(condition_id="cond_1")})
        markets, cursor = await client.get_markets()
        assert len(markets) == 1
        assert cursor == ""

    async def test_get_midpoint(self) -> None:
        client = SimulatedClient()
        client.set_orderbooks({"tok_1": _book()})
        mid = await client.get_midpoint("tok_1")
        # (0.85 + 0.87) / 2 = 0.86
        assert mid == Decimal("0.86")

    async def test_get_spread(self) -> None:
        client = SimulatedClient()
        client.set_orderbooks({"tok_1": _book()})
        spread = await client.get_spread("tok_1")
        assert spread == Decimal("0.02")

    async def test_connect_and_close(self) -> None:
        client = SimulatedClient()
        await client.connect()
        await client.close()

    async def test_context_manager(self) -> None:
        async with SimulatedClient() as client:
            assert client is not None


# ── Buy Order Fills ─────────────────────────────────────────────


class TestBuyFills:
    async def test_buy_fills_from_asks(self) -> None:
        client = SimulatedClient()
        client.set_orderbooks({"tok_1": _book()})
        req = OrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            price=Decimal("0.90"),
            size=Decimal("200"),
            order_type=OrderType.FOK,
        )
        resp = await client.place_order(req)
        assert resp.success is True
        assert len(client.fills) == 1
        fill = client.fills[0]
        assert fill.success is True
        assert fill.fill_size == Decimal("200")
        # Fills from first ask level at 0.87
        assert fill.fill_price == Decimal("0.87")

    async def test_buy_walks_multiple_levels(self) -> None:
        client = SimulatedClient()
        client.set_orderbooks({"tok_1": _book()})
        req = OrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            price=Decimal("0.95"),
            size=Decimal("800"),
            order_type=OrderType.GTC,
        )
        resp = await client.place_order(req)
        assert resp.success is True
        fill = client.fills[0]
        assert fill.fill_size == Decimal("800")
        # 500 at 0.87, 300 at 0.90 → avg = (500*0.87 + 300*0.90) / 800
        expected = (Decimal("500") * Decimal("0.87") + Decimal("300") * Decimal("0.90")) / Decimal("800")
        assert fill.fill_price == expected

    async def test_buy_fails_if_price_too_low(self) -> None:
        client = SimulatedClient()
        client.set_orderbooks({"tok_1": _book()})
        req = OrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            price=Decimal("0.80"),  # below all asks
            size=Decimal("100"),
            order_type=OrderType.FOK,
        )
        resp = await client.place_order(req)
        assert resp.success is False

    async def test_fok_fails_if_insufficient_liquidity(self) -> None:
        client = SimulatedClient()
        client.set_orderbooks({"tok_1": _book()})
        req = OrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            price=Decimal("0.95"),
            size=Decimal("2000"),  # more than available (500 + 1000)
            order_type=OrderType.FOK,
        )
        resp = await client.place_order(req)
        assert resp.success is False

    async def test_gtc_partial_fill(self) -> None:
        client = SimulatedClient()
        book = _book(asks=[("0.87", "50")])
        client.set_orderbooks({"tok_1": book})
        req = OrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            price=Decimal("0.90"),
            size=Decimal("100"),
            order_type=OrderType.GTC,
        )
        resp = await client.place_order(req)
        assert resp.success is True
        assert client.fills[0].fill_size == Decimal("50")


# ── Sell Order Fills ────────────────────────────────────────────


class TestSellFills:
    async def test_sell_fills_from_bids(self) -> None:
        client = SimulatedClient()
        client.set_orderbooks({"tok_1": _book()})
        req = OrderRequest(
            token_id="tok_1",
            side=Side.SELL,
            price=Decimal("0.80"),
            size=Decimal("200"),
            order_type=OrderType.FOK,
        )
        resp = await client.place_order(req)
        assert resp.success is True
        fill = client.fills[0]
        assert fill.fill_price == Decimal("0.85")
        assert fill.fill_size == Decimal("200")

    async def test_sell_fails_if_price_too_high(self) -> None:
        client = SimulatedClient()
        client.set_orderbooks({"tok_1": _book()})
        req = OrderRequest(
            token_id="tok_1",
            side=Side.SELL,
            price=Decimal("0.90"),  # above all bids
            size=Decimal("100"),
            order_type=OrderType.FOK,
        )
        resp = await client.place_order(req)
        assert resp.success is False


# ── Market Orders ───────────────────────────────────────────────


class TestMarketOrders:
    async def test_market_buy(self) -> None:
        client = SimulatedClient()
        client.set_orderbooks({"tok_1": _book()})
        req = MarketOrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            size=Decimal("100"),
            worst_price=Decimal("0.95"),
        )
        resp = await client.place_market_order(req)
        assert resp.success is True

    async def test_market_order_default_price(self) -> None:
        client = SimulatedClient()
        client.set_orderbooks({"tok_1": _book()})
        req = MarketOrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            size=Decimal("100"),
        )
        resp = await client.place_market_order(req)
        assert resp.success is True


# ── Slippage ────────────────────────────────────────────────────


class TestSlippage:
    async def test_buy_slippage_increases_price(self) -> None:
        client = SimulatedClient(slippage_bps=100)  # 1%
        client.set_orderbooks({"tok_1": _book()})
        req = OrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            price=Decimal("0.90"),
            size=Decimal("100"),
            order_type=OrderType.FOK,
        )
        await client.place_order(req)
        fill = client.fills[0]
        # Base fill at 0.87 + 1% slippage = 0.87 * 0.01 = 0.0087
        assert fill.fill_price > Decimal("0.87")
        assert fill.slippage > 0

    async def test_sell_slippage_decreases_price(self) -> None:
        client = SimulatedClient(slippage_bps=100)
        client.set_orderbooks({"tok_1": _book()})
        req = OrderRequest(
            token_id="tok_1",
            side=Side.SELL,
            price=Decimal("0.80"),
            size=Decimal("100"),
            order_type=OrderType.FOK,
        )
        await client.place_order(req)
        fill = client.fills[0]
        assert fill.fill_price < Decimal("0.85")

    async def test_zero_slippage(self) -> None:
        client = SimulatedClient(slippage_bps=0)
        client.set_orderbooks({"tok_1": _book()})
        req = OrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            price=Decimal("0.90"),
            size=Decimal("100"),
            order_type=OrderType.FOK,
        )
        await client.place_order(req)
        assert client.fills[0].slippage == Decimal(0)


# ── Fill Probability ────────────────────────────────────────────


class TestFillProbability:
    async def test_zero_probability_always_fails(self) -> None:
        client = SimulatedClient(fill_probability=0.0)
        client.set_orderbooks({"tok_1": _book()})
        for _ in range(5):
            req = OrderRequest(
                token_id="tok_1",
                side=Side.BUY,
                price=Decimal("0.90"),
                size=Decimal("100"),
                order_type=OrderType.FOK,
            )
            resp = await client.place_order(req)
            assert resp.success is False

    async def test_full_probability_fills(self) -> None:
        client = SimulatedClient(fill_probability=1.0)
        client.set_orderbooks({"tok_1": _book()})
        req = OrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            price=Decimal("0.90"),
            size=Decimal("100"),
            order_type=OrderType.FOK,
        )
        resp = await client.place_order(req)
        assert resp.success is True


# ── No Orderbook ────────────────────────────────────────────────


class TestNoOrderbook:
    async def test_order_fails_without_book(self) -> None:
        client = SimulatedClient()
        req = OrderRequest(
            token_id="tok_missing",
            side=Side.BUY,
            price=Decimal("0.90"),
            size=Decimal("100"),
            order_type=OrderType.FOK,
        )
        resp = await client.place_order(req)
        assert resp.success is False
        assert len(client.fills) == 1
        assert client.fills[0].success is False


# ── Fill Records ────────────────────────────────────────────────


class TestFillRecords:
    async def test_fills_accumulate(self) -> None:
        client = SimulatedClient()
        client.set_orderbooks({"tok_1": _book()})
        req = OrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            price=Decimal("0.90"),
            size=Decimal("100"),
            order_type=OrderType.FOK,
        )
        await client.place_order(req)
        await client.place_order(req)
        assert len(client.fills) == 2

    async def test_fill_timestamp(self) -> None:
        client = SimulatedClient()
        client.set_time(1234.0)
        client.set_orderbooks({"tok_1": _book()})
        req = OrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            price=Decimal("0.90"),
            size=Decimal("100"),
            order_type=OrderType.FOK,
        )
        await client.place_order(req)
        assert client.fills[0].timestamp == 1234.0

    async def test_cancel_stubs(self) -> None:
        client = SimulatedClient()
        resp = await client.cancel_order("order_1")
        assert resp.success is True
        resps = await client.cancel_orders(["a", "b"])
        assert len(resps) == 2
        resps2 = await client.cancel_all()
        assert resps2 == []
