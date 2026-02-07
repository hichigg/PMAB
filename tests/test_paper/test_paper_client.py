"""Tests for PaperTradingClient — reads delegate to real, writes to sim."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from src.backtest.sim_client import SimulatedClient
from src.core.config import PaperTradingConfig
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
from src.paper.client import PaperTradingClient


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


def _make_client() -> PaperTradingClient:
    """Create a PaperTradingClient with mocked real client."""
    paper_cfg = PaperTradingConfig(
        fill_probability=1.0,
        slippage_bps=5,
        orderbook_refresh_secs=30.0,
    )
    client = PaperTradingClient.__new__(PaperTradingClient)
    client._real_client = AsyncMock()
    client._sim = SimulatedClient(
        fill_probability=paper_cfg.fill_probability,
        slippage_bps=paper_cfg.slippage_bps,
    )
    client._refresh_interval = paper_cfg.orderbook_refresh_secs
    client._refresh_task = None
    client._tracked_tokens = set()
    return client


# ── Read Delegation ────────────────────────────────────────────


class TestReadDelegation:
    async def test_get_markets_delegates_to_real(self) -> None:
        client = _make_client()
        market = MarketInfo(condition_id="cond_1", question="Test?")
        client._real_client.get_markets.return_value = ([market], "")

        result, cursor = await client.get_markets()
        assert len(result) == 1
        assert result[0].condition_id == "cond_1"
        client._real_client.get_markets.assert_called_once()

    async def test_get_all_markets_delegates_to_real(self) -> None:
        client = _make_client()
        client._real_client.get_all_markets.return_value = []

        result = await client.get_all_markets(max_pages=5)
        assert result == []
        client._real_client.get_all_markets.assert_called_once_with(max_pages=5)

    async def test_get_market_delegates_to_real(self) -> None:
        client = _make_client()
        market = MarketInfo(condition_id="cond_1", question="Test?")
        client._real_client.get_market.return_value = market

        result = await client.get_market("cond_1")
        assert result.condition_id == "cond_1"

    async def test_get_orderbook_delegates_and_syncs_sim(self) -> None:
        client = _make_client()
        book = _book("tok_1")
        client._real_client.get_orderbook.return_value = book

        result = await client.get_orderbook("tok_1")
        assert result.token_id == "tok_1"
        assert result.best_bid == Decimal("0.85")

        # Verify sim got updated
        sim_book = await client._sim.get_orderbook("tok_1")
        assert sim_book.token_id == "tok_1"
        assert sim_book.best_bid == Decimal("0.85")

        # Verify token is tracked
        assert "tok_1" in client._tracked_tokens

    async def test_get_orderbooks_delegates_and_syncs_sim(self) -> None:
        client = _make_client()
        books = [_book("tok_1"), _book("tok_2")]
        client._real_client.get_orderbooks.return_value = books

        result = await client.get_orderbooks(["tok_1", "tok_2"])
        assert len(result) == 2

        # Sim should have both books
        sim_b1 = await client._sim.get_orderbook("tok_1")
        sim_b2 = await client._sim.get_orderbook("tok_2")
        assert sim_b1.best_bid == Decimal("0.85")
        assert sim_b2.best_bid == Decimal("0.85")

    async def test_get_midpoint_delegates_to_real(self) -> None:
        client = _make_client()
        client._real_client.get_midpoint.return_value = Decimal("0.86")

        result = await client.get_midpoint("tok_1")
        assert result == Decimal("0.86")

    async def test_get_spread_delegates_to_real(self) -> None:
        client = _make_client()
        client._real_client.get_spread.return_value = Decimal("0.02")

        result = await client.get_spread("tok_1")
        assert result == Decimal("0.02")

    async def test_subscribe_orderbook_delegates_to_real(self) -> None:
        client = _make_client()
        callback = AsyncMock()
        await client.subscribe_orderbook("tok_1", callback)
        client._real_client.subscribe_orderbook.assert_called_once_with("tok_1", callback)

    async def test_unsubscribe_orderbook_delegates_to_real(self) -> None:
        client = _make_client()
        await client.unsubscribe_orderbook("tok_1")
        client._real_client.unsubscribe_orderbook.assert_called_once_with("tok_1")


# ── Write Delegation ───────────────────────────────────────────


class TestWriteDelegation:
    async def test_place_order_goes_to_sim(self) -> None:
        client = _make_client()
        # Set up orderbook in sim so fill can succeed
        client._sim.set_orderbooks({"tok_1": _book("tok_1")})

        req = OrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            price=Decimal("0.90"),
            size=Decimal("100"),
            order_type=OrderType.FOK,
        )
        resp = await client.place_order(req)
        assert resp.success is True
        assert resp.order_id.startswith("sim_")

        # Real client should NOT have been called for order placement
        client._real_client.place_order.assert_not_called()

    async def test_place_market_order_goes_to_sim(self) -> None:
        client = _make_client()
        client._sim.set_orderbooks({"tok_1": _book("tok_1")})

        req = MarketOrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            size=Decimal("100"),
        )
        resp = await client.place_market_order(req)
        assert resp.success is True
        client._real_client.place_market_order.assert_not_called()

    async def test_cancel_order_goes_to_sim(self) -> None:
        client = _make_client()
        resp = await client.cancel_order("order_123")
        assert resp.success is True
        assert resp.order_id == "order_123"
        client._real_client.cancel_order.assert_not_called()

    async def test_cancel_orders_goes_to_sim(self) -> None:
        client = _make_client()
        results = await client.cancel_orders(["o1", "o2"])
        assert len(results) == 2
        assert all(r.success for r in results)

    async def test_cancel_all_goes_to_sim(self) -> None:
        client = _make_client()
        results = await client.cancel_all()
        assert results == []

    async def test_fills_recorded_in_sim(self) -> None:
        client = _make_client()
        client._sim.set_orderbooks({"tok_1": _book("tok_1")})

        req = OrderRequest(
            token_id="tok_1",
            side=Side.BUY,
            price=Decimal("0.90"),
            size=Decimal("100"),
            order_type=OrderType.FOK,
        )
        await client.place_order(req)
        assert len(client.sim.fills) == 1
        assert client.sim.fills[0].success is True


# ── Lifecycle ──────────────────────────────────────────────────


class TestLifecycle:
    async def test_connect_delegates_to_real(self) -> None:
        client = _make_client()
        await client.connect()
        client._real_client.connect.assert_called_once()

    async def test_close_delegates_to_real(self) -> None:
        client = _make_client()
        await client.close()
        client._real_client.close.assert_called_once()

    async def test_context_manager(self) -> None:
        client = _make_client()
        async with client:
            pass
        client._real_client.connect.assert_called_once()
        client._real_client.close.assert_called_once()


# ── Orderbook Refresh ──────────────────────────────────────────


class TestOrderbookRefresh:
    async def test_start_and_stop_refresh(self) -> None:
        client = _make_client()
        await client.start_orderbook_refresh(
            token_ids=["tok_1", "tok_2"],
            interval=100.0,  # large interval so it doesn't fire during test
        )
        assert client._refresh_task is not None
        assert "tok_1" in client._tracked_tokens
        assert "tok_2" in client._tracked_tokens

        await client.stop_orderbook_refresh()
        assert client._refresh_task is None

    async def test_get_orderbook_auto_tracks(self) -> None:
        client = _make_client()
        book = _book("tok_new")
        client._real_client.get_orderbook.return_value = book

        await client.get_orderbook("tok_new")
        assert "tok_new" in client._tracked_tokens
