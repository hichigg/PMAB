#!/usr/bin/env python3
"""Market scanner CLI — discover and rank Polymarket trading opportunities.

Usage::

    # Scan all categories (one-shot)
    python scripts/market_scanner.py

    # Filter to economic markets only
    python scripts/market_scanner.py --category ECONOMIC

    # Custom config file
    python scripts/market_scanner.py --config config/settings.yaml

    # Show top N markets
    python scripts/market_scanner.py --top 20

    # JSON output
    python scripts/market_scanner.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal

from src.core.config import load_settings
from src.core.logging import setup_logging
from src.core.types import MarketCategory, ScanFilter
from src.polymarket.client import PolymarketClient
from src.polymarket.scanner import MarketScanner


def _decimal_default(obj: object) -> str:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _render_table(opportunities: list[dict[str, object]], top: int) -> str:
    """Render opportunities as an ASCII table."""
    lines: list[str] = []
    header = (
        f"{'#':>3}  {'Score':>5}  {'Category':<10}  {'Bid':>6}  {'Ask':>6}  "
        f"{'Spread':>6}  {'Depth':>8}  Question"
    )
    lines.append(header)
    lines.append("-" * len(header))

    for i, opp in enumerate(opportunities[:top], 1):
        score = f"{opp.get('score', 0):.2f}"
        cat = str(opp.get("category", ""))[:10]
        bid = f"{opp.get('best_bid', 0)}"
        ask = f"{opp.get('best_ask', 0)}"
        spread = f"{opp.get('spread', 0)}"
        depth = f"${opp.get('depth_usd', 0)}"
        question = str(opp.get("question", ""))[:60]
        lines.append(
            f"{i:>3}  {score:>5}  {cat:<10}  {bid:>6}  {ask:>6}  "
            f"{spread:>6}  {depth:>8}  {question}"
        )

    return "\n".join(lines)


async def run_scan(args: argparse.Namespace) -> int:
    """Execute a single scan cycle and display results."""
    settings = load_settings(args.config)
    setup_logging(level="WARNING")

    # Build category filter
    scan_filter = ScanFilter()
    if args.category:
        try:
            cat = MarketCategory(args.category.upper())
            scan_filter = ScanFilter(categories=[cat])
        except ValueError:
            print(f"Unknown category: {args.category}", file=sys.stderr)
            print(f"Valid: {', '.join(c.value for c in MarketCategory)}", file=sys.stderr)
            return 1

    async with PolymarketClient(settings.polymarket) as client:
        scanner = MarketScanner(
            client=client,
            scan_filter=scan_filter,
            config=settings.scanner,
        )
        print("Scanning Polymarket for opportunities...", file=sys.stderr)
        opportunities = await scanner.scan_once()

    if not opportunities:
        print("No opportunities found.", file=sys.stderr)
        return 0

    # Serialize
    opp_dicts = [
        {
            "condition_id": o.condition_id,
            "question": o.question,
            "category": o.category.value,
            "token_id": o.token_id,
            "best_bid": o.best_bid,
            "best_ask": o.best_ask,
            "spread": o.spread,
            "depth_usd": o.depth_usd,
            "score": o.score,
        }
        for o in opportunities
    ]

    if args.json:
        print(json.dumps(opp_dicts, indent=2, default=_decimal_default))
    else:
        print(f"\nFound {len(opportunities)} opportunities:\n")
        print(_render_table(opp_dicts, args.top))
        print(f"\nShowing top {min(args.top, len(opportunities))} of {len(opportunities)}")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan Polymarket for trading opportunities.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to settings YAML (default: config/settings.yaml)",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="Filter by category: ECONOMIC, SPORTS, CRYPTO, POLITICS, OTHER",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        help="Number of results to display (default: 25)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of table",
    )
    args = parser.parse_args()

    code = asyncio.run(run_scan(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
