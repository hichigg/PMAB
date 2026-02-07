"""Tests for backtest types — scenario construction and serialization."""

from __future__ import annotations

from decimal import Decimal

from src.backtest.types import (
    BacktestConfig,
    BacktestResult,
    HistoricalEvent,
    Scenario,
)
from src.core.types import (
    FeedEvent,
    FeedEventType,
    FeedType,
    MarketCategory,
    MarketOpportunity,
    OrderBook,
    OutcomeType,
    PriceLevel,
)


# ── HistoricalEvent ─────────────────────────────────────────────


class TestHistoricalEvent:
    def test_basic_creation(self) -> None:
        ev = HistoricalEvent(
            feed_event=FeedEvent(
                feed_type=FeedType.ECONOMIC,
                event_type=FeedEventType.DATA_RELEASED,
                indicator="CPI",
                value="3.2",
                numeric_value=Decimal("3.2"),
                released_at=1000.0,
                received_at=1000.5,
            ),
        )
        assert ev.feed_event.indicator == "CPI"
        assert ev.orderbooks == {}

    def test_with_orderbook(self) -> None:
        book = OrderBook(
            token_id="tok_1",
            bids=[PriceLevel(price=Decimal("0.85"), size=Decimal("1000"))],
            asks=[PriceLevel(price=Decimal("0.87"), size=Decimal("1000"))],
        )
        ev = HistoricalEvent(
            feed_event=FeedEvent(
                feed_type=FeedType.ECONOMIC,
                event_type=FeedEventType.DATA_RELEASED,
            ),
            orderbooks={"tok_1": book},
        )
        assert "tok_1" in ev.orderbooks
        assert ev.orderbooks["tok_1"].best_bid == Decimal("0.85")


# ── Scenario ────────────────────────────────────────────────────


class TestScenario:
    def test_empty_scenario(self) -> None:
        s = Scenario(name="empty")
        assert s.name == "empty"
        assert s.events == []
        assert s.opportunities == {}

    def test_scenario_with_data(self) -> None:
        opp = MarketOpportunity(
            condition_id="cond_1",
            question="Will CPI be above 3.0%?",
            category=MarketCategory.ECONOMIC,
            tokens=[
                {"token_id": "tok_yes", "outcome": "Yes"},
                {"token_id": "tok_no", "outcome": "No"},
            ],
            token_id="tok_yes",
            best_bid=Decimal("0.85"),
            best_ask=Decimal("0.87"),
            depth_usd=Decimal("5000"),
        )
        ev = HistoricalEvent(
            feed_event=FeedEvent(
                feed_type=FeedType.ECONOMIC,
                event_type=FeedEventType.DATA_RELEASED,
                indicator="CPI",
                numeric_value=Decimal("3.2"),
                outcome_type=OutcomeType.NUMERIC,
                released_at=1000.0,
                received_at=1000.5,
            ),
        )
        s = Scenario(
            name="CPI test",
            opportunities={"cond_1": opp},
            events=[ev],
        )
        assert len(s.events) == 1
        assert "cond_1" in s.opportunities

    def test_scenario_json_round_trip(self) -> None:
        opp = MarketOpportunity(
            condition_id="cond_1",
            question="Test",
            category=MarketCategory.ECONOMIC,
            token_id="tok_1",
            best_bid=Decimal("0.80"),
            best_ask=Decimal("0.85"),
            depth_usd=Decimal("3000"),
        )
        ev = HistoricalEvent(
            feed_event=FeedEvent(
                feed_type=FeedType.ECONOMIC,
                event_type=FeedEventType.DATA_RELEASED,
                indicator="CPI",
            ),
        )
        s = Scenario(name="roundtrip", opportunities={"cond_1": opp}, events=[ev])
        json_str = s.model_dump_json()
        s2 = Scenario.model_validate_json(json_str)
        assert s2.name == "roundtrip"
        assert s2.opportunities["cond_1"].condition_id == "cond_1"
        assert len(s2.events) == 1


# ── BacktestConfig ──────────────────────────────────────────────


class TestBacktestConfig:
    def test_defaults(self) -> None:
        cfg = BacktestConfig()
        assert cfg.fill_probability == 1.0
        assert cfg.slippage_bps == 0

    def test_overrides(self) -> None:
        cfg = BacktestConfig(fill_probability=0.9, slippage_bps=5)
        assert cfg.fill_probability == 0.9
        assert cfg.slippage_bps == 5


# ── BacktestResult ──────────────────────────────────────────────


class TestBacktestResult:
    def test_defaults(self) -> None:
        r = BacktestResult()
        assert r.total_trades == 0
        assert r.cumulative_pnl == Decimal(0)
        assert r.win_rate == 0.0

    def test_populated(self) -> None:
        r = BacktestResult(
            scenario_name="test",
            total_events=5,
            total_trades=3,
            successful_trades=2,
            failed_trades=1,
            cumulative_pnl=Decimal("42.50"),
            win_rate=0.667,
        )
        assert r.scenario_name == "test"
        assert r.total_events == 5
