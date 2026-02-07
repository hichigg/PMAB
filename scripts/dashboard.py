#!/usr/bin/env python3
"""Performance dashboard — terminal-based display of bot metrics.

Usage:
    # One-shot report from a live MetricsCollector:
    render_dashboard(collector.summary(), collector.category_stats(),
                     collector.latency_percentiles(), collector.pnl_curve(),
                     collector.latency_histogram(), collector.liquidity_stats(),
                     risk_snapshot)

    # Live refresh (as a coroutine):
    await live_dashboard(collector, snapshot_fn, interval=5)
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from typing import Callable

from src.core.types import MarketCategory
from src.monitor.metrics import (
    CategoryStats,
    LatencySample,
    MetricsCollector,
    PnLPoint,
)

SnapshotFn = Callable[[], dict[str, object]]

# ── Rendering helpers ───────────────────────────────────────────

SEPARATOR = "=" * 72
THIN_SEP = "-" * 72


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _usd(value: Decimal | float) -> str:
    return f"${float(value):,.2f}"


def _secs(value: float) -> str:
    if value < 1.0:
        return f"{value * 1000:.0f}ms"
    return f"{value:.2f}s"


def _bar(value: int, max_value: int, width: int = 30) -> str:
    if max_value == 0:
        return ""
    filled = round(value / max_value * width)
    return "#" * filled + "." * (width - filled)


# ── Section renderers ───────────────────────────────────────────


def render_header() -> str:
    lines = [
        SEPARATOR,
        "  POLYMARKET ARB BOT — PERFORMANCE DASHBOARD",
        SEPARATOR,
    ]
    return "\n".join(lines)


def render_summary(summary: dict[str, object]) -> str:
    lines = [
        "",
        "  OVERALL SUMMARY",
        THIN_SEP,
        f"  Total Trades:     {summary['total_trades']}",
        f"  Successful:       {summary['successful_trades']}",
        f"  Failed:           {summary['failed_trades']}",
        f"  Win Rate:         {_pct(summary['win_rate'])}",  # type: ignore[arg-type]
        f"  Cumulative P&L:   {_usd(summary['cumulative_pnl'])}",  # type: ignore[arg-type]
        f"  Avg Profit/Trade: {_usd(summary['avg_profit_per_trade'])}",  # type: ignore[arg-type]
        "",
        f"  Signals Generated: {summary['signals_generated']}",
        f"  Trades Skipped:    {summary['trades_skipped']}",
        f"  Risk Rejected:     {summary['risk_rejected']}",
    ]
    return "\n".join(lines)


def render_category_stats(
    stats: dict[MarketCategory, CategoryStats],
) -> str:
    if not stats:
        return "\n  CATEGORY BREAKDOWN\n" + THIN_SEP + "\n  (no trades yet)"

    lines = [
        "",
        "  CATEGORY BREAKDOWN",
        THIN_SEP,
        f"  {'Category':<12} {'Trades':>7} {'Wins':>6} {'Losses':>7}"
        f" {'Win%':>7} {'P&L':>12} {'Avg':>10}",
        f"  {'-'*12} {'-'*7} {'-'*6} {'-'*7} {'-'*7} {'-'*12} {'-'*10}",
    ]
    for cat in sorted(stats, key=lambda c: c.value):
        s = stats[cat]
        lines.append(
            f"  {s.category.value:<12} {s.total_trades:>7} {s.wins:>6}"
            f" {s.losses:>7} {_pct(s.win_rate):>7}"
            f" {_usd(s.total_profit):>12} {_usd(s.avg_profit):>10}"
        )
    return "\n".join(lines)


def render_latency(percentiles: dict[str, float]) -> str:
    lines = [
        "",
        "  LATENCY (event release -> order fill)",
        THIN_SEP,
        f"  Min:  {_secs(percentiles['min'])}",
        f"  P50:  {_secs(percentiles['p50'])}",
        f"  P90:  {_secs(percentiles['p90'])}",
        f"  P99:  {_secs(percentiles['p99'])}",
        f"  Max:  {_secs(percentiles['max'])}",
    ]
    return "\n".join(lines)


def render_latency_histogram(
    histogram: list[tuple[float, float, int]],
) -> str:
    if not histogram:
        return ""

    max_count = max(c for _, _, c in histogram)
    lines = [
        "",
        "  LATENCY HISTOGRAM",
        THIN_SEP,
    ]
    for lo, hi, count in histogram:
        bar = _bar(count, max_count, 30)
        lines.append(f"  {_secs(lo):>8}-{_secs(hi):<8} |{bar}| {count}")
    return "\n".join(lines)


def render_pnl_curve(curve: list[PnLPoint], width: int = 60) -> str:
    if not curve:
        return "\n  CUMULATIVE P&L CURVE\n" + THIN_SEP + "\n  (no data)"

    values = [float(p.cumulative_pnl) for p in curve]
    lo, hi = min(values), max(values)

    lines = [
        "",
        "  CUMULATIVE P&L CURVE",
        THIN_SEP,
    ]

    if lo == hi:
        lines.append(f"  Flat at {_usd(Decimal(str(values[0])))}")
        return "\n".join(lines)

    height = 10
    for row in range(height, -1, -1):
        threshold = lo + (hi - lo) * row / height
        label = f"{threshold:>9.2f} |"
        chars = []
        # Sample evenly across the curve
        for col in range(width):
            idx = int(col * len(values) / width)
            val = values[min(idx, len(values) - 1)]
            if val >= threshold:
                chars.append("*")
            else:
                chars.append(" ")
        lines.append(f"  {label}{''.join(chars)}|")

    lines.append(f"  {' ' * 10}+{'-' * width}+")
    lines.append(
        f"  {' ' * 11}Trade 1{' ' * (width - 14)}Trade {len(curve)}"
    )
    return "\n".join(lines)


def render_liquidity(liquidity: dict[str, object]) -> str:
    lines = [
        "",
        "  LIQUIDITY CAPTURED vs AVAILABLE",
        THIN_SEP,
        f"  Captured:      {_usd(liquidity['total_captured_usd'])}",  # type: ignore[arg-type]
        f"  Available:     {_usd(liquidity['total_available_usd'])}",  # type: ignore[arg-type]
        f"  Capture Ratio: {_pct(liquidity['capture_ratio'])}",  # type: ignore[arg-type]
    ]
    return "\n".join(lines)


def render_risk_snapshot(snap: dict[str, object] | None) -> str:
    if snap is None:
        return ""

    status = "KILLED" if snap.get("killed") else "ACTIVE"
    lines = [
        "",
        "  RISK STATUS",
        THIN_SEP,
        f"  Status:           {status}",
        f"  Open Positions:   {snap.get('open_positions', 0)}",
        f"  Exposure:         {_usd(snap.get('total_exposure_usd', 0))}",  # type: ignore[arg-type]
        f"  Realized Today:   {_usd(snap.get('realized_today', 0))}",  # type: ignore[arg-type]
        f"  Realized Total:   {_usd(snap.get('realized_total', 0))}",  # type: ignore[arg-type]
        f"  Trades Today:     {snap.get('trade_count_today', 0)}",
    ]
    trigger = snap.get("kill_switch_trigger")
    if trigger:
        lines.append(f"  Kill Trigger:     {trigger}")
        lines.append(f"  Kill Reason:      {snap.get('kill_switch_reason', '')}")
    disputed = snap.get("disputed_markets")
    if disputed is not None:
        lines.append(f"  Disputed Markets: {disputed}")
        lines.append(
            f"  Exposure at Risk: {_usd(snap.get('exposure_at_risk_usd', 0))}"  # type: ignore[arg-type]
        )
    return "\n".join(lines)


# ── Full dashboard ──────────────────────────────────────────────


def render_dashboard(
    summary: dict[str, object],
    cat_stats: dict[MarketCategory, CategoryStats],
    latency_pct: dict[str, float],
    pnl: list[PnLPoint],
    histogram: list[tuple[float, float, int]],
    liquidity: dict[str, object],
    risk_snap: dict[str, object] | None = None,
) -> str:
    """Render the complete dashboard as a string."""
    sections = [
        render_header(),
        render_summary(summary),
        render_category_stats(cat_stats),
        render_latency(latency_pct),
        render_latency_histogram(histogram),
        render_pnl_curve(pnl),
        render_liquidity(liquidity),
        render_risk_snapshot(risk_snap),
        "",
        SEPARATOR,
    ]
    return "\n".join(s for s in sections if s)


def render_from_collector(
    collector: MetricsCollector,
    risk_snap: dict[str, object] | None = None,
) -> str:
    """Convenience: render dashboard directly from a MetricsCollector."""
    return render_dashboard(
        summary=collector.summary(),
        cat_stats=collector.category_stats(),
        latency_pct=collector.latency_percentiles(),
        pnl=collector.pnl_curve(),
        histogram=collector.latency_histogram(),
        liquidity=collector.liquidity_stats(),
        risk_snap=risk_snap,
    )


async def live_dashboard(
    collector: MetricsCollector,
    snapshot_fn: SnapshotFn | None = None,
    interval: float = 5.0,
) -> None:
    """Continuously refresh the dashboard in the terminal.

    Runs until cancelled.
    """
    while True:
        risk_snap = snapshot_fn() if snapshot_fn else None
        output = render_from_collector(collector, risk_snap)

        # Clear screen
        os.system("cls" if os.name == "nt" else "clear")
        print(output)

        await asyncio.sleep(interval)
