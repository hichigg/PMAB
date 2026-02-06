"""Tests for risk gate functions — stateless checks returning RiskVerdict."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.core.config import RiskConfig
from src.core.types import (
    FeedEvent,
    FeedEventType,
    FeedType,
    MarketCategory,
    MarketOpportunity,
    MatchResult,
    OutcomeType,
    Position,
    RiskRejectionReason,
    Side,
    Signal,
    SignalDirection,
    TradeAction,
)
from src.risk.gates import (
    check_daily_loss,
    check_kill_switch,
    check_max_concurrent_positions,
    check_orderbook_depth,
    check_position_concentration,
    check_spread,
)
from src.risk.pnl import PnLTracker

# ── Helpers ─────────────────────────────────────────────────────


def _tokens() -> list[dict[str, Any]]:
    return [{"token_id": "0xyes", "outcome": "Yes"}]


def _signal(
    condition_id: str = "cond1",
    depth_usd: Decimal = Decimal("5000"),
    spread: Decimal | None = Decimal("0.05"),
) -> Signal:
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
                depth_usd=depth_usd,
                spread=spread,
                best_bid=Decimal("0.45"),
                best_ask=Decimal("0.50"),
            ),
        ),
        direction=SignalDirection.BUY,
        fair_value=Decimal("0.90"),
    )


def _action(
    condition_id: str = "cond1",
    price: Decimal = Decimal("0.50"),
    size: Decimal = Decimal("100"),
    depth_usd: Decimal = Decimal("5000"),
    spread: Decimal | None = Decimal("0.05"),
) -> TradeAction:
    return TradeAction(
        signal=_signal(condition_id, depth_usd, spread),
        token_id="0xyes",
        side=Side.BUY,
        price=price,
        size=size,
    )


def _cfg(**overrides: object) -> RiskConfig:
    defaults: dict[str, object] = {
        "max_daily_loss_usd": 500.0,
        "max_position_usd": 5000.0,
        "max_bankroll_pct_per_event": 0.20,
        "bankroll_usd": 10000.0,
        "max_concurrent_positions": 10,
        "min_orderbook_depth_usd": 500.0,
        "max_spread": 0.10,
    }
    defaults.update(overrides)
    return RiskConfig(**defaults)  # type: ignore[arg-type]


# ── Kill Switch ────────────────────────────────────────────────


class TestKillSwitch:
    def test_approved_when_not_killed(self) -> None:
        v = check_kill_switch(False)
        assert v.approved is True

    def test_rejected_when_killed(self) -> None:
        v = check_kill_switch(True)
        assert v.approved is False

    def test_reason(self) -> None:
        v = check_kill_switch(True)
        assert v.reason == RiskRejectionReason.KILL_SWITCH_ACTIVE


# ── Daily Loss ─────────────────────────────────────────────────


class TestDailyLoss:
    def test_approved_within_limit(self) -> None:
        pnl = PnLTracker()
        pnl.realized_today = Decimal("-400")
        v = check_daily_loss(pnl, _cfg(max_daily_loss_usd=500.0))
        assert v.approved is True

    def test_rejected_over_limit(self) -> None:
        pnl = PnLTracker()
        pnl.realized_today = Decimal("-600")
        v = check_daily_loss(pnl, _cfg(max_daily_loss_usd=500.0))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.DAILY_LOSS_LIMIT

    def test_boundary_at_limit(self) -> None:
        """Exactly at limit should pass (not strictly exceeded)."""
        pnl = PnLTracker()
        pnl.realized_today = Decimal("-500")
        v = check_daily_loss(pnl, _cfg(max_daily_loss_usd=500.0))
        assert v.approved is True


# ── Position Concentration ─────────────────────────────────────


class TestPositionConcentration:
    def test_approved_within_limit(self) -> None:
        action = _action(price=Decimal("0.50"), size=Decimal("100"))
        v = check_position_concentration(action, {}, _cfg())
        assert v.approved is True

    def test_rejected_over_limit(self) -> None:
        # bankroll=10000, pct=0.20 → limit=2000
        action = _action(price=Decimal("0.50"), size=Decimal("5000"))
        # 0.50*5000 = 2500 > 2000
        v = check_position_concentration(action, {}, _cfg())
        assert v.approved is False
        assert v.reason == RiskRejectionReason.POSITION_CONCENTRATION

    def test_includes_existing_exposure(self) -> None:
        existing = {
            "0xa": Position(
                token_id="0xa",
                condition_id="cond1",
                side=Side.BUY,
                entry_price=Decimal("0.50"),
                size=Decimal("3000"),  # exposure = 1500
            ),
        }
        # New: 0.50 * 1500 = 750, total = 2250 > 2000
        action = _action(price=Decimal("0.50"), size=Decimal("1500"))
        v = check_position_concentration(action, existing, _cfg())
        assert v.approved is False


# ── Max Concurrent Positions ──────────────────────────────────


class TestMaxConcurrentPositions:
    def test_below_limit(self) -> None:
        positions: dict[str, Position] = {}
        v = check_max_concurrent_positions(positions, _cfg(max_concurrent_positions=10))
        assert v.approved is True

    def test_at_limit(self) -> None:
        positions = {
            f"tok{i}": Position(token_id=f"tok{i}", side=Side.BUY) for i in range(10)
        }
        v = check_max_concurrent_positions(positions, _cfg(max_concurrent_positions=10))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.MAX_CONCURRENT_POSITIONS


# ── Orderbook Depth ───────────────────────────────────────────


class TestOrderbookDepth:
    def test_sufficient_depth(self) -> None:
        action = _action(depth_usd=Decimal("5000"))
        v = check_orderbook_depth(action, _cfg(min_orderbook_depth_usd=500.0))
        assert v.approved is True

    def test_insufficient_depth(self) -> None:
        action = _action(depth_usd=Decimal("100"))
        v = check_orderbook_depth(action, _cfg(min_orderbook_depth_usd=500.0))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.ORDERBOOK_DEPTH


# ── Spread ─────────────────────────────────────────────────────


class TestSpread:
    def test_narrow_spread(self) -> None:
        action = _action(spread=Decimal("0.05"))
        v = check_spread(action, _cfg(max_spread=0.10))
        assert v.approved is True

    def test_wide_spread(self) -> None:
        action = _action(spread=Decimal("0.15"))
        v = check_spread(action, _cfg(max_spread=0.10))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.SPREAD_TOO_WIDE

    def test_none_spread_passes(self) -> None:
        action = _action(spread=None)
        v = check_spread(action, _cfg())
        assert v.approved is True


# ── Reason / Detail ───────────────────────────────────────────


class TestReasonDetail:
    def test_rejection_has_detail(self) -> None:
        v = check_kill_switch(True)
        assert v.detail != ""

    def test_approval_no_reason(self) -> None:
        v = check_kill_switch(False)
        assert v.reason is None
