"""Tests for PositionTracker — open, average, reduce, close, exposure."""

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
    Side,
    Signal,
    SignalDirection,
    TradeAction,
)
from src.risk.positions import PositionTracker

# ── Helpers ─────────────────────────────────────────────────────


def _tokens() -> list[dict[str, Any]]:
    return [
        {"token_id": "0xyes", "outcome": "Yes"},
        {"token_id": "0xno", "outcome": "No"},
    ]


def _signal(token_id: str = "0xyes", condition_id: str = "cond1") -> Signal:
    return Signal(
        match=MatchResult(
            feed_event=FeedEvent(
                feed_type=FeedType.ECONOMIC,
                event_type=FeedEventType.DATA_RELEASED,
                indicator="CPI",
                outcome_type=OutcomeType.NUMERIC,
            ),
            opportunity=MarketOpportunity(
                condition_id=condition_id,
                tokens=_tokens(),
                category=MarketCategory.ECONOMIC,
            ),
            target_token_id=token_id,
        ),
        direction=SignalDirection.BUY,
        fair_value=Decimal("0.90"),
        confidence=0.99,
        edge=Decimal("0.10"),
        current_price=Decimal("0.50"),
        created_at=time.time(),
    )


def _action(
    token_id: str = "0xyes",
    side: Side = Side.BUY,
    price: Decimal = Decimal("0.50"),
    size: Decimal = Decimal("100"),
    condition_id: str = "cond1",
) -> TradeAction:
    return TradeAction(
        signal=_signal(token_id, condition_id),
        token_id=token_id,
        side=side,
        price=price,
        size=size,
    )


def _result(
    token_id: str = "0xyes",
    side: Side = Side.BUY,
    price: Decimal = Decimal("0.50"),
    size: Decimal = Decimal("100"),
    condition_id: str = "cond1",
) -> ExecutionResult:
    action = _action(token_id, side, price, size, condition_id)
    return ExecutionResult(
        action=action,
        success=True,
        fill_price=price,
        fill_size=size,
        executed_at=time.time(),
    )


# ── Open Position ──────────────────────────────────────────────


class TestOpenPosition:
    def test_open_new(self) -> None:
        tracker = PositionTracker()
        pos = tracker.record_fill(_result())
        assert pos is not None
        assert pos.token_id == "0xyes"
        assert pos.side == Side.BUY
        assert pos.entry_price == Decimal("0.50")
        assert pos.size == Decimal("100")

    def test_open_sets_condition_id(self) -> None:
        tracker = PositionTracker()
        pos = tracker.record_fill(_result(condition_id="cond42"))
        assert pos is not None
        assert pos.condition_id == "cond42"

    def test_open_multiple(self) -> None:
        tracker = PositionTracker()
        tracker.record_fill(_result(token_id="0xa"))
        tracker.record_fill(_result(token_id="0xb"))
        assert tracker.count == 2

    def test_count_starts_zero(self) -> None:
        tracker = PositionTracker()
        assert tracker.count == 0


# ── Average In ─────────────────────────────────────────────────


class TestAverageIn:
    def test_weighted_price(self) -> None:
        tracker = PositionTracker()
        tracker.record_fill(_result(price=Decimal("0.40"), size=Decimal("100")))
        pos = tracker.record_fill(
            _result(price=Decimal("0.60"), size=Decimal("100"))
        )
        assert pos is not None
        assert pos.entry_price == Decimal("0.50")  # (40+60)/200*100 each
        assert pos.size == Decimal("200")

    def test_cumulative_size(self) -> None:
        tracker = PositionTracker()
        tracker.record_fill(_result(size=Decimal("50")))
        pos = tracker.record_fill(_result(size=Decimal("30")))
        assert pos is not None
        assert pos.size == Decimal("80")

    def test_last_updated(self) -> None:
        tracker = PositionTracker()
        tracker.record_fill(_result())
        pos1 = tracker.get("0xyes")
        assert pos1 is not None
        t1 = pos1.last_updated

        tracker.record_fill(_result())
        pos2 = tracker.get("0xyes")
        assert pos2 is not None
        assert pos2.last_updated >= t1


# ── Reduce / Close ────────────────────────────────────────────


class TestReduceClose:
    def test_reduce_partial(self) -> None:
        tracker = PositionTracker()
        tracker.record_fill(_result(side=Side.BUY, size=Decimal("100")))
        pos = tracker.record_fill(
            _result(side=Side.SELL, size=Decimal("40"))
        )
        assert pos is not None
        assert pos.size == Decimal("60")
        assert tracker.count == 1

    def test_close_exact(self) -> None:
        tracker = PositionTracker()
        tracker.record_fill(_result(side=Side.BUY, size=Decimal("100")))
        pos = tracker.record_fill(
            _result(side=Side.SELL, size=Decimal("100"))
        )
        assert pos is None
        assert tracker.count == 0

    def test_close_larger_fill(self) -> None:
        tracker = PositionTracker()
        tracker.record_fill(_result(side=Side.BUY, size=Decimal("50")))
        pos = tracker.record_fill(
            _result(side=Side.SELL, size=Decimal("100"))
        )
        assert pos is None
        assert tracker.count == 0


# ── Exposure ───────────────────────────────────────────────────


class TestExposure:
    def test_total_exposure(self) -> None:
        tracker = PositionTracker()
        tracker.record_fill(_result(token_id="0xa", price=Decimal("0.50"), size=Decimal("100")))
        tracker.record_fill(_result(token_id="0xb", price=Decimal("0.30"), size=Decimal("200")))
        assert tracker.total_exposure_usd() == Decimal("110")  # 50 + 60

    def test_condition_exposure(self) -> None:
        tracker = PositionTracker()
        tracker.record_fill(_result(token_id="0xa", condition_id="c1"))
        tracker.record_fill(_result(token_id="0xb", condition_id="c2"))
        # Only c1: 0.50 * 100 = 50
        assert tracker.exposure_for_condition("c1") == Decimal("50")

    def test_empty_exposure(self) -> None:
        tracker = PositionTracker()
        assert tracker.total_exposure_usd() == Decimal(0)

    def test_ignores_other_conditions(self) -> None:
        tracker = PositionTracker()
        tracker.record_fill(_result(condition_id="c2"))
        assert tracker.exposure_for_condition("c1") == Decimal(0)


# ── Lookup / Clear ─────────────────────────────────────────────


class TestLookupClear:
    def test_get_existing(self) -> None:
        tracker = PositionTracker()
        tracker.record_fill(_result())
        assert tracker.get("0xyes") is not None

    def test_get_none(self) -> None:
        tracker = PositionTracker()
        assert tracker.get("nonexistent") is None

    def test_clear(self) -> None:
        tracker = PositionTracker()
        tracker.record_fill(_result())
        tracker.clear()
        assert tracker.count == 0

    def test_positions_copy(self) -> None:
        tracker = PositionTracker()
        tracker.record_fill(_result())
        copy = tracker.positions
        copy.clear()
        assert tracker.count == 1  # original unaffected
