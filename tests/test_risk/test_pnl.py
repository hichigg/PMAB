"""Tests for PnLTracker — realized P&L, daily reset, unrealized."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from src.core.types import (
    ExecutionResult,
    FeedEvent,
    FeedEventType,
    FeedType,
    MarketCategory,
    MarketOpportunity,
    MatchResult,
    OutcomeType,
    Position,
    Side,
    Signal,
    SignalDirection,
    TradeAction,
)
from src.risk.pnl import PnLTracker

# ── Helpers ─────────────────────────────────────────────────────


def _tokens() -> list[dict[str, Any]]:
    return [{"token_id": "0xyes", "outcome": "Yes"}]


def _signal() -> Signal:
    return Signal(
        match=MatchResult(
            feed_event=FeedEvent(
                feed_type=FeedType.ECONOMIC,
                event_type=FeedEventType.DATA_RELEASED,
                indicator="CPI",
                outcome_type=OutcomeType.NUMERIC,
            ),
            opportunity=MarketOpportunity(
                condition_id="cond1",
                tokens=_tokens(),
                category=MarketCategory.ECONOMIC,
            ),
        ),
        direction=SignalDirection.BUY,
        fair_value=Decimal("0.90"),
    )


def _action(
    side: Side = Side.BUY,
    price: Decimal = Decimal("0.50"),
    size: Decimal = Decimal("100"),
) -> TradeAction:
    return TradeAction(
        signal=_signal(),
        token_id="0xyes",
        side=side,
        price=price,
        size=size,
    )


def _result(
    side: Side = Side.BUY,
    price: Decimal = Decimal("0.50"),
    size: Decimal = Decimal("100"),
) -> ExecutionResult:
    return ExecutionResult(
        action=_action(side, price, size),
        success=True,
        fill_price=price,
        fill_size=size,
        executed_at=time.time(),
    )


def _position(
    side: Side = Side.BUY,
    entry_price: Decimal = Decimal("0.50"),
    size: Decimal = Decimal("100"),
) -> Position:
    return Position(
        token_id="0xyes",
        condition_id="cond1",
        side=side,
        entry_price=entry_price,
        size=size,
    )


# ── Realized P&L ──────────────────────────────────────────────


class TestRealizedPnL:
    def test_opening_fill_zero(self) -> None:
        pnl = PnLTracker()
        result = pnl.record_fill(_result(), None)
        assert result == Decimal(0)

    def test_closing_buy_profit(self) -> None:
        """Bought at 0.40, sell at 0.60 → profit."""
        pnl = PnLTracker()
        pos = _position(side=Side.BUY, entry_price=Decimal("0.40"))
        result = pnl.record_fill(
            _result(side=Side.SELL, price=Decimal("0.60"), size=Decimal("100")),
            pos,
        )
        assert result == Decimal("20")  # (0.60 - 0.40) * 100

    def test_closing_buy_loss(self) -> None:
        """Bought at 0.60, sell at 0.40 → loss."""
        pnl = PnLTracker()
        pos = _position(side=Side.BUY, entry_price=Decimal("0.60"))
        result = pnl.record_fill(
            _result(side=Side.SELL, price=Decimal("0.40"), size=Decimal("100")),
            pos,
        )
        assert result == Decimal("-20")

    def test_closing_sell_profit(self) -> None:
        """Sold at 0.70, buy to close at 0.50 → profit."""
        pnl = PnLTracker()
        pos = _position(side=Side.SELL, entry_price=Decimal("0.70"))
        result = pnl.record_fill(
            _result(side=Side.BUY, price=Decimal("0.50"), size=Decimal("100")),
            pos,
        )
        assert result == Decimal("20")

    def test_closing_sell_loss(self) -> None:
        """Sold at 0.40, buy to close at 0.60 → loss."""
        pnl = PnLTracker()
        pos = _position(side=Side.SELL, entry_price=Decimal("0.40"))
        result = pnl.record_fill(
            _result(side=Side.BUY, price=Decimal("0.60"), size=Decimal("100")),
            pos,
        )
        assert result == Decimal("-20")

    def test_partial_close(self) -> None:
        """Close only part of position."""
        pnl = PnLTracker()
        pos = _position(side=Side.BUY, entry_price=Decimal("0.40"), size=Decimal("200"))
        result = pnl.record_fill(
            _result(side=Side.SELL, price=Decimal("0.60"), size=Decimal("100")),
            pos,
        )
        assert result == Decimal("20")  # (0.60-0.40)*100

    def test_accumulates(self) -> None:
        pnl = PnLTracker()
        pos = _position(side=Side.BUY, entry_price=Decimal("0.40"))
        pnl.record_fill(
            _result(side=Side.SELL, price=Decimal("0.60"), size=Decimal("100")),
            pos,
        )
        pnl.record_fill(
            _result(side=Side.SELL, price=Decimal("0.50"), size=Decimal("100")),
            pos,
        )
        assert pnl.realized_today == Decimal("30")
        assert pnl.realized_total == Decimal("30")


# ── Daily Reset ────────────────────────────────────────────────


class TestDailyReset:
    def test_auto_resets_on_new_day(self) -> None:
        pnl = PnLTracker()
        pos = _position(side=Side.BUY, entry_price=Decimal("0.40"))
        pnl.record_fill(
            _result(side=Side.SELL, price=Decimal("0.60"), size=Decimal("100")),
            pos,
        )
        assert pnl.realized_today == Decimal("20")

        # Simulate next day
        pnl._day_start -= 86400
        pnl.record_fill(_result(), None)
        assert pnl.realized_today == Decimal(0)

    def test_count_resets(self) -> None:
        pnl = PnLTracker()
        pnl.record_fill(_result(), None)
        assert pnl.trade_count_today == 1

        pnl._day_start -= 86400
        pnl.record_fill(_result(), None)
        assert pnl.trade_count_today == 1  # reset then incremented

    def test_total_not_reset(self) -> None:
        pnl = PnLTracker()
        pos = _position(side=Side.BUY, entry_price=Decimal("0.40"))
        pnl.record_fill(
            _result(side=Side.SELL, price=Decimal("0.60"), size=Decimal("100")),
            pos,
        )
        pnl._day_start -= 86400
        pnl.record_fill(_result(), None)
        assert pnl.realized_total == Decimal("20")

    def test_day_start_set(self) -> None:
        pnl = PnLTracker()
        now = time.time()
        expected = now - (now % 86400)
        assert abs(pnl._day_start - expected) < 2


# ── Unrealized P&L ────────────────────────────────────────────


class TestUnrealizedPnL:
    def test_buy_profit(self) -> None:
        pnl = PnLTracker()
        positions = {
            "0xa": _position(side=Side.BUY, entry_price=Decimal("0.40")),
        }
        prices = {"0xa": Decimal("0.60")}
        assert pnl.unrealized_pnl(positions, prices) == Decimal("20")

    def test_buy_loss(self) -> None:
        pnl = PnLTracker()
        positions = {
            "0xa": _position(side=Side.BUY, entry_price=Decimal("0.60")),
        }
        prices = {"0xa": Decimal("0.40")}
        assert pnl.unrealized_pnl(positions, prices) == Decimal("-20")

    def test_sell_unrealized(self) -> None:
        pnl = PnLTracker()
        positions = {
            "0xa": _position(side=Side.SELL, entry_price=Decimal("0.70")),
        }
        prices = {"0xa": Decimal("0.50")}
        assert pnl.unrealized_pnl(positions, prices) == Decimal("20")

    def test_missing_price_skipped(self) -> None:
        pnl = PnLTracker()
        positions = {
            "0xa": _position(side=Side.BUY, entry_price=Decimal("0.50")),
        }
        prices: dict[str, Decimal] = {}
        assert pnl.unrealized_pnl(positions, prices) == Decimal(0)

    def test_empty_positions(self) -> None:
        pnl = PnLTracker()
        assert pnl.unrealized_pnl({}, {}) == Decimal(0)


# ── Reset ──────────────────────────────────────────────────────


class TestReset:
    def test_reset_clears_all(self) -> None:
        pnl = PnLTracker()
        pnl.realized_today = Decimal("100")
        pnl.realized_total = Decimal("200")
        pnl.trade_count_today = 5
        pnl.reset()
        assert pnl.realized_today == Decimal(0)
        assert pnl.realized_total == Decimal(0)
        assert pnl.trade_count_today == 0

    def test_trade_count_increments(self) -> None:
        pnl = PnLTracker()
        pnl.record_fill(_result(), None)
        pnl.record_fill(_result(), None)
        assert pnl.trade_count_today == 2
