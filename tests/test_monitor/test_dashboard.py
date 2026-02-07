"""Tests for dashboard rendering — output formatting, empty state, edge cases."""

from __future__ import annotations

import re
from decimal import Decimal

from src.core.types import MarketCategory
from src.monitor.metrics import CategoryStats, LatencySample, PnLPoint

from scripts.dashboard import (
    _strip_ansi,
    render_category_stats,
    render_dashboard,
    render_from_collector,
    render_header,
    render_latency,
    render_latency_histogram,
    render_liquidity,
    render_pnl_curve,
    render_risk_snapshot,
    render_summary,
)
from src.monitor.metrics import MetricsCollector


# ── Helpers ─────────────────────────────────────────────────────


def _plain(text: str) -> str:
    """Strip ANSI codes for assertion matching."""
    return _strip_ansi(text)


def _summary(**kw: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "total_trades": 10,
        "successful_trades": 8,
        "failed_trades": 2,
        "win_rate": 0.8,
        "cumulative_pnl": Decimal("150.50"),
        "avg_profit_per_trade": Decimal("15.05"),
        "signals_generated": 20,
        "trades_executed": 8,
        "trades_failed": 2,
        "trades_skipped": 5,
        "risk_rejected": 5,
        "latency": {"p50": 0.5, "p90": 1.2, "p99": 2.1, "min": 0.1, "max": 3.0},
        "liquidity": {
            "total_captured_usd": Decimal("900"),
            "total_available_usd": Decimal("50000"),
            "capture_ratio": 0.018,
        },
    }
    defaults.update(kw)
    return defaults


def _cat_stats() -> dict[MarketCategory, CategoryStats]:
    return {
        MarketCategory.ECONOMIC: CategoryStats(
            category=MarketCategory.ECONOMIC,
            total_trades=5,
            wins=4,
            losses=1,
            total_profit=Decimal("100"),
            total_volume=Decimal("450"),
        ),
        MarketCategory.SPORTS: CategoryStats(
            category=MarketCategory.SPORTS,
            total_trades=3,
            wins=2,
            losses=1,
            total_profit=Decimal("30"),
            total_volume=Decimal("270"),
        ),
    }


# ── Header ──────────────────────────────────────────────────────


class TestHeader:
    def test_contains_title(self) -> None:
        h = _plain(render_header())
        assert "POLYMARKET ARB BOT" in h

    def test_has_border(self) -> None:
        h = render_header()
        # Should contain box-drawing characters
        assert "\u2550" in h or "=" in h or "\u2554" in h

    def test_shows_utc_time(self) -> None:
        h = _plain(render_header())
        assert "UTC" in h


# ── Summary Rendering ───────────────────────────────────────────


class TestRenderSummary:
    def test_contains_trade_count(self) -> None:
        output = _plain(render_summary(_summary()))
        assert "10" in output

    def test_contains_win_rate(self) -> None:
        output = _plain(render_summary(_summary()))
        assert "80.0%" in output

    def test_contains_pnl(self) -> None:
        output = _plain(render_summary(_summary()))
        assert "$150.50" in output

    def test_contains_signals(self) -> None:
        output = _plain(render_summary(_summary()))
        assert "20" in output

    def test_zero_trades(self) -> None:
        output = _plain(render_summary(_summary(total_trades=0, win_rate=0.0)))
        assert "0" in output


# ── Category Stats Rendering ───────────────────────────────────


class TestRenderCategoryStats:
    def test_contains_categories(self) -> None:
        output = _plain(render_category_stats(_cat_stats()))
        assert "ECONOMIC" in output
        assert "SPORTS" in output

    def test_contains_win_rates(self) -> None:
        output = _plain(render_category_stats(_cat_stats()))
        assert "80.0%" in output  # ECONOMIC: 4/5

    def test_empty_stats(self) -> None:
        output = _plain(render_category_stats({}))
        assert "Waiting" in output or "no trades" in output

    def test_pnl_shown(self) -> None:
        output = _plain(render_category_stats(_cat_stats()))
        assert "$100.00" in output


# ── Latency Rendering ──────────────────────────────────────────


class TestRenderLatency:
    def test_contains_percentiles(self) -> None:
        pct = {"p50": 0.5, "p90": 1.2, "p99": 2.1, "min": 0.1, "max": 3.0}
        output = _plain(render_latency(pct))
        assert "P50" in output
        assert "P90" in output
        assert "P99" in output

    def test_sub_second_uses_ms(self) -> None:
        pct = {"p50": 0.050, "p90": 0.100, "p99": 0.200, "min": 0.010, "max": 0.500}
        output = _plain(render_latency(pct))
        assert "ms" in output

    def test_over_second_uses_s(self) -> None:
        pct = {"p50": 1.5, "p90": 2.0, "p99": 3.0, "min": 0.5, "max": 5.0}
        output = _plain(render_latency(pct))
        assert "1.50s" in output


# ── Latency Histogram Rendering ────────────────────────────────


class TestRenderLatencyHistogram:
    def test_empty_histogram(self) -> None:
        output = render_latency_histogram([])
        assert output == ""

    def test_histogram_has_bars(self) -> None:
        histogram = [
            (0.0, 0.5, 3),
            (0.5, 1.0, 7),
            (1.0, 1.5, 2),
        ]
        output = render_latency_histogram(histogram)
        # Should contain block characters
        assert "\u2588" in output or "#" in output

    def test_histogram_counts_shown(self) -> None:
        histogram = [(0.0, 1.0, 5), (1.0, 2.0, 10)]
        output = _plain(render_latency_histogram(histogram))
        assert "5" in output
        assert "10" in output


