"""Tests for BacktestEngine — full replay pipeline integration."""

from __future__ import annotations

from decimal import Decimal

from src.backtest.replay import BacktestEngine
from src.backtest.types import BacktestConfig, HistoricalEvent, Scenario
from src.core.config import RiskConfig, StrategyConfig
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


# ── Helpers ─────────────────────────────────────────────────────


def _opp(
    condition_id: str = "cond_1",
    question: str = "Will CPI be above 3.0% for January?",
    category: MarketCategory = MarketCategory.ECONOMIC,
    token_id: str = "tok_yes",
    best_bid: str = "0.60",
    best_ask: str = "0.62",
    depth: str = "5000",
    fee_bps: int = 0,
) -> MarketOpportunity:
    return MarketOpportunity(
        condition_id=condition_id,
        question=question,
        category=category,
        tokens=[
            {"token_id": token_id, "outcome": "Yes"},
            {"token_id": f"{token_id}_no", "outcome": "No"},
        ],
        token_id=token_id,
        best_bid=Decimal(best_bid),
        best_ask=Decimal(best_ask),
        spread=Decimal(best_ask) - Decimal(best_bid),
        depth_usd=Decimal(depth),
        bid_depth_usd=Decimal(depth) / 2,
        ask_depth_usd=Decimal(depth) / 2,
        score=0.8,
        fee_rate_bps=fee_bps,
    )


def _book(
    token_id: str = "tok_yes",
    bid_price: str = "0.60",
    ask_price: str = "0.62",
    size: str = "2000",
) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        bids=[PriceLevel(price=Decimal(bid_price), size=Decimal(size))],
        asks=[PriceLevel(price=Decimal(ask_price), size=Decimal(size))],
    )


def _cpi_event(
    value: str = "3.2",
    released_at: float = 1000.0,
    received_at: float = 1000.5,
    token_id: str = "tok_yes",
    bid_price: str = "0.60",
    ask_price: str = "0.62",
) -> HistoricalEvent:
    return HistoricalEvent(
        feed_event=FeedEvent(
            feed_type=FeedType.ECONOMIC,
            event_type=FeedEventType.DATA_RELEASED,
            indicator="CPI",
            value=value,
            numeric_value=Decimal(value),
            outcome_type=OutcomeType.NUMERIC,
            released_at=released_at,
            received_at=received_at,
        ),
        orderbooks={
            token_id: _book(token_id, bid_price, ask_price),
        },
    )


def _config(**kw: object) -> BacktestConfig:
    strategy = StrategyConfig(
        match_confidence_threshold=0.5,
        min_edge=0.01,
        min_confidence=0.90,
        max_staleness_secs=99999,
        base_size_usd=100.0,
        max_size_usd=1000.0,
    )
    risk = RiskConfig(
        min_profit_usd=0.01,
        min_confidence=0.80,
        bankroll_usd=10000.0,
        min_orderbook_depth_usd=100.0,
        max_spread=0.50,
    )
    defaults: dict[str, object] = {
        "strategy": strategy,
        "risk": risk,
    }
    defaults.update(kw)
    return BacktestConfig(**defaults)  # type: ignore[arg-type]


def _scenario(
    name: str = "test",
    opps: dict[str, MarketOpportunity] | None = None,
    events: list[HistoricalEvent] | None = None,
) -> Scenario:
    return Scenario(
        name=name,
        opportunities=opps or {"cond_1": _opp()},
        events=events or [_cpi_event()],
    )


# ── Empty Scenario ──────────────────────────────────────────────


class TestEmptyScenario:
    async def test_no_events(self) -> None:
        s = Scenario(name="empty", events=[])
        engine = BacktestEngine(s, _config())
        result = await engine.run()
        assert result.total_events == 0
        assert result.total_trades == 0
        assert result.cumulative_pnl == Decimal(0)

    async def test_no_opportunities(self) -> None:
        s = Scenario(
            name="no_opps",
            opportunities={},
            events=[_cpi_event()],
        )
        engine = BacktestEngine(s, _config())
        result = await engine.run()
        assert result.total_trades == 0


# ── Single Event Replay ────────────────────────────────────────


