"""Tests for SignalGenerator — fair value calculation and signal emission."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from src.core.config import StrategyConfig
from src.core.types import (
    FeedEvent,
    FeedEventType,
    FeedType,
    MarketCategory,
    MarketOpportunity,
    MatchResult,
    OutcomeType,
    SignalDirection,
)
from src.strategy.signals import SignalGenerator, _determine_direction_and_price

# ── Helpers ─────────────────────────────────────────────────────


def _tokens() -> list[dict[str, Any]]:
    return [
        {"token_id": "0xyes", "outcome": "Yes"},
        {"token_id": "0xno", "outcome": "No"},
    ]


def _cfg(**overrides: object) -> StrategyConfig:
    defaults: dict[str, object] = {
        "min_edge": 0.05,
        "min_confidence": 0.80,
        "max_staleness_secs": 60.0,
    }
    defaults.update(overrides)
    return StrategyConfig(**defaults)  # type: ignore[arg-type]


def _match(
    feed_type: FeedType = FeedType.ECONOMIC,
    outcome_type: OutcomeType = OutcomeType.NUMERIC,
    indicator: str = "CPI",
    numeric_value: Decimal | None = Decimal("3.5"),
    best_bid: Decimal | None = Decimal("0.40"),
    best_ask: Decimal | None = Decimal("0.45"),
    depth_usd: Decimal = Decimal("5000"),
    received_at: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> MatchResult:
    if received_at is None:
        received_at = time.time()
    return MatchResult(
        feed_event=FeedEvent(
            feed_type=feed_type,
            event_type=FeedEventType.DATA_RELEASED,
            indicator=indicator,
            value=str(numeric_value) if numeric_value else "",
            numeric_value=numeric_value,
            outcome_type=outcome_type,
            received_at=received_at,
            metadata=metadata or {},
        ),
        opportunity=MarketOpportunity(
            condition_id="cond1",
            question=f"Will {indicator} be above 3.0%?",
            category=MarketCategory.ECONOMIC,
            tokens=_tokens(),
            best_bid=best_bid,
            best_ask=best_ask,
            depth_usd=depth_usd,
        ),
        target_token_id="0xyes",
        target_outcome="Yes",
        match_confidence=0.95,
        match_reason="test",
    )


# ── _determine_direction_and_price ─────────────────────────────


class TestDetermineDirection:
    def test_buy_when_fair_above_ask(self) -> None:
        result = _determine_direction_and_price(
            Decimal("0.99"), Decimal("0.40"), Decimal("0.45"),
        )
        assert result is not None
        assert result[0] == SignalDirection.BUY
        assert result[1] == Decimal("0.45")

    def test_sell_when_fair_below_bid(self) -> None:
        result = _determine_direction_and_price(
            Decimal("0.10"), Decimal("0.40"), Decimal("0.45"),
        )
        assert result is not None
        assert result[0] == SignalDirection.SELL
        assert result[1] == Decimal("0.40")

    def test_no_edge_fair_between_bid_ask(self) -> None:
        result = _determine_direction_and_price(
            Decimal("0.42"), Decimal("0.40"), Decimal("0.45"),
        )
        assert result is None

    def test_no_ask(self) -> None:
        """No ask side → can only sell."""
        result = _determine_direction_and_price(
            Decimal("0.10"), Decimal("0.40"), None,
        )
        assert result is not None
        assert result[0] == SignalDirection.SELL

    def test_no_bid(self) -> None:
        """No bid side → can only buy."""
        result = _determine_direction_and_price(
            Decimal("0.99"), None, Decimal("0.45"),
        )
        assert result is not None
        assert result[0] == SignalDirection.BUY


# ── Categorical (sports) signals ───────────────────────────────


class TestCategorical:
    def test_buy_winner_token(self) -> None:
        """Winning team's token is underpriced → BUY signal."""
        gen = SignalGenerator(_cfg(min_edge=0.05))
        m = _match(
            feed_type=FeedType.SPORTS,
            outcome_type=OutcomeType.CATEGORICAL,
            best_ask=Decimal("0.50"),
        )

        signal = gen.evaluate(m)
        assert signal is not None
        assert signal.direction == SignalDirection.BUY
        assert signal.fair_value == Decimal("0.99")
        assert signal.edge == Decimal("0.49")

    def test_already_priced_no_signal(self) -> None:
        """Token already at fair value → no signal."""
        gen = SignalGenerator(_cfg(min_edge=0.05))
        m = _match(
            feed_type=FeedType.SPORTS,
            outcome_type=OutcomeType.CATEGORICAL,
            best_bid=Decimal("0.98"),
            best_ask=Decimal("0.99"),
        )

        signal = gen.evaluate(m)
        # Fair value 0.99, ask 0.99 → no edge to buy
        # Fair value 0.99, bid 0.98 → no edge to sell (0.99 > 0.98)
        assert signal is None

    def test_edge_calculation(self) -> None:
        gen = SignalGenerator(_cfg(min_edge=0.01))
        m = _match(
            feed_type=FeedType.SPORTS,
            outcome_type=OutcomeType.CATEGORICAL,
            best_ask=Decimal("0.70"),
        )

        signal = gen.evaluate(m)
        assert signal is not None
        assert signal.edge == Decimal("0.29")

    def test_min_edge_filter(self) -> None:
        """Signal rejected when edge is below threshold."""
        gen = SignalGenerator(_cfg(min_edge=0.60))
        m = _match(
            feed_type=FeedType.SPORTS,
            outcome_type=OutcomeType.CATEGORICAL,
            best_ask=Decimal("0.50"),
        )

        signal = gen.evaluate(m)
        assert signal is None


