#!/usr/bin/env python3
"""Performance dashboard — sleek terminal UI with ANSI colors and box-drawing.

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
import time
from datetime import datetime, timezone
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


# ── ANSI Color Codes ──────────────────────────────────────────────

class C:
    """ANSI color/style codes."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"

    # Foreground
    WHITE   = "\033[97m"
    GREY    = "\033[90m"
    CYAN    = "\033[96m"
    GREEN   = "\033[92m"
    RED     = "\033[91m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"

    # Background
    BG_DARK   = "\033[48;5;234m"
    BG_HEADER = "\033[48;5;17m"
    BG_ROW    = "\033[48;5;235m"
    BG_ALT    = "\033[48;5;236m"


# ── Box Drawing Characters ────────────────────────────────────────

# Single-line box
TL = "\u250c"  # top-left
TR = "\u2510"  # top-right
BL = "\u2514"  # bottom-left
BR = "\u2518"  # bottom-right
H  = "\u2500"  # horizontal
V  = "\u2502"  # vertical
LT = "\u251c"  # left-tee
RT = "\u2524"  # right-tee
TT = "\u252c"  # top-tee
BT = "\u2534"  # bottom-tee
CR = "\u253c"  # cross

# Double-line box for header
DH  = "\u2550"
DV  = "\u2551"
DTL = "\u2554"
DTR = "\u2557"
DBL = "\u255a"
DBR = "\u255d"

# Block/bar chars
FULL_BLOCK  = "\u2588"
LIGHT_SHADE = "\u2591"

# Sparkline chars (ascending 1/8th blocks)
SPARK = [" ", "\u2581", "\u2582", "\u2583", "\u2584", "\u2585", "\u2586", "\u2587", "\u2588"]

DASHBOARD_WIDTH = 80


# ── Formatting Helpers ────────────────────────────────────────────


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _usd(value: Decimal | float) -> str:
    return f"${float(value):,.2f}"


def _usd_color(value: Decimal | float) -> str:
    """USD with green/red color based on sign."""
    v = float(value)
    color = C.GREEN if v >= 0 else C.RED
    return f"{color}{_usd(value)}{C.RESET}"


def _secs(value: float) -> str:
    if value < 1.0:
        return f"{value * 1000:.0f}ms"
    return f"{value:.2f}s"


def _bar_colored(value: int, max_value: int, width: int = 25) -> str:
    """Colored bar using block characters."""
    if max_value == 0:
        return C.DIM + LIGHT_SHADE * width + C.RESET
    filled = round(value / max_value * width)
    return (
        C.CYAN + FULL_BLOCK * filled + C.RESET
        + C.DIM + LIGHT_SHADE * (width - filled) + C.RESET
    )


def _sparkline(values: list[float], width: int = 30) -> str:
    """Generate a sparkline from a list of values."""
    if not values:
        return C.DIM + "---" + C.RESET
    if len(values) == 1:
        return C.GREEN + SPARK[4] + C.RESET

    lo, hi = min(values), max(values)
    span = hi - lo if hi != lo else 1.0

    # Sample down to width
    sampled = []
    for i in range(width):
        idx = int(i * len(values) / width)
        sampled.append(values[min(idx, len(values) - 1)])

    chars = []
    for v in sampled:
        level = int((v - lo) / span * 7)
        level = max(0, min(7, level))
        chars.append(SPARK[level + 1])

    # Color the sparkline based on trend
    last = values[-1]
    color = C.GREEN if last >= values[0] else C.RED
    return color + "".join(chars) + C.RESET


def _hline(width: int = DASHBOARD_WIDTH) -> str:
    return C.DIM + H * width + C.RESET


def _box_top(width: int = DASHBOARD_WIDTH) -> str:
    return C.DIM + TL + H * (width - 2) + TR + C.RESET


def _box_bot(width: int = DASHBOARD_WIDTH) -> str:
    return C.DIM + BL + H * (width - 2) + BR + C.RESET


def _box_mid(width: int = DASHBOARD_WIDTH) -> str:
    return C.DIM + LT + H * (width - 2) + RT + C.RESET


def _box_row(content: str, width: int = DASHBOARD_WIDTH) -> str:
    """Pad content inside box borders."""
    # Strip ANSI for length calculation
    stripped = _strip_ansi(content)
    pad = width - 2 - len(stripped)
    if pad < 0:
        pad = 0
    return C.DIM + V + C.RESET + " " + content + " " * max(0, pad - 1) + C.DIM + V + C.RESET


