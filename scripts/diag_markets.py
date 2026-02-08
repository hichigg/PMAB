#!/usr/bin/env python3
"""Quick diagnostic: check how many sports markets have active orderbooks."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.config import load_settings
from src.core.types import MarketCategory
from src.polymarket.client import PolymarketClient
from src.polymarket.scanner import _classify_market


async def main():
    settings = load_settings("config/settings.yaml")
    pm = settings.polymarket
    client = PolymarketClient(
        host=pm.host,
        api_key=pm.api_key,
        api_secret=pm.api_secret.get_secret_value(),
        api_passphrase=pm.api_passphrase.get_secret_value(),
    )
    await client.connect()

    markets = await client.get_all_markets()
    sports = [
        m
        for m in markets
        if _classify_market(m) == MarketCategory.SPORTS
        and m.active
        and not m.closed
        and m.tokens
    ]

    print(f"Active sports markets with tokens: {len(sports)}")

    has_book = 0
    empty = 0

    for m in sports[:30]:
        tid = m.tokens[0].get("token_id", "")
        if not tid:
            continue
        try:
            book = await client.get_orderbook(tid)
            if book.bids or book.asks:
                has_book += 1
                spread = f"{float(book.spread):.3f}" if book.spread else "?"
                print(
                    f"  BOOK: {m.question[:60]:60s} "
                    f"depth=${float(book.depth_usd):.0f} spread={spread}"
                )
            else:
                empty += 1
        except Exception:
            empty += 1

    print(f"\nOf first 30: {has_book} with orderbook, {empty} empty/error")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