# ── Numeric threshold signals ──────────────────────────────────


class TestNumericThreshold:
    def test_economic_above_threshold(self) -> None:
        gen = SignalGenerator(_cfg(min_edge=0.05))
        m = _match(
            feed_type=FeedType.ECONOMIC,
            outcome_type=OutcomeType.NUMERIC,
            best_ask=Decimal("0.50"),
        )

        signal = gen.evaluate(m)
        assert signal is not None
        assert signal.direction == SignalDirection.BUY
        assert signal.confidence == 0.99

    def test_crypto_lower_confidence(self) -> None:
        """Crypto signals have lower confidence (price can revert)."""
        gen = SignalGenerator(_cfg(min_edge=0.05, min_confidence=0.50))
        m = _match(
            feed_type=FeedType.CRYPTO,
            outcome_type=OutcomeType.NUMERIC,
            best_ask=Decimal("0.50"),
        )

        signal = gen.evaluate(m)
        assert signal is not None
        assert signal.confidence == 0.85

    def test_crypto_cross_validated(self) -> None:
        """Cross-validated crypto gets higher confidence."""
        gen = SignalGenerator(_cfg(min_edge=0.05, min_confidence=0.50))
        m = _match(
            feed_type=FeedType.CRYPTO,
            outcome_type=OutcomeType.NUMERIC,
            best_ask=Decimal("0.50"),
            metadata={"cross_validated": True},
        )

        signal = gen.evaluate(m)
        assert signal is not None
        assert signal.confidence == 0.92

    def test_no_edge(self) -> None:
        """Fair value between bid/ask → no signal."""
        gen = SignalGenerator(_cfg(min_edge=0.05))
        m = _match(
            best_bid=Decimal("0.98"),
            best_ask=Decimal("0.99"),
        )

        signal = gen.evaluate(m)
        assert signal is None

    def test_edge_below_min(self) -> None:
        gen = SignalGenerator(_cfg(min_edge=0.60))
        m = _match(best_ask=Decimal("0.50"))

        signal = gen.evaluate(m)
        assert signal is None


# ── Evaluate dispatch ──────────────────────────────────────────


class TestEvaluateDispatch:
    def test_routes_categorical(self) -> None:
        gen = SignalGenerator(_cfg(min_edge=0.01))
        m = _match(
            feed_type=FeedType.SPORTS,
            outcome_type=OutcomeType.CATEGORICAL,
            best_ask=Decimal("0.50"),
        )
        signal = gen.evaluate(m)
        assert signal is not None

    def test_routes_numeric(self) -> None:
        gen = SignalGenerator(_cfg(min_edge=0.01))
        m = _match(
            feed_type=FeedType.ECONOMIC,
            outcome_type=OutcomeType.NUMERIC,
            best_ask=Decimal("0.50"),
        )
        signal = gen.evaluate(m)
        assert signal is not None

    def test_routes_boolean(self) -> None:
        gen = SignalGenerator(_cfg(min_edge=0.01))
        m = _match(
            outcome_type=OutcomeType.BOOLEAN,
            best_ask=Decimal("0.50"),
        )
        signal = gen.evaluate(m)
        assert signal is not None

    def test_stale_rejected(self) -> None:
        """Events older than max_staleness_secs are rejected."""
        gen = SignalGenerator(_cfg(max_staleness_secs=10.0))
        m = _match(received_at=time.time() - 100.0, best_ask=Decimal("0.50"))

        signal = gen.evaluate(m)
        assert signal is None

    def test_per_category_edge(self) -> None:
        """Per-category min_edge overrides global."""
        gen = SignalGenerator(_cfg(min_edge=0.01, economic_min_edge=0.60))
        m = _match(
            feed_type=FeedType.ECONOMIC,
            outcome_type=OutcomeType.NUMERIC,
            best_ask=Decimal("0.50"),
        )
        # Edge is 0.49 < economic_min_edge 0.60
        signal = gen.evaluate(m)
        assert signal is None

    def test_per_category_edge_allows(self) -> None:
        """Per-category min_edge set low allows signal."""
        gen = SignalGenerator(_cfg(min_edge=0.60, sports_min_edge=0.01))
        m = _match(
            feed_type=FeedType.SPORTS,
            outcome_type=OutcomeType.CATEGORICAL,
            best_ask=Decimal("0.50"),
        )
        signal = gen.evaluate(m)
        assert signal is not None
