#!/usr/bin/env python3
"""Paper trading entrypoint — real market data with simulated execution.

Uses real Polymarket orderbooks (GET only) with SimulatedClient fills.
Runs the live terminal dashboard and sends [PAPER] prefixed Discord alerts.

Usage::

    # Run with default config
    python scripts/paper_trade.py

    # Custom config file
    python scripts/paper_trade.py --config config/settings.yaml

    # Override log level
    python scripts/paper_trade.py --log-level DEBUG
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

# Ensure project root is on sys.path so `scripts.dashboard` is importable.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import structlog

from src.core.config import load_settings
from src.core.logging import setup_logging
from src.feeds.crypto import CryptoFeed
from src.feeds.economic import EconomicFeed
from src.feeds.sports import SportsFeed
from src.monitor.factory import create_monitor_stack
from src.monitor.metrics import MetricsCollector
from src.monitor.web_dashboard import start_web_dashboard
from src.paper.client import PaperTradingClient
from src.polymarket.scanner import MarketScanner
from src.risk.market_quality import MarketQualityFilter
from src.risk.monitor import RiskMonitor
from src.risk.oracle_monitor import OracleMonitor
from src.strategy.engine import ArbEngine

logger = structlog.get_logger(__name__)

BANNER = r"""
========================================================================
  POLYMARKET ARB BOT — PAPER TRADING MODE
  Starting bankroll: $10,000 (simulated)
  Orders are NOT sent to Polymarket — fills are simulated locally.
