"""Tests for PositionSizer — position sizing logic."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from src.core.config import RiskConfig, StrategyConfig
from src.core.types import (
    FeedEvent,
    FeedEventType,
    FeedType,
    MarketCategory,
    MarketOpportunity,
    MatchResult,
    OrderType,
    OutcomeType,
    Side,
    Signal,
    SignalDirection,
)
from src.strategy.sizer import PositionSizer

# ── Helpers ─────────────────────────────────────────────────────


def _tokens() -> list[dict[str, Any]]:
    return [
        {"token_id": "0xyes", "outcome": "Yes"},
        {"token_id": "0xno", "outcome": "No"},
    ]


def _cfg(**overrides: object) -> StrategyConfig:
    defaults: dict[str, object] = {
        "base_size_usd": 100.0,
        "max_size_usd": 1000.0,
        "min_edge": 0.05,
        "max_slippage": 0.02,
        "default_order_type": "FOK",
        "use_kelly_sizing": False,
        "kelly_fraction": 0.25,
    }
    defaults.update(overrides)
    return StrategyConfig(**defaults)  # type: ignore[arg-type]


def _risk(**overrides: object) -> RiskConfig:
    defaults: dict[str, object] = {
        "min_profit_usd": 5.0,
        "max_position_usd": 5000.0,
    }
    defaults.update(overrides)
    return RiskConfig(**defaults)  # type: ignore[arg-type]


def _signal(
    edge: Decimal = Decimal("0.50"),
    current_price: Decimal = Decimal("0.50"),
    confidence: float = 0.99,
    direction: SignalDirection = SignalDirection.BUY,
    depth_usd: Decimal = Decimal("5000"),
) -> Signal:
    return Signal(
        match=MatchResult(
            feed_event=FeedEvent(
                feed_type=FeedType.ECONOMIC,
                event_type=FeedEventType.DATA_RELEASED,
                indicator="CPI",
                value="3.5",
                numeric_value=Decimal("3.5"),
                outcome_type=OutcomeType.NUMERIC,
                received_at=time.time(),
            ),
            opportunity=MarketOpportunity(
                condition_id="cond1",
                question="Will CPI be above 3.0%?",
                category=MarketCategory.ECONOMIC,
                tokens=_tokens(),
                best_bid=Decimal("0.40"),
                best_ask=current_price,
                depth_usd=depth_usd,
            ),
            target_token_id="0xyes",
            target_outcome="Yes",
            match_confidence=0.95,
        ),
        fair_value=Decimal("0.99"),
        confidence=confidence,
        direction=direction,
        edge=edge,
        current_price=current_price,
        created_at=time.time(),
    )


# ── Tests ──────────────────────────────────────────────────────


class TestPositionSizer:
    def test_basic_sizing(self) -> None:
        """Base case: produces a trade action with correct fields."""
        sizer = PositionSizer(_cfg(), _risk(min_profit_usd=1.0))
        sig = _signal(edge=Decimal("0.50"), current_price=Decimal("0.50"))

        action = sizer.size(sig)
        assert action is not None
        assert action.token_id == "0xyes"
        assert action.side == Side.BUY
        assert action.size > Decimal("0")
        assert action.order_type == OrderType.FOK

    def test_size_capped_at_max(self) -> None:
        """Size should not exceed max_size_usd worth of tokens."""
        sizer = PositionSizer(
            _cfg(base_size_usd=2000.0, max_size_usd=500.0),
            _risk(min_profit_usd=1.0),
        )
        sig = _signal(edge=Decimal("0.50"), current_price=Decimal("0.50"))

        action = sizer.size(sig)
        assert action is not None
        # Size in tokens = size_usd / price. Max $500 / $0.50 = 1000 tokens
        # But also capped by depth (20% of 5000 = 1000 USD → 2000 tokens)
        # So cap is min(500, 1000) = 500 USD → 1000 tokens
        assert action.size <= Decimal("1000")

    def test_min_profit_filter(self) -> None:
        """Returns None when estimated profit is below min_profit_usd."""
        sizer = PositionSizer(
            _cfg(base_size_usd=10.0),
            _risk(min_profit_usd=100.0),
        )
        sig = _signal(edge=Decimal("0.10"), current_price=Decimal("0.50"))

        action = sizer.size(sig)
        assert action is None

    def test_depth_cap(self) -> None:
        """Size capped to 20% of orderbook depth."""
        sizer = PositionSizer(
            _cfg(base_size_usd=500.0, max_size_usd=5000.0),
            _risk(min_profit_usd=1.0),
        )
        # Only $100 depth → 20% = $20 max
        sig = _signal(
            edge=Decimal("0.50"),
            current_price=Decimal("0.50"),
            depth_usd=Decimal("100"),
        )

        action = sizer.size(sig)
        assert action is not None
        # $20 / $0.50 = 40 tokens max
        assert action.size <= Decimal("40")

    def test_kelly_sizing(self) -> None:
        """Kelly sizing produces different size than base."""
        sizer = PositionSizer(
            _cfg(use_kelly_sizing=True, kelly_fraction=0.25, max_size_usd=1000.0),
            _risk(min_profit_usd=1.0),
        )
        sig = _signal(
            edge=Decimal("0.50"),
            current_price=Decimal("0.50"),
            confidence=0.99,
        )

        action = sizer.size(sig)
        assert action is not None
        assert action.size > Decimal("0")

    def test_zero_edge_no_action(self) -> None:
        """Zero edge → zero profit → below min_profit → None."""
        sizer = PositionSizer(_cfg(), _risk(min_profit_usd=1.0))
        sig = _signal(edge=Decimal("0"), current_price=Decimal("0.50"))

        action = sizer.size(sig)
        assert action is None

    def test_sell_side(self) -> None:
        """SELL direction produces Side.SELL."""
        sizer = PositionSizer(_cfg(), _risk(min_profit_usd=1.0))
        sig = _signal(
            edge=Decimal("0.50"),
            current_price=Decimal("0.50"),
            direction=SignalDirection.SELL,
        )

        action = sizer.size(sig)
        assert action is not None
        assert action.side == Side.SELL

    def test_gtc_order_type(self) -> None:
        """GTC order type config produces OrderType.GTC."""
        sizer = PositionSizer(
            _cfg(default_order_type="GTC"),
            _risk(min_profit_usd=1.0),
        )
        sig = _signal(edge=Decimal("0.50"), current_price=Decimal("0.50"))

        action = sizer.size(sig)
        assert action is not None
        assert action.order_type == OrderType.GTC

    def test_zero_depth_no_action(self) -> None:
        """Zero depth → capped to zero → None."""
        sizer = PositionSizer(_cfg(), _risk(min_profit_usd=1.0))
        sig = _signal(
            edge=Decimal("0.50"),
            current_price=Decimal("0.50"),
            depth_usd=Decimal("0"),
        )

        action = sizer.size(sig)
        assert action is None

    def test_zero_price_no_action(self) -> None:
        """Zero current_price → division error → None."""
        sizer = PositionSizer(_cfg(), _risk(min_profit_usd=1.0))
        sig = _signal(edge=Decimal("0.50"), current_price=Decimal("0"))

        action = sizer.size(sig)
        assert action is None

    def test_max_slippage_set(self) -> None:
        """Max slippage from config is applied to action."""
        sizer = PositionSizer(
            _cfg(max_slippage=0.05),
            _risk(min_profit_usd=1.0),
        )
        sig = _signal(edge=Decimal("0.50"), current_price=Decimal("0.50"))

        action = sizer.size(sig)
        assert action is not None
        assert action.max_slippage == Decimal("0.05")

    def test_estimated_profit(self) -> None:
        """Estimated profit = size_tokens * edge."""
        sizer = PositionSizer(
            _cfg(base_size_usd=100.0),
            _risk(min_profit_usd=1.0),
        )
        sig = _signal(edge=Decimal("0.50"), current_price=Decimal("0.50"))

        action = sizer.size(sig)
        assert action is not None
        expected_profit = action.size * Decimal("0.50")
        assert action.estimated_profit_usd == expected_profit
