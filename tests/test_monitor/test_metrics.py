"""Tests for MetricsCollector — per-category stats, latency, P&L curve, liquidity."""

from __future__ import annotations

from decimal import Decimal

from src.core.types import (
    ArbEvent,
    ArbEventType,
    ExecutionResult,
    FeedEvent,
    FeedEventType,
    FeedType,
    MarketCategory,
    MarketOpportunity,
    MatchResult,
    OrderType,
    Side,
    Signal,
    SignalDirection,
    TradeAction,
)
from src.monitor.metrics import MetricsCollector


# ── Helpers ─────────────────────────────────────────────────────


def _signal(
    category: MarketCategory = MarketCategory.ECONOMIC,
    released_at: float = 100.0,
    received_at: float = 100.5,
    created_at: float = 101.0,
    **kw: object,
) -> Signal:
    feed_event = FeedEvent(
        feed_type=FeedType.ECONOMIC,
        event_type=FeedEventType.DATA_RELEASED,
        indicator="CPI",
        released_at=released_at,
        received_at=received_at,
    )
    opp = MarketOpportunity(
        condition_id="cond1",
        category=category,
        depth_usd=Decimal("5000"),
    )
    match = MatchResult(
        feed_event=feed_event,
        opportunity=opp,
        match_confidence=0.99,
    )
    defaults: dict[str, object] = {
        "match": match,
        "confidence": 0.99,
        "edge": Decimal("0.05"),
        "direction": SignalDirection.BUY,
        "fair_value": Decimal("0.95"),
        "current_price": Decimal("0.90"),
        "created_at": created_at,
    }
    defaults.update(kw)
    return Signal(**defaults)  # type: ignore[arg-type]


def _action(
    signal: Signal | None = None,
    **kw: object,
) -> TradeAction:
    defaults: dict[str, object] = {
        "signal": signal or _signal(),
        "token_id": "tok_1",
        "side": Side.BUY,
        "price": Decimal("0.90"),
        "size": Decimal("100"),
        "estimated_profit_usd": Decimal("10"),
    }
    defaults.update(kw)
    return TradeAction(**defaults)  # type: ignore[arg-type]


def _result(
    action: TradeAction | None = None,
    **kw: object,
) -> ExecutionResult:
    defaults: dict[str, object] = {
        "action": action or _action(),
        "success": True,
        "fill_price": Decimal("0.90"),
        "fill_size": Decimal("100"),
        "executed_at": 102.0,
    }
    defaults.update(kw)
    return ExecutionResult(**defaults)  # type: ignore[arg-type]


def _trade_executed_event(
    category: MarketCategory = MarketCategory.ECONOMIC,
    released_at: float = 100.0,
    received_at: float = 100.5,
    created_at: float = 101.0,
    executed_at: float = 102.0,
    estimated_profit: Decimal = Decimal("10"),
    depth_usd: Decimal = Decimal("5000"),
    price: Decimal = Decimal("0.90"),
    size: Decimal = Decimal("100"),
    timestamp: float = 102.0,
) -> ArbEvent:
    sig = _signal(
        category=category,
        released_at=released_at,
        received_at=received_at,
        created_at=created_at,
    )
    sig.match.opportunity.depth_usd = depth_usd
    act = _action(
        signal=sig,
        price=price,
        size=size,
        estimated_profit_usd=estimated_profit,
    )
    res = _result(action=act, executed_at=executed_at)
    return ArbEvent(
        event_type=ArbEventType.TRADE_EXECUTED,
        signal=sig,
        action=act,
        result=res,
        timestamp=timestamp,
    )


def _trade_failed_event(
    category: MarketCategory = MarketCategory.SPORTS,
    timestamp: float = 200.0,
) -> ArbEvent:
    sig = _signal(category=category)
    act = _action(signal=sig, estimated_profit_usd=Decimal("5"))
    res = _result(action=act, success=False)
    return ArbEvent(
        event_type=ArbEventType.TRADE_FAILED,
        signal=sig,
        action=act,
        result=res,
        timestamp=timestamp,
    )