========================================================================
"""


async def run(args: argparse.Namespace) -> int:
    """Start all components in paper trading mode and run until interrupted."""
    settings = load_settings(args.config)
    setup_logging(level=args.log_level)

    print(BANNER)
    logger.info(
        "paper_trading_starting",
        economic=settings.feeds.economic.enabled,
        sports=settings.feeds.sports.enabled,
        crypto=settings.feeds.crypto.enabled,
        fill_probability=settings.paper_trading.fill_probability,
        slippage_bps=settings.paper_trading.slippage_bps,
        orderbook_refresh_secs=settings.paper_trading.orderbook_refresh_secs,
    )

    # ── Paper trading client ──────────────────────────────────────
    client = PaperTradingClient(
        pm_config=settings.polymarket,
        paper_config=settings.paper_trading,
    )
    await client.connect()

    # ── Market scanner ────────────────────────────────────────────
    scanner = MarketScanner(client=client, config=settings.scanner)

    # ── Oracle monitor ────────────────────────────────────────────
    oracle_monitor: OracleMonitor | None = None
    if settings.risk.oracle.enabled:
        oracle_monitor = OracleMonitor(config=settings.risk.oracle)

    # ── Risk monitor ──────────────────────────────────────────────
    risk_monitor = RiskMonitor(
        config=settings.risk,
        oracle_monitor=oracle_monitor,
    )

    # ── Market quality filter ─────────────────────────────────────
    quality_filter = MarketQualityFilter(
        config=settings.risk,
        oracle_monitor=oracle_monitor,
    )

    # ── Strategy engine ───────────────────────────────────────────
    engine = ArbEngine(
        client=client,
        scanner=scanner,
        config=settings.strategy,
        risk_config=settings.risk,
        risk_monitor=risk_monitor,
        quality_filter=quality_filter,
    )

    # ── Metrics collector ─────────────────────────────────────────
    collector = MetricsCollector()
    engine.on_event(collector.on_arb_event)

    # ── Alert dispatcher (paper_mode=True → [PAPER] prefix) ──────
    dispatcher, scheduler = create_monitor_stack(
        config=settings.alerts,
        snapshot_fn=risk_monitor.snapshot,
        paper_mode=True,
    )
    engine.on_event(dispatcher.on_arb_event)
    risk_monitor.on_event(dispatcher.on_risk_event)

    # ── Data feeds ────────────────────────────────────────────────
    feeds = []

    if settings.feeds.economic.enabled:
        econ_feed = EconomicFeed(settings.feeds.economic)
        econ_feed.on_event(engine.on_feed_event)
        econ_feed.on_event(dispatcher.on_feed_event)
        feeds.append(econ_feed)
        logger.info("feed_enabled", feed="economic")

    if settings.feeds.sports.enabled:
        sports_feed = SportsFeed(settings.feeds.sports)
        sports_feed.on_event(engine.on_feed_event)
        sports_feed.on_event(dispatcher.on_feed_event)
        feeds.append(sports_feed)
        logger.info("feed_enabled", feed="sports")

    if settings.feeds.crypto.enabled:
        crypto_feed = CryptoFeed(settings.feeds.crypto)
        crypto_feed.on_event(engine.on_feed_event)
        crypto_feed.on_event(dispatcher.on_feed_event)
        feeds.append(crypto_feed)
        logger.info("feed_enabled", feed="crypto")

    if not feeds:
        logger.error("no_feeds_enabled")
        print(
            "No feeds enabled. Enable at least one feed in config/settings.yaml "
            "(feeds.economic.enabled, feeds.sports.enabled, or feeds.crypto.enabled).",
            file=sys.stderr,
        )
        await client.close()
        return 1

    # ── Start everything ──────────────────────────────────────────
    await engine.start()
    await scanner.start()

    if oracle_monitor is not None:
        await oracle_monitor.start()

    if scheduler is not None:
        await scheduler.start()

    for feed in feeds:
        await feed.start()

    # Start background orderbook refresh for realistic simulated fills
    await client.start_orderbook_refresh()

    logger.info(
        "paper_trading_running",
        feeds=len(feeds),
        scanner="active",
        risk_monitor="active",
        oracle_monitor="active" if oracle_monitor else "disabled",
    )

    # ── Live terminal dashboard as concurrent task ────────────────
    # Import here to avoid circular dependency at module level
    from scripts.dashboard import live_dashboard

    dashboard_task = asyncio.create_task(
        live_dashboard(collector, risk_monitor.snapshot, interval=5.0, paper_mode=True),
    )

    # ── Web dashboard (accessible via browser) ───────────────────
    dash_user = os.environ.get("DASHBOARD_USER", "Hichi")
    dash_pass = os.environ.get("DASHBOARD_PASS", "1337")
    web_runner = await start_web_dashboard(
        collector=collector,
        snapshot_fn=risk_monitor.snapshot,
        host="0.0.0.0",
        port=8080,
        username=dash_user,
        password=dash_pass,
    )
    logger.info("web_dashboard_started", url="http://0.0.0.0:8080", auth="enabled")

    # ── Wait for shutdown signal ──────────────────────────────────
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("shutdown_signal_received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows: signal handlers not supported on ProactorEventLoop
            pass

    # On Windows, also handle KeyboardInterrupt
    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")

    # ── Graceful shutdown ─────────────────────────────────────────
    logger.info("paper_trading_shutting_down")

    dashboard_task.cancel()
    try:
        await dashboard_task
    except asyncio.CancelledError:
        pass

    await web_runner.cleanup()
    logger.info("web_dashboard_stopped")

    for feed in feeds:
        try:
            await feed.stop()
        except Exception:
            logger.exception("feed_stop_error")

    if scheduler is not None:
        await scheduler.stop()

    if oracle_monitor is not None:
        await oracle_monitor.stop()

    await scanner.stop()
    await engine.stop()
    await dispatcher.close()
    await client.close()

    # ── Final summary ─────────────────────────────────────────────
    summary = collector.summary()
    snap = risk_monitor.snapshot()
    fills = client.sim.fills

    print("\n" + "=" * 72)
    print("  PAPER TRADING SESSION SUMMARY")
    print("=" * 72)
    print(f"  Total Trades:     {summary['total_trades']}")
    print(f"  Successful:       {summary['successful_trades']}")
    print(f"  Cumulative P&L:   ${float(summary['cumulative_pnl']):,.2f}")
    print(f"  Realized Today:   {snap['realized_today']}")
    print(f"  Simulated Fills:  {len(fills)}")
    print("=" * 72)

    logger.info(
        "paper_trading_stopped",
        total_trades=summary["total_trades"],
        successful_trades=summary["successful_trades"],
        cumulative_pnl=str(summary["cumulative_pnl"]),
        realized_today=snap["realized_today"],
        simulated_fills=len(fills),
    )

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Polymarket arb bot in paper trading mode.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to settings YAML (default: config/settings.yaml)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Log level override: DEBUG, INFO, WARNING, ERROR",
    )
    args = parser.parse_args()

    code = asyncio.run(run(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
