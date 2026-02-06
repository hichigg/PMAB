"""Tests for ArbEngine — orchestration, lifecycle, execution."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import RiskConfig, StrategyConfig
from src.core.types import (
    ArbEvent,
    ArbEventType,
    FeedEvent,
    FeedEventType,
    FeedType,
    MarketCategory,
    MarketOpportunity,
    OrderResponse,
    OutcomeType,
)
from src.risk.monitor import RiskMonitor
from src.strategy.engine import ArbEngine

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
        "base_size_usd": 100.0,
        "max_size_usd": 1000.0,
        "max_slippage": 0.02,
        "default_order_type": "FOK",
        "match_confidence_threshold": 0.8,
    }
    defaults.update(overrides)
    return StrategyConfig(**defaults)  # type: ignore[arg-type]


def _risk(**overrides: object) -> RiskConfig:
    defaults: dict[str, object] = {
        "min_profit_usd": 1.0,
        "max_position_usd": 5000.0,
    }
    defaults.update(overrides)
    return RiskConfig(**defaults)  # type: ignore[arg-type]


def _opp(
    condition_id: str = "cond1",
    question: str = "Will CPI be above 3.0%?",
    best_bid: Decimal = Decimal("0.40"),
    best_ask: Decimal = Decimal("0.45"),
    depth_usd: Decimal = Decimal("5000"),
    category: MarketCategory = MarketCategory.ECONOMIC,
) -> MarketOpportunity:
    return MarketOpportunity(
        condition_id=condition_id,
        question=question,
        category=category,
        tokens=_tokens(),
        best_bid=best_bid,
        best_ask=best_ask,
        depth_usd=depth_usd,
    )


def _event(
    feed_type: FeedType = FeedType.ECONOMIC,
    event_type: FeedEventType = FeedEventType.DATA_RELEASED,
    indicator: str = "CPI",
    numeric_value: Decimal | None = Decimal("3.5"),
    outcome_type: OutcomeType = OutcomeType.NUMERIC,
) -> FeedEvent:
    return FeedEvent(
        feed_type=feed_type,
        event_type=event_type,
        indicator=indicator,
        value=str(numeric_value) if numeric_value else "",
        numeric_value=numeric_value,
        outcome_type=outcome_type,
        received_at=time.time(),
    )


def _make_engine(
    opportunities: dict[str, MarketOpportunity] | None = None,
    config: StrategyConfig | None = None,
    risk_config: RiskConfig | None = None,
    order_success: bool = True,
) -> ArbEngine:
    """Create an ArbEngine with mocked client and scanner."""
    client = MagicMock()
    # Mock place_market_order
    response = OrderResponse(
        order_id="order123",
        success=order_success,
        raw={"orderID": "order123"},
    )
    client.place_market_order = AsyncMock(return_value=response)
    client.place_order = AsyncMock(return_value=response)

    scanner = MagicMock()
    if opportunities is None:
        opportunities = {}
    scanner.opportunities = opportunities

    return ArbEngine(
        client=client,
        scanner=scanner,
        config=config or _cfg(),
        risk_config=risk_config or _risk(),
    )


# ── Lifecycle ──────────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start(self) -> None:
        engine = _make_engine()
        await engine.start()
        assert engine.running is True

    @pytest.mark.asyncio
    async def test_stop(self) -> None:
        engine = _make_engine()
        await engine.start()
        await engine.stop()
        assert engine.running is False

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        engine = _make_engine()
        await engine.start()
        await engine.start()  # Should not error
        assert engine.running is True

    @pytest.mark.asyncio
    async def test_emits_start_event(self) -> None:
        engine = _make_engine()
        events: list[ArbEvent] = []
        engine.on_event(lambda e: events.append(e))

        await engine.start()
        assert any(e.event_type == ArbEventType.ENGINE_STARTED for e in events)

    @pytest.mark.asyncio
    async def test_emits_stop_event(self) -> None:
        engine = _make_engine()
        events: list[ArbEvent] = []
        engine.on_event(lambda e: events.append(e))

        await engine.start()
        await engine.stop()
        assert any(e.event_type == ArbEventType.ENGINE_STOPPED for e in events)


# ── on_feed_event ──────────────────────────────────────────────


class TestOnFeedEvent:
    @pytest.mark.asyncio
    async def test_ignores_non_data_released(self) -> None:
        engine = _make_engine()
        await engine.start()

        event = _event(event_type=FeedEventType.FEED_CONNECTED)
        await engine.on_feed_event(event)
        assert engine.stats["signals_generated"] == 0

    @pytest.mark.asyncio
    async def test_ignores_when_stopped(self) -> None:
        engine = _make_engine()
        # Don't start
        event = _event()
        await engine.on_feed_event(event)
        assert engine.stats["signals_generated"] == 0

    @pytest.mark.asyncio
    async def test_processes_data_released(self) -> None:
        """Full pipeline: event → match → signal → trade."""
        opps = {"cond1": _opp()}
        engine = _make_engine(opportunities=opps)
        await engine.start()

        event = _event()
        await engine.on_feed_event(event)
        # Should have generated a signal and attempted execution
        stats = engine.stats
        assert stats["signals_generated"] >= 1

    @pytest.mark.asyncio
    async def test_no_match_no_signal(self) -> None:
        """No matching opportunities → no signals."""
        engine = _make_engine(opportunities={})
        await engine.start()

        event = _event()
        await engine.on_feed_event(event)
        assert engine.stats["signals_generated"] == 0

    @pytest.mark.asyncio
    async def test_skipped_when_no_edge(self) -> None:
        """Market already priced → signal skipped."""
        opps = {
            "cond1": _opp(
                best_bid=Decimal("0.98"),
                best_ask=Decimal("0.99"),
            ),
        }
        engine = _make_engine(opportunities=opps)
        await engine.start()

        event = _event()
        await engine.on_feed_event(event)
        assert engine.stats["trades_skipped"] >= 1

    @pytest.mark.asyncio
    async def test_stats_accumulate(self) -> None:
        """Stats increment across multiple events."""
        opps = {"cond1": _opp()}
        engine = _make_engine(opportunities=opps)
        await engine.start()

        await engine.on_feed_event(_event())
        await engine.on_feed_event(_event())
        stats = engine.stats
        assert stats["signals_generated"] >= 2


# ── Execution ──────────────────────────────────────────────────


class TestExecution:
    @pytest.mark.asyncio
    async def test_fok_execution(self) -> None:
        """FOK orders use place_market_order."""
        opps = {"cond1": _opp()}
        engine = _make_engine(opportunities=opps)
        await engine.start()

        results = await engine.process_event(_event())
        assert len(results) >= 1
        assert results[0].success is True
        engine._client.place_market_order.assert_called()

    @pytest.mark.asyncio
    async def test_gtc_execution(self) -> None:
        """GTC orders use place_order."""
        opps = {"cond1": _opp()}
        engine = _make_engine(
            opportunities=opps,
            config=_cfg(default_order_type="GTC"),
        )
        await engine.start()

        results = await engine.process_event(_event())
        assert len(results) >= 1
        engine._client.place_order.assert_called()

    @pytest.mark.asyncio
    async def test_failed_order(self) -> None:
        """Failed order increments trades_failed."""
        opps = {"cond1": _opp()}
        engine = _make_engine(opportunities=opps, order_success=False)
        await engine.start()

        results = await engine.process_event(_event())
        assert len(results) >= 1
        assert results[0].success is False
        assert engine.stats["trades_failed"] >= 1

    @pytest.mark.asyncio
    async def test_execution_error(self) -> None:
        """Exception during execution → trades_failed, error recorded."""
        opps = {"cond1": _opp()}
        engine = _make_engine(opportunities=opps)
        engine._client.place_market_order = AsyncMock(
            side_effect=RuntimeError("network error"),
        )
        await engine.start()

        results = await engine.process_event(_event())
        assert len(results) >= 1
        assert results[0].success is False
        assert "network error" in results[0].error

    @pytest.mark.asyncio
    async def test_emits_trade_executed(self) -> None:
        opps = {"cond1": _opp()}
        engine = _make_engine(opportunities=opps)
        events: list[ArbEvent] = []
        engine.on_event(lambda e: events.append(e))
        await engine.start()

        await engine.process_event(_event())
        assert any(e.event_type == ArbEventType.TRADE_EXECUTED for e in events)


# ── process_event ──────────────────────────────────────────────


class TestProcessEvent:
    @pytest.mark.asyncio
    async def test_returns_results(self) -> None:
        opps = {"cond1": _opp()}
        engine = _make_engine(opportunities=opps)

        results = await engine.process_event(_event())
        assert isinstance(results, list)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_empty_when_no_opps(self) -> None:
        engine = _make_engine(opportunities={})
        results = await engine.process_event(_event())
        assert results == []

    @pytest.mark.asyncio
    async def test_multiple_matches(self) -> None:
        """Multiple matching markets produce multiple results."""
        opps = {
            "c1": _opp(condition_id="c1", question="Will CPI be above 3.0%?"),
            "c2": _opp(condition_id="c2", question="Will CPI exceed 2.0%?"),
        }
        engine = _make_engine(opportunities=opps)

        results = await engine.process_event(_event())
        assert len(results) >= 2

    @pytest.mark.asyncio
    async def test_e2e_economic(self) -> None:
        """End-to-end: CPI release → match → signal → trade."""
        opps = {"c1": _opp(best_ask=Decimal("0.50"))}
        engine = _make_engine(opportunities=opps)

        event = _event(
            indicator="CPI",
            numeric_value=Decimal("3.5"),
        )
        results = await engine.process_event(event)
        assert len(results) == 1
        assert results[0].success is True

    @pytest.mark.asyncio
    async def test_e2e_sports(self) -> None:
        """End-to-end: game result → match → signal → trade."""
        opps = {
            "c1": _opp(
                question="Will the Lakers win tonight?",
                category=MarketCategory.SPORTS,
                best_ask=Decimal("0.50"),
            ),
        }
        engine = _make_engine(opportunities=opps)

        event = FeedEvent(
            feed_type=FeedType.SPORTS,
            event_type=FeedEventType.DATA_RELEASED,
            indicator="NBA",
            value="Lakers",
            outcome_type=OutcomeType.CATEGORICAL,
            received_at=time.time(),
            metadata={
                "winner": "Lakers",
                "home_team": "Lakers",
                "away_team": "Celtics",
            },
        )
        results = await engine.process_event(event)
        assert len(results) == 1
        assert results[0].success is True

    @pytest.mark.asyncio
    async def test_e2e_crypto(self) -> None:
        """End-to-end: BTC price move → match → signal → trade."""
        opps = {
            "c1": _opp(
                question="Will BTC go above $50,000?",
                category=MarketCategory.CRYPTO,
                best_ask=Decimal("0.50"),
            ),
        }
        engine = _make_engine(
            opportunities=opps,
            config=_cfg(min_confidence=0.50),
        )

        event = FeedEvent(
            feed_type=FeedType.CRYPTO,
            event_type=FeedEventType.DATA_RELEASED,
            indicator="BTC_USDT",
            value="55000",
            numeric_value=Decimal("55000"),
            outcome_type=OutcomeType.NUMERIC,
            received_at=time.time(),
        )
        results = await engine.process_event(event)
        assert len(results) == 1
        assert results[0].success is True


# ── Callbacks ──────────────────────────────────────────────────


class TestCallbacks:
    @pytest.mark.asyncio
    async def test_receives_events(self) -> None:
        opps = {"c1": _opp()}
        engine = _make_engine(opportunities=opps)
        events: list[ArbEvent] = []
        engine.on_event(lambda e: events.append(e))

        await engine.process_event(_event())
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_async_callback(self) -> None:
        opps = {"c1": _opp()}
        engine = _make_engine(opportunities=opps)
        events: list[ArbEvent] = []

        async def async_cb(e: ArbEvent) -> None:
            events.append(e)

        engine.on_event(async_cb)
        await engine.process_event(_event())
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_callback_exception_safety(self) -> None:
        """Callback exceptions don't crash the engine."""
        opps = {"c1": _opp()}
        engine = _make_engine(opportunities=opps)

        def bad_cb(e: ArbEvent) -> None:
            raise RuntimeError("callback boom")

        engine.on_event(bad_cb)

        # Should not raise
        results = await engine.process_event(_event())
        assert isinstance(results, list)