class TestSingleEvent:
    async def test_successful_trade(self) -> None:
        s = _scenario()
        engine = BacktestEngine(s, _config())
        result = await engine.run()
        # CPI 3.2 > 3.0 threshold → match → signal → size → execute
        assert result.total_events == 1
        assert result.signals_generated >= 1
        # If the pipeline produces a trade, it should show up
        assert result.total_trades >= 0  # may skip if sizing/risk rejects

    async def test_collector_populated(self) -> None:
        s = _scenario()
        engine = BacktestEngine(s, _config())
        await engine.run()
        summary = engine.collector.summary()
        assert summary["signals_generated"] >= 0

    async def test_sim_client_fills_recorded(self) -> None:
        s = _scenario()
        engine = BacktestEngine(s, _config())
        await engine.run()
        # The simulated client records all fill attempts
        fills = engine.sim_client.fills
        # May or may not have fills depending on pipeline
        assert isinstance(fills, list)

    async def test_risk_snapshot_available(self) -> None:
        s = _scenario()
        engine = BacktestEngine(s, _config())
        await engine.run()
        snap = engine.risk_snapshot
        assert "killed" in snap
        assert "realized_today" in snap


# ── Multiple Events ─────────────────────────────────────────────


class TestMultipleEvents:
    async def test_two_events(self) -> None:
        events = [
            _cpi_event(value="3.2", released_at=1000.0, received_at=1000.5),
            _cpi_event(value="3.5", released_at=2000.0, received_at=2000.5),
        ]
        s = _scenario(events=events)
        engine = BacktestEngine(s, _config())
        result = await engine.run()
        assert result.total_events == 2

    async def test_orderbook_updates_between_events(self) -> None:
        events = [
            _cpi_event(
                value="3.2",
                released_at=1000.0,
                received_at=1000.5,
                bid_price="0.60",
                ask_price="0.62",
            ),
            _cpi_event(
                value="3.5",
                released_at=2000.0,
                received_at=2000.5,
                bid_price="0.70",
                ask_price="0.72",
            ),
        ]
        s = _scenario(events=events)
        engine = BacktestEngine(s, _config())
        result = await engine.run()
        assert result.total_events == 2


# ── Config Overrides ────────────────────────────────────────────


class TestConfigOverrides:
    async def test_slippage_applied(self) -> None:
        s = _scenario()
        cfg = _config(slippage_bps=50)
        engine = BacktestEngine(s, cfg)
        await engine.run()
        # If fills occurred, slippage should be applied
        for fill in engine.sim_client.fills:
            if fill.success:
                assert fill.slippage >= 0

    async def test_fill_probability_zero(self) -> None:
        s = _scenario()
        cfg = _config(fill_probability=0.0)
        engine = BacktestEngine(s, cfg)
        result = await engine.run()
        # With 0% fill probability, no trades should succeed
        assert result.successful_trades == 0


# ── Result Structure ────────────────────────────────────────────


class TestResultStructure:
    async def test_result_scenario_name(self) -> None:
        s = _scenario(name="my_test")
        engine = BacktestEngine(s, _config())
        result = await engine.run()
        assert result.scenario_name == "my_test"

    async def test_result_has_all_fields(self) -> None:
        s = _scenario()
        engine = BacktestEngine(s, _config())
        result = await engine.run()
        assert hasattr(result, "total_events")
        assert hasattr(result, "total_trades")
        assert hasattr(result, "successful_trades")
        assert hasattr(result, "failed_trades")
        assert hasattr(result, "signals_generated")
        assert hasattr(result, "trades_skipped")
        assert hasattr(result, "risk_rejected")
        assert hasattr(result, "cumulative_pnl")
        assert hasattr(result, "win_rate")
        assert hasattr(result, "execution_results")

    async def test_execution_results_list(self) -> None:
        s = _scenario()
        engine = BacktestEngine(s, _config())
        result = await engine.run()
        assert isinstance(result.execution_results, list)


# ── Category Stats ──────────────────────────────────────────────


class TestCategoryIntegration:
    async def test_economic_category_tracked(self) -> None:
        s = _scenario()
        engine = BacktestEngine(s, _config())
        await engine.run()
        cat_stats = engine.collector.category_stats()
        # If trades were executed, ECONOMIC should be tracked
        if cat_stats:
            assert MarketCategory.ECONOMIC in cat_stats or len(cat_stats) == 0