# ── P&L Curve Rendering ────────────────────────────────────────


class TestRenderPnLCurve:
    def test_empty_curve(self) -> None:
        output = _plain(render_pnl_curve([]))
        assert "Waiting" in output or "no data" in output

    def test_curve_with_data(self) -> None:
        curve = [
            PnLPoint(timestamp=100.0, cumulative_pnl=Decimal("10"), trade_index=1),
            PnLPoint(timestamp=200.0, cumulative_pnl=Decimal("30"), trade_index=2),
            PnLPoint(timestamp=300.0, cumulative_pnl=Decimal("25"), trade_index=3),
        ]
        output = _plain(render_pnl_curve(curve))
        assert "P&L" in output

    def test_flat_curve(self) -> None:
        curve = [
            PnLPoint(timestamp=100.0, cumulative_pnl=Decimal("50"), trade_index=1),
            PnLPoint(timestamp=200.0, cumulative_pnl=Decimal("50"), trade_index=2),
        ]
        output = _plain(render_pnl_curve(curve))
        assert "Flat" in output


# ── Liquidity Rendering ────────────────────────────────────────


class TestRenderLiquidity:
    def test_contains_values(self) -> None:
        liq: dict[str, object] = {
            "total_captured_usd": Decimal("900"),
            "total_available_usd": Decimal("50000"),
            "capture_ratio": 0.018,
        }
        output = _plain(render_liquidity(liq))
        assert "$900.00" in output
        assert "$50,000.00" in output
        assert "1.8%" in output


# ── Risk Snapshot Rendering ─────────────────────────────────────


class TestRenderRiskSnapshot:
    def test_none_snapshot(self) -> None:
        assert render_risk_snapshot(None) == ""

    def test_active_status(self) -> None:
        snap: dict[str, object] = {
            "killed": False,
            "open_positions": 3,
            "total_exposure_usd": 1500.0,
            "realized_today": 45.0,
            "realized_total": 200.0,
            "trade_count_today": 7,
        }
        output = _plain(render_risk_snapshot(snap))
        assert "ACTIVE" in output
        assert "$1,500.00" in output

    def test_killed_status(self) -> None:
        snap: dict[str, object] = {
            "killed": True,
            "kill_switch_trigger": "DAILY_LOSS",
            "kill_switch_reason": "exceeded limit",
            "open_positions": 0,
            "total_exposure_usd": 0,
            "realized_today": -500.0,
            "realized_total": -300.0,
            "trade_count_today": 12,
        }
        output = _plain(render_risk_snapshot(snap))
        assert "KILLED" in output
        assert "DAILY_LOSS" in output

    def test_with_oracle_data(self) -> None:
        snap: dict[str, object] = {
            "killed": False,
            "open_positions": 1,
            "total_exposure_usd": 100.0,
            "realized_today": 10.0,
            "realized_total": 10.0,
            "trade_count_today": 1,
            "disputed_markets": 2,
            "exposure_at_risk_usd": 500.0,
        }
        output = _plain(render_risk_snapshot(snap))
        assert "Disputed" in output
        assert "$500.00" in output


# ── Full Dashboard Rendering ───────────────────────────────────


class TestRenderDashboard:
    def test_full_dashboard_string(self) -> None:
        output = _plain(render_dashboard(
            summary=_summary(),
            cat_stats=_cat_stats(),
            latency_pct={"p50": 0.5, "p90": 1.2, "p99": 2.1, "min": 0.1, "max": 3.0},
            pnl=[
                PnLPoint(100.0, Decimal("10"), 1),
                PnLPoint(200.0, Decimal("30"), 2),
            ],
            histogram=[(0.0, 1.0, 5), (1.0, 2.0, 3)],
            liquidity={
                "total_captured_usd": Decimal("900"),
                "total_available_usd": Decimal("50000"),
                "capture_ratio": 0.018,
            },
        ))
        assert "POLYMARKET ARB BOT" in output
        assert "OVERVIEW" in output
        assert "CATEGORIES" in output
        assert "LATENCY" in output
        assert "P&L" in output
        assert "LIQUIDITY" in output

    def test_full_dashboard_with_risk(self) -> None:
        snap: dict[str, object] = {
            "killed": False,
            "open_positions": 2,
            "total_exposure_usd": 1000.0,
            "realized_today": 50.0,
            "realized_total": 200.0,
            "trade_count_today": 5,
        }
        output = _plain(render_dashboard(
            summary=_summary(),
            cat_stats=_cat_stats(),
            latency_pct={"p50": 0.5, "p90": 1.2, "p99": 2.1, "min": 0.1, "max": 3.0},
            pnl=[],
            histogram=[],
            liquidity={
                "total_captured_usd": Decimal("0"),
                "total_available_usd": Decimal("0"),
                "capture_ratio": 0.0,
            },
            risk_snap=snap,
        ))
        assert "RISK STATUS" in output


# ── render_from_collector ───────────────────────────────────────


class TestRenderFromCollector:
    def test_empty_collector(self) -> None:
        c = MetricsCollector()
        output = _plain(render_from_collector(c))
        assert "POLYMARKET ARB BOT" in output
        assert "0" in output  # zero trades

    def test_with_risk_snap(self) -> None:
        c = MetricsCollector()
        snap: dict[str, object] = {
            "killed": False,
            "open_positions": 0,
            "total_exposure_usd": 0,
            "realized_today": 0,
            "realized_total": 0,
            "trade_count_today": 0,
        }
        output = _plain(render_from_collector(c, risk_snap=snap))
        assert "RISK STATUS" in output