# ── Risk Integration ──────────────────────────────────────────


def _make_engine_with_risk(
    opportunities: dict[str, MarketOpportunity] | None = None,
    config: StrategyConfig | None = None,
    risk_config: RiskConfig | None = None,
    order_success: bool = True,
    risk_monitor: RiskMonitor | None = None,
) -> ArbEngine:
    """Create an ArbEngine with mocked client/scanner and a RiskMonitor."""
    client = MagicMock()
    response = OrderResponse(
        order_id="order123",
        success=order_success,
        raw={"orderID": "order123"},
    )
    client.place_market_order = AsyncMock(return_value=response)
    client.place_order = AsyncMock(return_value=response)

    scanner = MagicMock()
    if opportunities is None:
        opportunities = {}
    scanner.opportunities = opportunities

    rc = risk_config or _risk()
    if risk_monitor is None:
        risk_monitor = RiskMonitor(config=rc)

    return ArbEngine(
        client=client,
        scanner=scanner,
        config=config or _cfg(),
        risk_config=rc,
        risk_monitor=risk_monitor,
    )


class TestRiskIntegration:
    @pytest.mark.asyncio
    async def test_no_monitor_still_works(self) -> None:
        """Engine works without a risk monitor (backward compat)."""
        opps = {"cond1": _opp()}
        engine = _make_engine(opportunities=opps)
        results = await engine.process_event(_event())
        assert len(results) >= 1
        assert results[0].success is True

    @pytest.mark.asyncio
    async def test_rejection_skips_trade(self) -> None:
        """Kill switch active → trade skipped."""
        opps = {"cond1": _opp()}
        monitor = RiskMonitor(config=_risk())
        monitor._killed = True
        engine = _make_engine_with_risk(opportunities=opps, risk_monitor=monitor)

        results = await engine.process_event(_event())
        assert results == []
        assert engine.stats["trades_skipped"] >= 1

    @pytest.mark.asyncio
    async def test_approval_allows_trade(self) -> None:
        """All gates pass → trade executes normally."""
        opps = {"cond1": _opp()}
        engine = _make_engine_with_risk(opportunities=opps)

        results = await engine.process_event(_event())
        assert len(results) >= 1
        assert results[0].success is True

    @pytest.mark.asyncio
    async def test_record_fill_called_on_success(self) -> None:
        """record_fill is called after successful execution."""
        opps = {"cond1": _opp()}
        monitor = RiskMonitor(config=_risk())
        engine = _make_engine_with_risk(opportunities=opps, risk_monitor=monitor)

        await engine.process_event(_event())
        assert monitor.positions.count >= 1

    @pytest.mark.asyncio
    async def test_record_fill_not_called_on_failure(self) -> None:
        """record_fill is NOT called when order fails."""
        opps = {"cond1": _opp()}
        monitor = RiskMonitor(config=_risk())
        engine = _make_engine_with_risk(
            opportunities=opps, risk_monitor=monitor, order_success=False,
        )

        await engine.process_event(_event())
        assert monitor.positions.count == 0

    @pytest.mark.asyncio
    async def test_emits_risk_rejected(self) -> None:
        """RISK_REJECTED event is emitted when risk gate rejects."""
        opps = {"cond1": _opp()}
        monitor = RiskMonitor(config=_risk())
        monitor._killed = True
        engine = _make_engine_with_risk(opportunities=opps, risk_monitor=monitor)
        events: list[ArbEvent] = []
        engine.on_event(lambda e: events.append(e))

        await engine.process_event(_event())
        assert any(e.event_type == ArbEventType.RISK_REJECTED for e in events)

    @pytest.mark.asyncio
    async def test_increments_skipped(self) -> None:
        """trades_skipped increments on risk rejection."""
        opps = {"cond1": _opp()}
        monitor = RiskMonitor(config=_risk())
        monitor._killed = True
        engine = _make_engine_with_risk(opportunities=opps, risk_monitor=monitor)

        initial = engine.stats["trades_skipped"]
        await engine.process_event(_event())
        assert engine.stats["trades_skipped"] > initial

    def test_risk_snapshot_property(self) -> None:
        """risk_snapshot delegates to monitor.snapshot()."""
        monitor = RiskMonitor(config=_risk())
        engine = _make_engine_with_risk(risk_monitor=monitor)
        snap = engine.risk_snapshot
        assert snap is not None
        assert "killed" in snap

    def test_risk_snapshot_none_without_monitor(self) -> None:
        """risk_snapshot is None when no monitor configured."""
        engine = _make_engine()
        assert engine.risk_snapshot is None