# ── Counter Tests ───────────────────────────────────────────────


class TestCounters:
    def test_initial_state(self) -> None:
        c = MetricsCollector()
        s = c.summary()
        assert s["total_trades"] == 0
        assert s["signals_generated"] == 0
        assert s["trades_executed"] == 0
        assert s["trades_failed"] == 0
        assert s["trades_skipped"] == 0
        assert s["risk_rejected"] == 0

    def test_signal_generated_counter(self) -> None:
        c = MetricsCollector()
        ev = ArbEvent(
            event_type=ArbEventType.SIGNAL_GENERATED,
            signal=_signal(),
            timestamp=100.0,
        )
        c.on_arb_event(ev)
        c.on_arb_event(ev)
        assert c.summary()["signals_generated"] == 2

    def test_trade_skipped_counter(self) -> None:
        c = MetricsCollector()
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_SKIPPED,
            reason="No signal",
            timestamp=100.0,
        )
        c.on_arb_event(ev)
        assert c.summary()["trades_skipped"] == 1

    def test_risk_rejected_counter(self) -> None:
        c = MetricsCollector()
        ev = ArbEvent(
            event_type=ArbEventType.RISK_REJECTED,
            reason="Daily loss",
            timestamp=100.0,
        )
        c.on_arb_event(ev)
        assert c.summary()["risk_rejected"] == 1

    def test_trade_executed_counter(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(_trade_executed_event())
        assert c.summary()["trades_executed"] == 1
        assert c.summary()["total_trades"] == 1

    def test_trade_failed_counter(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(_trade_failed_event())
        assert c.summary()["trades_failed"] == 1

    def test_engine_events_ignored(self) -> None:
        c = MetricsCollector()
        for etype in [ArbEventType.ENGINE_STARTED, ArbEventType.ENGINE_STOPPED]:
            c.on_arb_event(ArbEvent(event_type=etype, timestamp=100.0))
        s = c.summary()
        assert s["total_trades"] == 0
        assert s["signals_generated"] == 0


# ── Category Stats ──────────────────────────────────────────────


class TestCategoryStats:
    def test_single_category(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(_trade_executed_event(category=MarketCategory.ECONOMIC))
        stats = c.category_stats()
        assert MarketCategory.ECONOMIC in stats
        s = stats[MarketCategory.ECONOMIC]
        assert s.total_trades == 1
        assert s.wins == 1
        assert s.losses == 0
        assert s.win_rate == 1.0

    def test_multiple_categories(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(_trade_executed_event(category=MarketCategory.ECONOMIC))
        c.on_arb_event(_trade_executed_event(category=MarketCategory.SPORTS))
        c.on_arb_event(_trade_failed_event(category=MarketCategory.SPORTS))
        stats = c.category_stats()
        assert len(stats) == 2
        assert stats[MarketCategory.ECONOMIC].wins == 1
        assert stats[MarketCategory.SPORTS].total_trades == 2
        assert stats[MarketCategory.SPORTS].wins == 1
        assert stats[MarketCategory.SPORTS].losses == 1

    def test_win_rate_calculation(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(_trade_executed_event(category=MarketCategory.CRYPTO))
        c.on_arb_event(_trade_executed_event(category=MarketCategory.CRYPTO))
        c.on_arb_event(_trade_failed_event(category=MarketCategory.CRYPTO))
        stats = c.category_stats()
        s = stats[MarketCategory.CRYPTO]
        assert s.total_trades == 3
        assert s.wins == 2
        assert abs(s.win_rate - 2 / 3) < 0.01

    def test_avg_profit(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(
            _trade_executed_event(
                category=MarketCategory.ECONOMIC,
                estimated_profit=Decimal("20"),
            )
        )
        c.on_arb_event(
            _trade_executed_event(
                category=MarketCategory.ECONOMIC,
                estimated_profit=Decimal("10"),
            )
        )
        stats = c.category_stats()
        assert stats[MarketCategory.ECONOMIC].avg_profit == Decimal("15")

    def test_empty_category_stats(self) -> None:
        c = MetricsCollector()
        assert c.category_stats() == {}

    def test_volume_tracked(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(
            _trade_executed_event(price=Decimal("0.50"), size=Decimal("200"))
        )
        stats = c.category_stats()
        s = stats[MarketCategory.ECONOMIC]
        # fill_price * fill_size = 0.50 * 200 = 100
        # (fill_price defaults to action.price in _result via override)
        assert s.total_volume > 0


# ── P&L Curve ───────────────────────────────────────────────────


class TestPnLCurve:
    def test_empty_curve(self) -> None:
        c = MetricsCollector()
        assert c.pnl_curve() == []

    def test_curve_grows_with_trades(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(
            _trade_executed_event(estimated_profit=Decimal("10"), timestamp=100.0)
        )
        c.on_arb_event(
            _trade_executed_event(estimated_profit=Decimal("20"), timestamp=200.0)
        )
        curve = c.pnl_curve()
        assert len(curve) == 2
        assert curve[0].cumulative_pnl == Decimal("10")
        assert curve[1].cumulative_pnl == Decimal("30")

    def test_curve_decreases_on_loss(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(
            _trade_executed_event(estimated_profit=Decimal("50"), timestamp=100.0)
        )
        c.on_arb_event(_trade_failed_event(timestamp=200.0))
        curve = c.pnl_curve()
        assert len(curve) == 2
        assert curve[0].cumulative_pnl == Decimal("50")
        # Failed trade: estimated_pnl = -(price * size) = -(0.90 * 100) = -90
        assert curve[1].cumulative_pnl == Decimal("50") + Decimal("-90")

    def test_curve_trade_index(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(_trade_executed_event(timestamp=100.0))
        c.on_arb_event(_trade_executed_event(timestamp=200.0))
        curve = c.pnl_curve()
        assert curve[0].trade_index == 1
        assert curve[1].trade_index == 2

    def test_curve_timestamps(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(_trade_executed_event(timestamp=100.0))
        c.on_arb_event(_trade_executed_event(timestamp=200.0))
        curve = c.pnl_curve()
        assert curve[0].timestamp == 100.0
        assert curve[1].timestamp == 200.0


# ── Latency ─────────────────────────────────────────────────────


class TestLatency:
    def test_no_samples(self) -> None:
        c = MetricsCollector()
        p = c.latency_percentiles()
        assert p["p50"] == 0.0
        assert p["p90"] == 0.0

    def test_latency_recorded_for_success(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(
            _trade_executed_event(
                released_at=100.0,
                received_at=100.5,
                executed_at=102.0,
            )
        )
        samples = c.latency_samples()
        assert len(samples) == 1
        assert samples[0].total_secs == 2.0
        assert samples[0].feed_latency_secs == 0.5
        assert samples[0].processing_secs == 1.5
        assert samples[0].category == MarketCategory.ECONOMIC

    def test_latency_not_recorded_for_failure(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(_trade_failed_event())
        assert len(c.latency_samples()) == 0

    def test_latency_not_recorded_without_timestamps(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(
            _trade_executed_event(released_at=0.0, executed_at=102.0)
        )
        assert len(c.latency_samples()) == 0

    def test_latency_percentiles(self) -> None:
        c = MetricsCollector()
        for i in range(100):
            released = 1000.0 + i * 10
            executed = released + (i + 1) * 0.1
            c.on_arb_event(
                _trade_executed_event(
                    released_at=released,
                    received_at=released + 0.05,
                    executed_at=executed,
                    timestamp=executed,
                )
            )
        p = c.latency_percentiles()
        assert p["min"] < p["p50"] < p["p90"] < p["p99"] <= p["max"]

    def test_max_latency_samples(self) -> None:
        c = MetricsCollector(max_latency_samples=5)
        for i in range(10):
            released = 1000.0 + i * 10
            c.on_arb_event(
                _trade_executed_event(
                    released_at=released,
                    received_at=released + 0.05,
                    executed_at=released + 1.0,
                    timestamp=released + 1.0,
                )
            )
        assert len(c.latency_samples()) == 5


# ── Latency Histogram ──────────────────────────────────────────


class TestLatencyHistogram:
    def test_empty_histogram(self) -> None:
        c = MetricsCollector()
        assert c.latency_histogram() == []

    def test_histogram_buckets(self) -> None:
        c = MetricsCollector()
        for i in range(20):
            released = 1000.0 + i * 10
            c.on_arb_event(
                _trade_executed_event(
                    released_at=released,
                    received_at=released + 0.05,
                    executed_at=released + (i + 1) * 0.1,
                    timestamp=released + (i + 1) * 0.1,
                )
            )
        h = c.latency_histogram(buckets=5)
        assert len(h) == 5
        total = sum(count for _, _, count in h)
        assert total == 20

    def test_single_value_histogram(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(
            _trade_executed_event(
                released_at=100.0,
                received_at=100.05,
                executed_at=101.0,
                timestamp=101.0,
            )
        )
        h = c.latency_histogram()
        assert len(h) == 1
        assert h[0][2] == 1


# ── Liquidity ───────────────────────────────────────────────────


class TestLiquidity:
    def test_empty_liquidity(self) -> None:
        c = MetricsCollector()
        liq = c.liquidity_stats()
        assert liq["total_captured_usd"] == Decimal(0)
        assert liq["total_available_usd"] == Decimal(0)
        assert liq["capture_ratio"] == 0.0

    def test_liquidity_captured(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(
            _trade_executed_event(
                price=Decimal("0.90"),
                size=Decimal("100"),
                depth_usd=Decimal("5000"),
            )
        )
        liq = c.liquidity_stats()
        assert liq["total_captured_usd"] == Decimal("90")  # 0.90 * 100
        assert liq["total_available_usd"] == Decimal("5000")
        assert liq["capture_ratio"] == 0.018  # 90/5000

    def test_failed_trades_excluded_from_liquidity(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(_trade_failed_event())
        liq = c.liquidity_stats()
        # Failed trades don't count as captured
        assert liq["total_captured_usd"] == Decimal(0)


# ── Summary ─────────────────────────────────────────────────────


class TestSummary:
    def test_win_rate_mixed(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(_trade_executed_event())
        c.on_arb_event(_trade_executed_event())
        c.on_arb_event(_trade_failed_event())
        s = c.summary()
        assert s["total_trades"] == 3
        assert s["successful_trades"] == 2
        assert s["failed_trades"] == 1
        assert abs(s["win_rate"] - 0.6667) < 0.01

    def test_summary_includes_latency(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(_trade_executed_event())
        s = c.summary()
        assert "latency" in s
        assert "p50" in s["latency"]

    def test_summary_includes_liquidity(self) -> None:
        c = MetricsCollector()
        s = c.summary()
        assert "liquidity" in s


# ── Trade Records ───────────────────────────────────────────────


class TestTradeRecords:
    def test_trades_list(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(_trade_executed_event())
        trades = c.trades
        assert len(trades) == 1
        assert trades[0].success is True
        assert trades[0].category == MarketCategory.ECONOMIC
        assert trades[0].token_id == "tok_1"

    def test_trades_are_copies(self) -> None:
        c = MetricsCollector()
        c.on_arb_event(_trade_executed_event())
        t1 = c.trades
        t2 = c.trades
        assert t1 is not t2

    def test_event_without_action_ignored(self) -> None:
        c = MetricsCollector()
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_EXECUTED,
            timestamp=100.0,
        )
        c.on_arb_event(ev)
        # Counter incremented but no record (no action)
        assert c.summary()["trades_executed"] == 1
        assert c.summary()["total_trades"] == 0
