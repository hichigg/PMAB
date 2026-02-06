"""Market scanner — discovers, filters, scores, and tracks trading opportunities."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from src.core.config import ScannerConfig, get_settings
from src.core.types import (
    LiquidityScreen,
    MarketCategory,
    MarketInfo,
    MarketOpportunity,
    OrderBook,
    ScanEvent,
    ScanEventType,
    ScanFilter,
)
from src.polymarket.client import PolymarketClient

logger = structlog.stdlib.get_logger()

# Type alias for scan event callbacks
ScanEventCallback = Callable[[ScanEvent], Awaitable[None] | None]

# Tag → category mapping (lowercase)
_TAG_CATEGORY_MAP: dict[str, MarketCategory] = {
    "economics": MarketCategory.ECONOMIC,
    "economy": MarketCategory.ECONOMIC,
    "fed": MarketCategory.ECONOMIC,
    "inflation": MarketCategory.ECONOMIC,
    "cpi": MarketCategory.ECONOMIC,
    "gdp": MarketCategory.ECONOMIC,
    "jobs": MarketCategory.ECONOMIC,
    "unemployment": MarketCategory.ECONOMIC,
    "interest-rates": MarketCategory.ECONOMIC,
    "sports": MarketCategory.SPORTS,
    "nfl": MarketCategory.SPORTS,
    "nba": MarketCategory.SPORTS,
    "mlb": MarketCategory.SPORTS,
    "soccer": MarketCategory.SPORTS,
    "football": MarketCategory.SPORTS,
    "baseball": MarketCategory.SPORTS,
    "basketball": MarketCategory.SPORTS,
    "hockey": MarketCategory.SPORTS,
    "mma": MarketCategory.SPORTS,
    "ufc": MarketCategory.SPORTS,
    "tennis": MarketCategory.SPORTS,
    "crypto": MarketCategory.CRYPTO,
    "bitcoin": MarketCategory.CRYPTO,
    "ethereum": MarketCategory.CRYPTO,
    "defi": MarketCategory.CRYPTO,
    "politics": MarketCategory.POLITICS,
    "elections": MarketCategory.POLITICS,
    "congress": MarketCategory.POLITICS,
    "president": MarketCategory.POLITICS,
    "senate": MarketCategory.POLITICS,
}

# Question keyword → category fallback (lowercase substrings)
_QUESTION_CATEGORY_HINTS: list[tuple[str, MarketCategory]] = [
    ("inflation", MarketCategory.ECONOMIC),
    ("cpi", MarketCategory.ECONOMIC),
    ("gdp", MarketCategory.ECONOMIC),
    ("fed ", MarketCategory.ECONOMIC),
    ("federal reserve", MarketCategory.ECONOMIC),
    ("interest rate", MarketCategory.ECONOMIC),
    ("unemployment", MarketCategory.ECONOMIC),
    ("nonfarm", MarketCategory.ECONOMIC),
    ("payroll", MarketCategory.ECONOMIC),
    ("super bowl", MarketCategory.SPORTS),
    ("world series", MarketCategory.SPORTS),
    ("championship", MarketCategory.SPORTS),
    ("playoff", MarketCategory.SPORTS),
    ("bitcoin", MarketCategory.CRYPTO),
    ("btc", MarketCategory.CRYPTO),
    ("ethereum", MarketCategory.CRYPTO),
    ("eth ", MarketCategory.CRYPTO),
    ("crypto", MarketCategory.CRYPTO),
    ("election", MarketCategory.POLITICS),
    ("president", MarketCategory.POLITICS),
    ("congress", MarketCategory.POLITICS),
    ("senate", MarketCategory.POLITICS),
    ("governor", MarketCategory.POLITICS),
    ("vote", MarketCategory.POLITICS),
]


def _classify_market(market: MarketInfo) -> MarketCategory:
    """Classify a market into a category using tags then question text."""
    # Try tags first (highest priority)
    for tag in market.tags:
        tag_lower = tag.lower()
        if tag_lower in _TAG_CATEGORY_MAP:
            return _TAG_CATEGORY_MAP[tag_lower]

    # Fall back to question text keyword matching
    question_lower = market.question.lower()
    for hint, category in _QUESTION_CATEGORY_HINTS:
        if hint in question_lower:
            return category

    return MarketCategory.OTHER


def _hours_until_expiry(market: MarketInfo) -> float | None:
    """Calculate hours until market expiry, or None if no end date."""
    if not market.end_date_iso:
        return None
    try:
        end_dt = datetime.fromisoformat(market.end_date_iso.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        delta = end_dt - now
        return delta.total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def _passes_filter(market: MarketInfo, scan_filter: ScanFilter) -> bool:
    """Check if a market passes all filter criteria (AND logic)."""
    if scan_filter.require_active and not market.active:
        return False

    if scan_filter.exclude_closed and market.closed:
        return False

    if scan_filter.categories:
        category = _classify_market(market)
        if category not in scan_filter.categories:
            return False

    if scan_filter.tag_allowlist:
        market_tags_lower = {t.lower() for t in market.tags}
        allowlist_lower = {t.lower() for t in scan_filter.tag_allowlist}
        if not market_tags_lower & allowlist_lower:
            return False

    if scan_filter.tag_blocklist:
        market_tags_lower = {t.lower() for t in market.tags}
        blocklist_lower = {t.lower() for t in scan_filter.tag_blocklist}
        if market_tags_lower & blocklist_lower:
            return False

    if scan_filter.question_patterns:
        compiled = scan_filter.compiled_patterns()
        if not any(p.search(market.question) for p in compiled):
            return False

    hours = _hours_until_expiry(market)
    if scan_filter.min_hours_to_expiry is not None:
        if hours is None or hours < scan_filter.min_hours_to_expiry:
            return False

    if scan_filter.max_hours_to_expiry is not None:
        if hours is not None and hours > scan_filter.max_hours_to_expiry:
            return False

    return True


def _passes_liquidity(book: OrderBook, screen: LiquidityScreen) -> bool:
    """Check if an order book meets liquidity thresholds."""
    if book.depth_usd < screen.min_depth_usd:
        return False

    if book.spread is not None and book.spread > screen.max_spread:
        return False

    # Per-side depth checks
    bid_depth = sum((lvl.price * lvl.size for lvl in book.bids), Decimal(0))
    if bid_depth < screen.min_bid_depth_usd:
        return False

    ask_depth = sum((lvl.price * lvl.size for lvl in book.asks), Decimal(0))
    if ask_depth < screen.min_ask_depth_usd:
        return False

    return True


def _score_opportunity(
    book: OrderBook,
    hours_to_expiry: float | None,
    weights: dict[str, float],
) -> float:
    """Score an opportunity based on depth, spread, and time-to-expiry.

    Each component is normalized to [0, 1] then combined with weights.
    """
    w_depth = weights.get("depth", 0.4)
    w_spread = weights.get("spread", 0.4)
    w_recency = weights.get("recency", 0.2)

    # Depth score: log-scale, capped at $10k
    depth_val = float(book.depth_usd)
    depth_score = min(depth_val / 10000.0, 1.0)

    # Spread score: tighter is better (inverted, capped)
    spread_val = float(book.spread) if book.spread is not None else 1.0
    spread_score = max(1.0 - spread_val * 10.0, 0.0)  # 0.10 → 0.0, 0.01 → 0.9

    # Recency score: closer expiry = more urgent = higher score
    if hours_to_expiry is not None and hours_to_expiry > 0:
        # Markets expiring within 24h score highest
        recency_score = max(1.0 - hours_to_expiry / 168.0, 0.0)  # 168h = 1 week
    else:
        recency_score = 0.5  # neutral if no expiry

    return w_depth * depth_score + w_spread * spread_score + w_recency * recency_score


class MarketScanner:
    """Discovers, filters, scores, and tracks market opportunities.

    Usage::

        scanner = MarketScanner(client, scan_filter, liquidity_screen)
        scanner.on_event(my_callback)
        await scanner.start()
        # ... scanner runs in background ...
        await scanner.stop()
    """

    def __init__(
        self,
        client: PolymarketClient,
        scan_filter: ScanFilter | None = None,
        liquidity_screen: LiquidityScreen | None = None,
        config: ScannerConfig | None = None,
    ) -> None:
        self._client = client
        self._filter = scan_filter or ScanFilter()
        self._screen = liquidity_screen or LiquidityScreen()
        self._config = config or get_settings().scanner

        self._opportunities: dict[str, MarketOpportunity] = {}
        self._callbacks: list[ScanEventCallback] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False
        # Map token_id → condition_id for WS callback lookups
        self._token_to_condition: dict[str, str] = {}

    @property
    def opportunities(self) -> dict[str, MarketOpportunity]:
        """Currently tracked opportunities, keyed by condition_id."""
        return dict(self._opportunities)

    @property
    def tracked_count(self) -> int:
        """Number of currently tracked opportunities."""
        return len(self._opportunities)

    def on_event(self, callback: ScanEventCallback) -> None:
        """Register a callback for scan events."""
        self._callbacks.append(callback)

    async def _emit(self, event: ScanEvent) -> None:
        """Dispatch a scan event to all registered callbacks."""
        for cb in self._callbacks:
            try:
                result = cb(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("scan_event_callback_error", event_type=event.event_type)

    async def start(self) -> None:
        """Start the background scan loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._scan_loop())
        logger.info("scanner_started", interval=self._config.scan_interval_secs)

    async def stop(self) -> None:
        """Stop the scanner and clean up WS subscriptions."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Unsubscribe all tracked tokens
        for token_id in list(self._token_to_condition.keys()):
            try:
                await self._client.unsubscribe_orderbook(token_id)
            except Exception:
                pass
        self._token_to_condition.clear()
        logger.info("scanner_stopped")

    async def _scan_loop(self) -> None:
        """Background loop that calls scan_once() on an interval."""
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("scan_loop_error")

            try:
                await asyncio.sleep(self._config.scan_interval_secs)
            except asyncio.CancelledError:
                break

    async def scan_once(self) -> list[MarketOpportunity]:
        """Run a single scan cycle: fetch, filter, score, reconcile.

        Returns the list of current opportunities, sorted by score descending.
        """
        now = time.time()

        # 1. Fetch all markets
        try:
            all_markets = await self._client.get_all_markets()
        except Exception:
            logger.exception("scan_fetch_markets_error")
            return list(self._opportunities.values())

        # 2. Filter by ScanFilter criteria
        filtered = [m for m in all_markets if _passes_filter(m, self._filter)]

        # 3. Build token_id → market mapping (use first token per market)
        token_market_map: dict[str, MarketInfo] = {}
        for market in filtered:
            if market.tokens:
                # Pick the first token as representative
                token_id = market.tokens[0].get("token_id", "")
                if token_id:
                    token_market_map[token_id] = market

        # 4. Fetch order books in batches
        token_ids = list(token_market_map.keys())
        books: dict[str, OrderBook] = {}
        batch_size = self._config.orderbook_batch_size

        for i in range(0, len(token_ids), batch_size):
            batch = token_ids[i : i + batch_size]
            try:
                batch_books = await self._client.get_orderbooks(batch)
                for book in batch_books:
                    books[book.token_id] = book
            except Exception:
                logger.exception("scan_fetch_books_error", batch_start=i)

        # 5. Screen for liquidity, score, and build opportunities
        new_opps: dict[str, MarketOpportunity] = {}
        for token_id, market in token_market_map.items():
            market_book = books.get(token_id)
            if market_book is None:
                continue

            if not _passes_liquidity(market_book, self._screen):
                continue

            hours = _hours_until_expiry(market)
            score = _score_opportunity(market_book, hours, self._config.score_weights)
            category = _classify_market(market)

            opp = MarketOpportunity(
                condition_id=market.condition_id,
                question=market.question,
                category=category,
                tokens=market.tokens,
                token_id=token_id,
                best_bid=market_book.best_bid,
                best_ask=market_book.best_ask,
                spread=market_book.spread,
                depth_usd=market_book.depth_usd,
                score=score,
                first_seen=now,
                last_updated=now,
                market_info=market,
            )
            new_opps[market.condition_id] = opp

        # 6. Sort by score and cap to max_tracked_markets
        sorted_opps = sorted(new_opps.values(), key=lambda o: o.score, reverse=True)
        capped: dict[str, MarketOpportunity] = {}
        for opp in sorted_opps[: self._config.max_tracked_markets]:
            capped[opp.condition_id] = opp

        # 7. Reconcile with previous state → emit events
        await self._reconcile(capped, now)

        return sorted(self._opportunities.values(), key=lambda o: o.score, reverse=True)

    async def _reconcile(
        self,
        new_opps: dict[str, MarketOpportunity],
        now: float,
    ) -> None:
        """Compare new opportunities with existing, emit events, manage WS."""
        old_ids = set(self._opportunities.keys())
        new_ids = set(new_opps.keys())

        # New opportunities
        for cid in new_ids - old_ids:
            opp = new_opps[cid]
            self._opportunities[cid] = opp
            await self._emit(ScanEvent(
                event_type=ScanEventType.OPPORTUNITY_FOUND,
                opportunity=opp,
                timestamp=now,
            ))
            # Subscribe to WS for this token
            if opp.token_id:
                self._token_to_condition[opp.token_id] = cid
                try:
                    await self._client.subscribe_orderbook(
                        opp.token_id, self._on_book_update
                    )
                except Exception:
                    logger.exception("scan_ws_subscribe_error", token_id=opp.token_id)

        # Updated opportunities (still present)
        for cid in new_ids & old_ids:
            opp = new_opps[cid]
            # Preserve first_seen from original
            opp.first_seen = self._opportunities[cid].first_seen
            self._opportunities[cid] = opp
            await self._emit(ScanEvent(
                event_type=ScanEventType.OPPORTUNITY_UPDATED,
                opportunity=opp,
                timestamp=now,
            ))

        # Lost opportunities
        for cid in old_ids - new_ids:
            lost_opp = self._opportunities.pop(cid)
            await self._emit(ScanEvent(
                event_type=ScanEventType.OPPORTUNITY_LOST,
                opportunity=lost_opp,
                timestamp=now,
            ))
            # Unsubscribe WS
            if lost_opp.token_id:
                self._token_to_condition.pop(lost_opp.token_id, None)
                try:
                    await self._client.unsubscribe_orderbook(lost_opp.token_id)
                except Exception:
                    logger.exception(
                        "scan_ws_unsubscribe_error", token_id=lost_opp.token_id
                    )

    async def _on_book_update(self, book: OrderBook) -> None:
        """Handle real-time book updates from WebSocket.

        If a tracked market's liquidity drops below the screen thresholds,
        emit an OPPORTUNITY_LOST event immediately (don't wait for next scan).
        If still passing, update the opportunity in place.
        """
        cid = self._token_to_condition.get(book.token_id)
        if cid is None or cid not in self._opportunities:
            return

        now = time.time()
        opp = self._opportunities[cid]

        if not _passes_liquidity(book, self._screen):
            # Liquidity degraded — remove immediately
            self._opportunities.pop(cid, None)
            self._token_to_condition.pop(book.token_id, None)
            await self._emit(ScanEvent(
                event_type=ScanEventType.OPPORTUNITY_LOST,
                opportunity=opp,
                timestamp=now,
            ))
            try:
                await self._client.unsubscribe_orderbook(book.token_id)
            except Exception:
                pass
        else:
            # Update in place
            opp.best_bid = book.best_bid
            opp.best_ask = book.best_ask
            opp.spread = book.spread
            opp.depth_usd = book.depth_usd
            opp.last_updated = now
            hours = _hours_until_expiry(opp.market_info) if opp.market_info else None
            opp.score = _score_opportunity(book, hours, self._config.score_weights)
            await self._emit(ScanEvent(
                event_type=ScanEventType.OPPORTUNITY_UPDATED,
                opportunity=opp,
                timestamp=now,
            ))