def _strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences for length calculations."""
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


def _center(text: str, width: int = DASHBOARD_WIDTH - 2) -> str:
    """Center text accounting for ANSI codes."""
    visible_len = len(_strip_ansi(text))
    pad = max(0, width - visible_len)
    left = pad // 2
    right = pad - left
    return " " * left + text + " " * right


def _kv(key: str, value: str, key_width: int = 20) -> str:
    """Key-value pair with dim key and bright value."""
    return f"  {C.DIM}{key:<{key_width}}{C.RESET}{C.BOLD}{C.WHITE}{value}{C.RESET}"


# ── Section Renderers ─────────────────────────────────────────────


def render_header(paper_mode: bool = False) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    mode = f"  {C.YELLOW}{C.BOLD}PAPER MODE{C.RESET}" if paper_mode else ""

    title = f"{C.BOLD}{C.CYAN}POLYMARKET ARB BOT{C.RESET}"
    subtitle = f"{C.DIM}Performance Dashboard{C.RESET}"

    lines = [
        "",
        C.DIM + DTL + DH * (DASHBOARD_WIDTH - 2) + DTR + C.RESET,
        C.DIM + DV + C.RESET + _center(title) + C.DIM + DV + C.RESET,
        C.DIM + DV + C.RESET + _center(subtitle + mode) + C.DIM + DV + C.RESET,
        C.DIM + DV + C.RESET + _center(f"{C.DIM}{now}{C.RESET}") + C.DIM + DV + C.RESET,
        C.DIM + DBL + DH * (DASHBOARD_WIDTH - 2) + DBR + C.RESET,
    ]
    return "\n".join(lines)


def render_summary(summary: dict[str, object]) -> str:
    total = summary["total_trades"]
    wins = summary["successful_trades"]
    fails = summary["failed_trades"]
    wr = summary["win_rate"]  # type: ignore[assignment]
    pnl = summary["cumulative_pnl"]
    avg = summary["avg_profit_per_trade"]
    sigs = summary["signals_generated"]
    skipped = summary["trades_skipped"]
    rejected = summary["risk_rejected"]

    # Win rate color
    wr_float = float(wr)  # type: ignore[arg-type]
    wr_color = C.GREEN if wr_float >= 0.8 else C.YELLOW if wr_float >= 0.5 else C.RED
    wr_str = f"{wr_color}{C.BOLD}{_pct(wr_float)}{C.RESET}"

    lines = [
        "",
        _box_top(),
        _box_row(f"{C.BOLD}{C.WHITE}  OVERVIEW{C.RESET}"),
        _box_mid(),
        _box_row(
            f"  {C.CYAN}Trades{C.RESET}  {C.BOLD}{total}{C.RESET}"
            f"   {C.GREEN}{wins}W{C.RESET}"
            f"  {C.RED}{fails}L{C.RESET}"
            f"     {C.CYAN}Win Rate{C.RESET}  {wr_str}"
            f"     {C.CYAN}P&L{C.RESET}  {_usd_color(pnl)}"  # type: ignore[arg-type]
        ),
        _box_row(
            f"  {C.CYAN}Avg Profit{C.RESET}  {_usd_color(avg)}"  # type: ignore[arg-type]
            f"   {C.DIM}|{C.RESET}"
            f"  {C.CYAN}Signals{C.RESET} {C.WHITE}{sigs}{C.RESET}"
            f"  {C.CYAN}Skipped{C.RESET} {C.WHITE}{skipped}{C.RESET}"
            f"  {C.CYAN}Rejected{C.RESET} {C.WHITE}{rejected}{C.RESET}"
        ),
        _box_bot(),
    ]
    return "\n".join(lines)


def render_category_stats(
    stats: dict[MarketCategory, CategoryStats],
) -> str:
    if not stats:
        lines = [
            "",
            _box_top(),
            _box_row(f"{C.BOLD}{C.WHITE}  CATEGORIES{C.RESET}"),
            _box_mid(),
            _box_row(f"  {C.DIM}Waiting for first trade...{C.RESET}"),
            _box_bot(),
        ]
        return "\n".join(lines)

    header = (
        f"  {C.BOLD}{C.WHITE}{'Category':<12}"
        f" {'Trades':>7}"
        f"  {'Win':>4}"
        f" {'Loss':>4}"
        f"  {'Win%':>7}"
        f"  {'P&L':>12}"
        f"  {'Avg':>10}{C.RESET}"
    )
    lines = [
        "",
        _box_top(),
        _box_row(f"{C.BOLD}{C.WHITE}  CATEGORIES{C.RESET}"),
        _box_mid(),
        _box_row(header),
        _box_row(f"  {C.DIM}{H * 65}{C.RESET}"),
    ]

    for i, cat in enumerate(sorted(stats, key=lambda c: c.value)):
        s = stats[cat]
        wr_color = C.GREEN if s.win_rate >= 0.8 else C.YELLOW if s.win_rate >= 0.5 else C.RED
        pnl_color = C.GREEN if s.total_profit >= 0 else C.RED

        row = (
            f"  {C.CYAN}{s.category.value:<12}{C.RESET}"
            f" {C.WHITE}{s.total_trades:>7}{C.RESET}"
            f"  {C.GREEN}{s.wins:>4}{C.RESET}"
            f" {C.RED}{s.losses:>4}{C.RESET}"
            f"  {wr_color}{_pct(s.win_rate):>7}{C.RESET}"
            f"  {pnl_color}{_usd(s.total_profit):>12}{C.RESET}"
            f"  {pnl_color}{_usd(s.avg_profit):>10}{C.RESET}"
        )
        lines.append(_box_row(row))

    lines.append(_box_bot())
    return "\n".join(lines)


def render_latency(percentiles: dict[str, float]) -> str:
    lines = [
        "",
        _box_top(),
        _box_row(f"{C.BOLD}{C.WHITE}  LATENCY{C.RESET}  {C.DIM}event release -> order fill{C.RESET}"),
        _box_mid(),
    ]

    metrics = [
        ("Min", percentiles["min"]),
        ("P50", percentiles["p50"]),
        ("P90", percentiles["p90"]),
        ("P99", percentiles["p99"]),
        ("Max", percentiles["max"]),
    ]

    parts = []
    for name, val in metrics:
        color = C.GREEN if val < 0.5 else C.YELLOW if val < 2.0 else C.RED
        parts.append(f"  {C.DIM}{name}{C.RESET} {color}{C.BOLD}{_secs(val)}{C.RESET}")

    lines.append(_box_row("  ".join(parts)))
    lines.append(_box_bot())
    return "\n".join(lines)


def render_latency_histogram(
    histogram: list[tuple[float, float, int]],
) -> str:
    if not histogram:
        return ""

    max_count = max(c for _, _, c in histogram)
    lines = [
        "",
        _box_top(),
        _box_row(f"{C.BOLD}{C.WHITE}  LATENCY DISTRIBUTION{C.RESET}"),
        _box_mid(),
    ]
    for lo, hi, count in histogram:
        bar = _bar_colored(count, max_count, 25)
        lines.append(_box_row(
            f"  {C.DIM}{_secs(lo):>7}-{_secs(hi):<7}{C.RESET} {bar} {C.WHITE}{count}{C.RESET}"
        ))

    lines.append(_box_bot())
    return "\n".join(lines)


def render_pnl_curve(curve: list[PnLPoint], width: int = 50) -> str:
    lines = [
        "",
        _box_top(),
        _box_row(f"{C.BOLD}{C.WHITE}  CUMULATIVE P&L{C.RESET}"),
        _box_mid(),
    ]

    if not curve:
        lines.append(_box_row(f"  {C.DIM}Waiting for trades...{C.RESET}"))
        lines.append(_box_bot())
        return "\n".join(lines)

    values = [float(p.cumulative_pnl) for p in curve]
    lo, hi = min(values), max(values)

    if lo == hi:
        lines.append(_box_row(f"  {C.DIM}Flat at{C.RESET} {_usd_color(Decimal(str(values[0])))}"))
        lines.append(_box_bot())
        return "\n".join(lines)

    # Sparkline representation
    spark = _sparkline(values, width)
    lines.append(_box_row(f"  {spark}"))
    lines.append(_box_row(
        f"  {C.DIM}Low{C.RESET} {_usd_color(Decimal(str(lo)))}"
        f"  {C.DIM}{H * 3}{C.RESET}"
        f"  {C.DIM}Current{C.RESET} {_usd_color(Decimal(str(values[-1])))}"
        f"  {C.DIM}{H * 3}{C.RESET}"
        f"  {C.DIM}High{C.RESET} {_usd_color(Decimal(str(hi)))}"
    ))

    # ASCII chart
    height = 8
    for row in range(height, -1, -1):
        threshold = lo + (hi - lo) * row / height
        label = f"{threshold:>8.1f}"
        chars = []
        for col in range(width):
            idx = int(col * len(values) / width)
            val = values[min(idx, len(values) - 1)]
            if val >= threshold:
                chars.append(FULL_BLOCK)
            else:
                chars.append(" ")
        color = C.GREEN if values[-1] >= 0 else C.RED
        lines.append(_box_row(
            f"  {C.DIM}{label} {V}{C.RESET}{color}{''.join(chars)}{C.RESET}{C.DIM}{V}{C.RESET}"
        ))

    lines.append(_box_row(
        f"  {' ' * 9}{C.DIM}{BL}{H * width}{BR}{C.RESET}"
    ))
    lines.append(_box_row(
        f"  {' ' * 10}{C.DIM}Trade 1{' ' * (width - 14)}Trade {len(curve)}{C.RESET}"
    ))
    lines.append(_box_bot())
    return "\n".join(lines)


def render_liquidity(liquidity: dict[str, object]) -> str:
    captured = liquidity["total_captured_usd"]
    available = liquidity["total_available_usd"]
    ratio = liquidity["capture_ratio"]

    lines = [
        "",
        _box_top(),
        _box_row(f"{C.BOLD}{C.WHITE}  LIQUIDITY{C.RESET}"),
        _box_mid(),
        _box_row(
            f"  {C.CYAN}Captured{C.RESET} {_usd_color(captured)}"  # type: ignore[arg-type]
            f"   {C.DIM}of{C.RESET} {C.WHITE}{_usd(available)}{C.RESET}"  # type: ignore[arg-type]
            f"   {C.DIM}({C.RESET}{C.YELLOW}{_pct(ratio)}{C.RESET}{C.DIM}){C.RESET}"  # type: ignore[arg-type]
        ),
        _box_bot(),
    ]
    return "\n".join(lines)


def render_risk_snapshot(snap: dict[str, object] | None) -> str:
    if snap is None:
        return ""

    killed = snap.get("killed", False)
    status_color = C.RED if killed else C.GREEN
    status_text = "KILLED" if killed else "ACTIVE"
    status_icon = "X" if killed else "+"

    lines = [
        "",
        _box_top(),
        _box_row(
            f"{C.BOLD}{C.WHITE}  RISK STATUS{C.RESET}"
            f"   {status_color}{C.BOLD}{status_icon} {status_text}{C.RESET}"
        ),
        _box_mid(),
        _box_row(
            f"  {C.CYAN}Positions{C.RESET} {C.WHITE}{snap.get('open_positions', 0)}{C.RESET}"
            f"   {C.CYAN}Exposure{C.RESET} {_usd_color(snap.get('total_exposure_usd', 0))}"  # type: ignore[arg-type]
            f"   {C.CYAN}Today{C.RESET} {_usd_color(snap.get('realized_today', 0))}"  # type: ignore[arg-type]
            f"   {C.CYAN}Total{C.RESET} {_usd_color(snap.get('realized_total', 0))}"  # type: ignore[arg-type]
        ),
        _box_row(
            f"  {C.CYAN}Trades Today{C.RESET} {C.WHITE}{snap.get('trade_count_today', 0)}{C.RESET}"
        ),
    ]

    trigger = snap.get("kill_switch_trigger")
    if trigger:
        lines.append(_box_row(
            f"  {C.RED}{C.BOLD}Kill: {trigger}{C.RESET}"
            f"  {C.DIM}{snap.get('kill_switch_reason', '')}{C.RESET}"
        ))

    disputed = snap.get("disputed_markets")
    if disputed is not None:
        lines.append(_box_row(
            f"  {C.YELLOW}Disputed Markets{C.RESET} {C.WHITE}{disputed}{C.RESET}"
            f"   {C.YELLOW}At Risk{C.RESET} {C.RED}{_usd(snap.get('exposure_at_risk_usd', 0))}{C.RESET}"  # type: ignore[arg-type]
        ))

    lines.append(_box_bot())
    return "\n".join(lines)


# ── Full Dashboard ────────────────────────────────────────────────


def render_dashboard(
    summary: dict[str, object],
    cat_stats: dict[MarketCategory, CategoryStats],
    latency_pct: dict[str, float],
    pnl: list[PnLPoint],
    histogram: list[tuple[float, float, int]],
    liquidity: dict[str, object],
    risk_snap: dict[str, object] | None = None,
    paper_mode: bool = False,
) -> str:
    """Render the complete dashboard as a string."""
    sections = [
        render_header(paper_mode=paper_mode),
        render_summary(summary),
        render_category_stats(cat_stats),
        render_latency(latency_pct),
        render_latency_histogram(histogram),
        render_pnl_curve(pnl),
        render_liquidity(liquidity),
        render_risk_snapshot(risk_snap),
        "",
    ]
    return "\n".join(s for s in sections if s)


def render_from_collector(
    collector: MetricsCollector,
    risk_snap: dict[str, object] | None = None,
    paper_mode: bool = False,
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
        paper_mode=paper_mode,
    )


async def live_dashboard(
    collector: MetricsCollector,
    snapshot_fn: SnapshotFn | None = None,
    interval: float = 5.0,
    paper_mode: bool = False,
) -> None:
    """Continuously refresh the dashboard in the terminal.

    Runs until cancelled.
    """
    while True:
        risk_snap = snapshot_fn() if snapshot_fn else None
        output = render_from_collector(collector, risk_snap, paper_mode=paper_mode)

        # Clear screen
        if os.name == "nt":
            os.system("cls")
        else:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        print(output)

        await asyncio.sleep(interval)
