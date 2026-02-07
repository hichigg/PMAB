#!/usr/bin/env python3
"""Backtest CLI — replay historical scenarios through the strategy pipeline.

Usage:
    python -m scripts.backtest scenario.json
    python -m scripts.backtest scenario.json --slippage-bps 5
    python -m scripts.backtest scenario.json --fill-prob 0.95

Scenario JSON format::

    {
        "name": "CPI release Jan 2025",
        "description": "...",
        "opportunities": {
            "cond_1": {
                "condition_id": "cond_1",
                "question": "Will CPI be above 3.0%?",
                "category": "ECONOMIC",
                "tokens": [{"token_id": "tok_yes", "outcome": "Yes"},
                           {"token_id": "tok_no", "outcome": "No"}],
                "token_id": "tok_yes",
                "best_bid": "0.85",
                "best_ask": "0.87",
                "spread": "0.02",
                "depth_usd": "5000",
                "bid_depth_usd": "2500",
                "ask_depth_usd": "2500",
                "score": 0.8,
                "fee_rate_bps": 0
            }
        },
        "events": [
            {
                "feed_event": {
                    "feed_type": "ECONOMIC",
                    "event_type": "DATA_RELEASED",
                    "indicator": "CPI",
                    "value": "3.2",
                    "numeric_value": "3.2",
                    "outcome_type": "NUMERIC",
                    "released_at": 1700000000.0,
                    "received_at": 1700000000.5
                },
                "orderbooks": {
                    "tok_yes": {
                        "token_id": "tok_yes",
                        "bids": [{"price": "0.85", "size": "1000"}],
                        "asks": [{"price": "0.87", "size": "1000"}]
                    }
                }
            }
        ]
    }
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from src.backtest.replay import BacktestEngine
from src.backtest.types import BacktestConfig, Scenario
from src.core.config import load_settings
from scripts.dashboard import render_from_collector


def load_scenario(path: str) -> Scenario:
    """Load a Scenario from a JSON file."""
    with open(path) as f:
        data = json.load(f)
    return Scenario.model_validate(data)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay a historical scenario through the strategy pipeline.",
    )
    parser.add_argument(
        "scenario",
        help="Path to scenario JSON file",
    )
    parser.add_argument(
        "--slippage-bps",
        type=int,
        default=0,
        help="Simulated slippage in basis points (default: 0)",
    )
    parser.add_argument(
        "--fill-prob",
        type=float,
        default=1.0,
        help="Fill probability 0.0-1.0 (default: 1.0)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to settings YAML (default: config/settings.yaml)",
    )
    return parser.parse_args(argv)


async def run_backtest(args: argparse.Namespace) -> None:
    # Load base settings
    load_settings(args.config)

    # Load scenario
    scenario = load_scenario(args.scenario)

    # Build backtest config
    config = BacktestConfig(
        fill_probability=args.fill_prob,
        slippage_bps=args.slippage_bps,
    )

    print(f"Running backtest: {scenario.name}")
    print(f"  Events: {len(scenario.events)}")
    print(f"  Opportunities: {len(scenario.opportunities)}")
    print(f"  Fill prob: {config.fill_probability}")
    print(f"  Slippage: {config.slippage_bps} bps")
    print()

    # Run
    engine = BacktestEngine(scenario, config)
    result = await engine.run()

    # Display results
    dashboard = render_from_collector(
        engine.collector,
        risk_snap=engine.risk_snapshot,
    )
    print(dashboard)

    # Print simulated fill details
    fills = engine.sim_client.fills
    if fills:
        print()
        print("SIMULATED FILLS")
        print("-" * 72)
        for fill in fills:
            status = "OK" if fill.success else "FAIL"
            print(
                f"  [{status}] {fill.token_id} {fill.side}"
                f" price={fill.fill_price:.4f}"
                f" size={fill.fill_size:.2f}"
                f" slippage={fill.slippage:.6f}"
            )

    # Summary line
    print()
    print(f"Backtest complete: {result.successful_trades}/{result.total_trades}"
          f" trades filled, P&L={result.cumulative_pnl}")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    asyncio.run(run_backtest(args))


if __name__ == "__main__":
    main()
