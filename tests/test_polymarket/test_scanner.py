"""Tests for the market scanner.

Tests mock at the PolymarketClient interface level to verify:
- Market classification heuristics
- Filter criteria (AND logic)
- Liquidity screening
- Opportunity scoring
- Full scan_once flow, event reconciliation, WS management
"""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.core.config import ScannerConfig, reset_settings
from src.core.types import (
    LiquidityScreen,
    MarketCategory,
    MarketInfo,
    OrderBook,
    PriceLevel,
    ScanEvent,
    ScanEventType,
    ScanFilter,
)
from src.polymarket.scanner import (
    MarketScanner,
    _classify_market,
    _hours_until_expiry,
    _passes_filter,
    _passes_liquidity,
    _score_opportunity,
)


@pytest.fixture(autouse=True)
def _clean_settings() -> None:
    reset_settings()


# ── Helpers ──────────────────────────────────────────────────────


def _make_market(
    condition_id: str = "0xabc",
    question: str = "Will something happen?",
    tags: list[str] | None = None,
    tokens: list[dict[str, str]] | None = None,
    active: bool = True,
    closed: bool = False,
    end_date_iso: str = "",
) -> MarketInfo:
    return MarketInfo(
        condition_id=condition_id,
        question=question,
        tags=tags or [],
        tokens=tokens or [{"token_id": "tok1"}],
        active=active,
        closed=closed,
        end_date_iso=end_date_iso,
    )


def _make_book(
    token_id: str = "tok1",
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
) -> OrderBook:
    bid_levels = [
        PriceLevel(price=Decimal(p), size=Decimal(s)) for p, s in (bids or [])
    ]
    ask_levels = [
        PriceLevel(price=Decimal(p), size=Decimal(s)) for p, s in (asks or [])
    ]
    return OrderBook(
        token_id=token_id,
        bids=bid_levels,
        asks=ask_levels,
        timestamp=time.time(),
    )


def _make_mock_client(
    markets: list[MarketInfo] | None = None,
    books: dict[str, OrderBook] | None = None,
) -> AsyncMock:
    client = AsyncMock()
    client.get_all_markets = AsyncMock(return_value=markets or [])

    async def mock_get_orderbooks(token_ids: list[str]) -> list[OrderBook]:
        if books is None:
            return []
        return [books[tid] for tid in token_ids if tid in books]

    client.get_orderbooks = AsyncMock(side_effect=mock_get_orderbooks)
    client.subscribe_orderbook = AsyncMock()
    client.unsubscribe_orderbook = AsyncMock()
    return client


# ── TestClassifyMarket ───────────────────────────────────────────


class TestClassifyMarket:
    """Test market classification heuristic."""

    def test_classify_by_economics_tag(self) -> None:
        market = _make_market(tags=["Economics"])
        assert _classify_market(market) == MarketCategory.ECONOMIC

    def test_classify_by_sports_tag(self) -> None:
        market = _make_market(tags=["NFL"])
        assert _classify_market(market) == MarketCategory.SPORTS

    def test_classify_by_crypto_tag(self) -> None:
        market = _make_market(tags=["Bitcoin"])
        assert _classify_market(market) == MarketCategory.CRYPTO

    def test_classify_by_politics_tag(self) -> None:
        market = _make_market(tags=["Elections"])
        assert _classify_market(market) == MarketCategory.POLITICS

    def test_classify_by_question_fallback(self) -> None:
        market = _make_market(question="Will the CPI exceed 3%?")
        assert _classify_market(market) == MarketCategory.ECONOMIC

    def test_classify_unknown_returns_other(self) -> None:
        market = _make_market(question="Will aliens land?", tags=["weird"])
        assert _classify_market(market) == MarketCategory.OTHER


# ── TestPassesFilter ─────────────────────────────────────────────


class TestPassesFilter:
    """Test market filtering with AND logic."""

    def test_empty_filter_passes_all(self) -> None:
        market = _make_market()
        f = ScanFilter(require_active=False, exclude_closed=False)
        assert _passes_filter(market, f) is True

    def test_require_active_blocks_inactive(self) -> None:
        market = _make_market(active=False)
        f = ScanFilter(require_active=True)
        assert _passes_filter(market, f) is False

    def test_exclude_closed_blocks_closed(self) -> None:
        market = _make_market(closed=True)
        f = ScanFilter(exclude_closed=True)
        assert _passes_filter(market, f) is False

    def test_category_filter_matches(self) -> None:
        market = _make_market(tags=["Economics"])
        f = ScanFilter(categories=[MarketCategory.ECONOMIC])
        assert _passes_filter(market, f) is True

    def test_category_filter_rejects(self) -> None:
        market = _make_market(tags=["Economics"])
        f = ScanFilter(categories=[MarketCategory.SPORTS])
        assert _passes_filter(market, f) is False

    def test_tag_allowlist_matches(self) -> None:
        market = _make_market(tags=["Fed", "Economics"])
        f = ScanFilter(tag_allowlist=["Fed"])
        assert _passes_filter(market, f) is True

    def test_tag_allowlist_rejects(self) -> None:
        market = _make_market(tags=["Crypto"])
        f = ScanFilter(tag_allowlist=["Fed"])
        assert _passes_filter(market, f) is False

    def test_tag_blocklist_rejects(self) -> None:
        market = _make_market(tags=["Scam"])
        f = ScanFilter(tag_blocklist=["Scam"])
        assert _passes_filter(market, f) is False

    def test_tag_blocklist_allows_non_matching(self) -> None:
        market = _make_market(tags=["Good"])
        f = ScanFilter(tag_blocklist=["Scam"])
        assert _passes_filter(market, f) is True

    def test_question_pattern_matches(self) -> None:
        market = _make_market(question="Will CPI exceed 3%?")
        f = ScanFilter(question_patterns=[r"CPI.*\d+%"])
        assert _passes_filter(market, f) is True

    def test_question_pattern_rejects(self) -> None:
        market = _make_market(question="Will aliens land?")
        f = ScanFilter(question_patterns=[r"CPI.*\d+%"])
        assert _passes_filter(market, f) is False

    def test_combined_filter_all_must_pass(self) -> None:
        market = _make_market(
            tags=["Economics"],
            question="Will CPI exceed 3%?",
            active=True,
        )
        f = ScanFilter(
            categories=[MarketCategory.ECONOMIC],
            tag_allowlist=["Economics"],
            question_patterns=[r"CPI"],
            require_active=True,
        )
        assert _passes_filter(market, f) is True


# ── TestPassesLiquidity ──────────────────────────────────────────


class TestPassesLiquidity:
    """Test liquidity screening."""

    def test_sufficient_depth_passes(self) -> None:
        book = _make_book(
            bids=[("0.50", "200")],
            asks=[("0.55", "200")],
        )
        screen = LiquidityScreen(min_depth_usd=Decimal("100"))
        assert _passes_liquidity(book, screen) is True

    def test_insufficient_depth_fails(self) -> None:
        book = _make_book(
            bids=[("0.50", "10")],
            asks=[("0.55", "10")],
        )
        screen = LiquidityScreen(min_depth_usd=Decimal("500"))
        assert _passes_liquidity(book, screen) is False

    def test_spread_too_wide_fails(self) -> None:
        book = _make_book(
            bids=[("0.30", "1000")],
            asks=[("0.70", "1000")],
        )
        screen = LiquidityScreen(
            min_depth_usd=Decimal("0"),
            max_spread=Decimal("0.10"),
        )
        assert _passes_liquidity(book, screen) is False

    def test_tight_spread_passes(self) -> None:
        book = _make_book(
            bids=[("0.50", "500")],
            asks=[("0.52", "500")],
        )
        screen = LiquidityScreen(
            min_depth_usd=Decimal("0"),
            max_spread=Decimal("0.05"),
        )
        assert _passes_liquidity(book, screen) is True

    def test_per_side_depth_check(self) -> None:
        book = _make_book(
            bids=[("0.50", "10")],  # bid_depth = 5
            asks=[("0.55", "500")],  # ask_depth = 275
        )
        screen = LiquidityScreen(
            min_depth_usd=Decimal("0"),
            min_bid_depth_usd=Decimal("100"),
        )
        assert _passes_liquidity(book, screen) is False


# ── TestScoreOpportunity ─────────────────────────────────────────


class TestScoreOpportunity:
    """Test opportunity scoring."""

    def test_high_depth_high_score(self) -> None:
        book = _make_book(
            bids=[("0.50", "10000")],
            asks=[("0.51", "10000")],
        )
        weights = {"depth": 1.0, "spread": 0.0, "recency": 0.0}
        score = _score_opportunity(book, None, weights)
        assert score > 0.5

    def test_tight_spread_high_score(self) -> None:
        book = _make_book(
            bids=[("0.50", "100")],
            asks=[("0.51", "100")],
        )
        weights = {"depth": 0.0, "spread": 1.0, "recency": 0.0}
        score = _score_opportunity(book, None, weights)
        assert score > 0.8  # 0.01 spread → score ~0.9

    def test_wide_spread_low_score(self) -> None:
        book = _make_book(
            bids=[("0.30", "100")],
            asks=[("0.70", "100")],
        )
        weights = {"depth": 0.0, "spread": 1.0, "recency": 0.0}
        score = _score_opportunity(book, None, weights)
        assert score == 0.0  # 0.40 spread → score 0

    def test_close_expiry_high_recency(self) -> None:
        book = _make_book(
            bids=[("0.50", "100")],
            asks=[("0.55", "100")],
        )
        weights = {"depth": 0.0, "spread": 0.0, "recency": 1.0}
        # 12 hours to expiry → high recency
        score = _score_opportunity(book, 12.0, weights)
        assert score > 0.8


# ── TestMarketScanner ────────────────────────────────────────────


class TestMarketScanner:
    """Test the MarketScanner class: scan_once, reconcile, WS, lifecycle."""

    @pytest.fixture()
    def scanner_config(self) -> ScannerConfig:
        return ScannerConfig(
            scan_interval_secs=1,
            max_tracked_markets=5,
            orderbook_batch_size=2,
            score_weights={"depth": 0.4, "spread": 0.4, "recency": 0.2},
        )

    @pytest.fixture()
    def scan_filter(self) -> ScanFilter:
        return ScanFilter(require_active=True, exclude_closed=True)

    @pytest.fixture()
    def liquidity_screen(self) -> LiquidityScreen:
        return LiquidityScreen(
            min_depth_usd=Decimal("10"),
            max_spread=Decimal("0.20"),
        )

    @pytest.mark.asyncio()
    async def test_scan_once_returns_opportunities(
        self,
        scanner_config: ScannerConfig,
        scan_filter: ScanFilter,
        liquidity_screen: LiquidityScreen,
    ) -> None:
        market = _make_market(
            condition_id="0x1",
            question="Will CPI exceed 3%?",
            tags=["Economics"],
            tokens=[{"token_id": "tok1"}],
        )
        book = _make_book(
            token_id="tok1",
            bids=[("0.50", "200")],
            asks=[("0.55", "200")],
        )
        client = _make_mock_client(markets=[market], books={"tok1": book})

        scanner = MarketScanner(
            client, scan_filter, liquidity_screen, scanner_config
        )
        result = await scanner.scan_once()

        assert len(result) == 1
        assert result[0].condition_id == "0x1"
        assert scanner.tracked_count == 1

    @pytest.mark.asyncio()
    async def test_scan_once_filters_out_inactive(
        self,
        scanner_config: ScannerConfig,
        scan_filter: ScanFilter,
        liquidity_screen: LiquidityScreen,
    ) -> None:
        market = _make_market(active=False)
        client = _make_mock_client(markets=[market])

        scanner = MarketScanner(
            client, scan_filter, liquidity_screen, scanner_config
        )
        result = await scanner.scan_once()

        assert len(result) == 0

    @pytest.mark.asyncio()
    async def test_scan_once_filters_by_liquidity(
        self,
        scanner_config: ScannerConfig,
        scan_filter: ScanFilter,
    ) -> None:
        market = _make_market(tokens=[{"token_id": "tok1"}])
        # Very thin book
        book = _make_book(
            token_id="tok1",
            bids=[("0.50", "1")],
            asks=[("0.55", "1")],
        )
        client = _make_mock_client(markets=[market], books={"tok1": book})

        screen = LiquidityScreen(min_depth_usd=Decimal("1000"))
        scanner = MarketScanner(client, scan_filter, screen, scanner_config)
        result = await scanner.scan_once()

        assert len(result) == 0

    @pytest.mark.asyncio()
    async def test_reconcile_emits_found_event(
        self,
        scanner_config: ScannerConfig,
        scan_filter: ScanFilter,
        liquidity_screen: LiquidityScreen,
    ) -> None:
        market = _make_market(tokens=[{"token_id": "tok1"}])
        book = _make_book(
            token_id="tok1",
            bids=[("0.50", "200")],
            asks=[("0.55", "200")],
        )
        client = _make_mock_client(markets=[market], books={"tok1": book})

        events: list[ScanEvent] = []

        async def capture(event: ScanEvent) -> None:
            events.append(event)

        scanner = MarketScanner(
            client, scan_filter, liquidity_screen, scanner_config
        )
        scanner.on_event(capture)
        await scanner.scan_once()

        found_events = [e for e in events if e.event_type == ScanEventType.OPPORTUNITY_FOUND]
        assert len(found_events) == 1

    @pytest.mark.asyncio()
    async def test_reconcile_emits_updated_event_on_second_scan(
        self,
        scanner_config: ScannerConfig,
        scan_filter: ScanFilter,
        liquidity_screen: LiquidityScreen,
    ) -> None:
        market = _make_market(tokens=[{"token_id": "tok1"}])
        book = _make_book(
            token_id="tok1",
            bids=[("0.50", "200")],
            asks=[("0.55", "200")],
        )
        client = _make_mock_client(markets=[market], books={"tok1": book})

        events: list[ScanEvent] = []

        async def capture(event: ScanEvent) -> None:
            events.append(event)

        scanner = MarketScanner(
            client, scan_filter, liquidity_screen, scanner_config
        )
        scanner.on_event(capture)

        await scanner.scan_once()
        await scanner.scan_once()

        updated_events = [
            e for e in events if e.event_type == ScanEventType.OPPORTUNITY_UPDATED
        ]
        assert len(updated_events) == 1

    @pytest.mark.asyncio()
    async def test_reconcile_emits_lost_event(
        self,
        scanner_config: ScannerConfig,
        scan_filter: ScanFilter,
        liquidity_screen: LiquidityScreen,
    ) -> None:
        market = _make_market(tokens=[{"token_id": "tok1"}])
        book = _make_book(
            token_id="tok1",
            bids=[("0.50", "200")],
            asks=[("0.55", "200")],
        )
        client = _make_mock_client(markets=[market], books={"tok1": book})

        events: list[ScanEvent] = []

        async def capture(event: ScanEvent) -> None:
            events.append(event)

        scanner = MarketScanner(
            client, scan_filter, liquidity_screen, scanner_config
        )
        scanner.on_event(capture)

        # First scan: finds opportunity
        await scanner.scan_once()
        assert scanner.tracked_count == 1

        # Second scan: market is gone
        client.get_all_markets.return_value = []
        await scanner.scan_once()

        lost_events = [e for e in events if e.event_type == ScanEventType.OPPORTUNITY_LOST]
        assert len(lost_events) == 1
        assert scanner.tracked_count == 0

    @pytest.mark.asyncio()
    async def test_ws_subscribe_on_new_opportunity(
        self,
        scanner_config: ScannerConfig,
        scan_filter: ScanFilter,
        liquidity_screen: LiquidityScreen,
    ) -> None:
        market = _make_market(tokens=[{"token_id": "tok1"}])
        book = _make_book(
            token_id="tok1",
            bids=[("0.50", "200")],
            asks=[("0.55", "200")],
        )
        client = _make_mock_client(markets=[market], books={"tok1": book})

        scanner = MarketScanner(
            client, scan_filter, liquidity_screen, scanner_config
        )
        await scanner.scan_once()

        client.subscribe_orderbook.assert_called_once()
        call_args = client.subscribe_orderbook.call_args
        assert call_args[0][0] == "tok1"

    @pytest.mark.asyncio()
    async def test_ws_unsubscribe_on_lost_opportunity(
        self,
        scanner_config: ScannerConfig,
        scan_filter: ScanFilter,
        liquidity_screen: LiquidityScreen,
    ) -> None:
        market = _make_market(tokens=[{"token_id": "tok1"}])
        book = _make_book(
            token_id="tok1",
            bids=[("0.50", "200")],
            asks=[("0.55", "200")],
        )
        client = _make_mock_client(markets=[market], books={"tok1": book})

        scanner = MarketScanner(
            client, scan_filter, liquidity_screen, scanner_config
        )
        await scanner.scan_once()

        # Remove market
        client.get_all_markets.return_value = []
        await scanner.scan_once()

        client.unsubscribe_orderbook.assert_called_with("tok1")

    @pytest.mark.asyncio()
    async def test_ws_callback_removes_degraded_opportunity(
        self,
        scanner_config: ScannerConfig,
        scan_filter: ScanFilter,
        liquidity_screen: LiquidityScreen,
    ) -> None:
        market = _make_market(tokens=[{"token_id": "tok1"}])
        book = _make_book(
            token_id="tok1",
            bids=[("0.50", "200")],
            asks=[("0.55", "200")],
        )
        client = _make_mock_client(markets=[market], books={"tok1": book})

        events: list[ScanEvent] = []

        async def capture(event: ScanEvent) -> None:
            events.append(event)

        scanner = MarketScanner(
            client, scan_filter, liquidity_screen, scanner_config
        )
        scanner.on_event(capture)
        await scanner.scan_once()
        assert scanner.tracked_count == 1

        # Simulate degraded book via WS callback
        degraded_book = _make_book(
            token_id="tok1",
            bids=[("0.50", "1")],
            asks=[("0.55", "1")],
        )
        await scanner._on_book_update(degraded_book)

        assert scanner.tracked_count == 0
        lost_events = [e for e in events if e.event_type == ScanEventType.OPPORTUNITY_LOST]
        assert len(lost_events) == 1

    @pytest.mark.asyncio()
    async def test_ws_callback_updates_opportunity(
        self,
        scanner_config: ScannerConfig,
        scan_filter: ScanFilter,
        liquidity_screen: LiquidityScreen,
    ) -> None:
        market = _make_market(tokens=[{"token_id": "tok1"}])
        book = _make_book(
            token_id="tok1",
            bids=[("0.50", "200")],
            asks=[("0.55", "200")],
        )
        client = _make_mock_client(markets=[market], books={"tok1": book})

        scanner = MarketScanner(
            client, scan_filter, liquidity_screen, scanner_config
        )
        await scanner.scan_once()

        # Updated book with better liquidity
        updated_book = _make_book(
            token_id="tok1",
            bids=[("0.51", "500")],
            asks=[("0.53", "500")],
        )
        await scanner._on_book_update(updated_book)

        opp = scanner.opportunities["0xabc"]
        assert opp.best_bid == Decimal("0.51")
        assert opp.best_ask == Decimal("0.53")

    @pytest.mark.asyncio()
    async def test_max_tracked_markets_cap(
        self,
        scan_filter: ScanFilter,
        liquidity_screen: LiquidityScreen,
    ) -> None:
        config = ScannerConfig(max_tracked_markets=2, orderbook_batch_size=10)
        markets = []
        books = {}
        for i in range(5):
            m = _make_market(
                condition_id=f"0x{i}",
                tokens=[{"token_id": f"tok{i}"}],
            )
            markets.append(m)
            books[f"tok{i}"] = _make_book(
                token_id=f"tok{i}",
                bids=[("0.50", str(100 * (i + 1)))],
                asks=[("0.55", str(100 * (i + 1)))],
            )

        client = _make_mock_client(markets=markets, books=books)
        scanner = MarketScanner(client, scan_filter, liquidity_screen, config)
        await scanner.scan_once()

        assert scanner.tracked_count <= 2

    @pytest.mark.asyncio()
    async def test_scan_once_handles_fetch_error_gracefully(
        self,
        scanner_config: ScannerConfig,
        scan_filter: ScanFilter,
        liquidity_screen: LiquidityScreen,
    ) -> None:
        client = AsyncMock()
        client.get_all_markets = AsyncMock(side_effect=RuntimeError("API down"))

        scanner = MarketScanner(
            client, scan_filter, liquidity_screen, scanner_config
        )
        result = await scanner.scan_once()

        # Should return empty (no prior state) without raising
        assert result == []

    @pytest.mark.asyncio()
    async def test_start_stop_lifecycle(
        self,
        scanner_config: ScannerConfig,
        scan_filter: ScanFilter,
        liquidity_screen: LiquidityScreen,
    ) -> None:
        client = _make_mock_client()

        scanner = MarketScanner(
            client, scan_filter, liquidity_screen, scanner_config
        )
        await scanner.start()
        assert scanner._running is True
        assert scanner._task is not None

        await scanner.stop()
        assert scanner._running is False
        assert scanner._task is None

    @pytest.mark.asyncio()
    async def test_preserves_first_seen_on_update(
        self,
        scanner_config: ScannerConfig,
        scan_filter: ScanFilter,
        liquidity_screen: LiquidityScreen,
    ) -> None:
        market = _make_market(tokens=[{"token_id": "tok1"}])
        book = _make_book(
            token_id="tok1",
            bids=[("0.50", "200")],
            asks=[("0.55", "200")],
        )
        client = _make_mock_client(markets=[market], books={"tok1": book})

        scanner = MarketScanner(
            client, scan_filter, liquidity_screen, scanner_config
        )
        await scanner.scan_once()
        first_seen = scanner.opportunities["0xabc"].first_seen

        await scanner.scan_once()
        assert scanner.opportunities["0xabc"].first_seen == first_seen


# ── TestHoursUntilExpiry ─────────────────────────────────────────


class TestHoursUntilExpiry:
    """Test expiry time calculation."""

    def test_no_end_date_returns_none(self) -> None:
        market = _make_market(end_date_iso="")
        assert _hours_until_expiry(market) is None

    def test_future_date_returns_positive(self) -> None:
        market = _make_market(end_date_iso="2099-01-01T00:00:00Z")
        hours = _hours_until_expiry(market)
        assert hours is not None
        assert hours > 0

    def test_past_date_returns_negative(self) -> None:
        market = _make_market(end_date_iso="2020-01-01T00:00:00Z")
        hours = _hours_until_expiry(market)
        assert hours is not None
        assert hours < 0

    def test_invalid_date_returns_none(self) -> None:
        market = _make_market(end_date_iso="not-a-date")
        assert _hours_until_expiry(market) is None


# ── Directional Depth ─────────────────────────────────────────


class TestDirectionalDepth:
    """Verify bid_depth_usd / ask_depth_usd populated by scan and WS."""

    @pytest.mark.asyncio()
    async def test_scan_once_populates_directional_depth(self) -> None:
        market = _make_market(tokens=[{"token_id": "tok1"}])
        book = _make_book(
            token_id="tok1",
            bids=[("0.50", "200")],  # bid depth = 0.50*200 = 100
            asks=[("0.60", "300")],  # ask depth = 0.60*300 = 180
        )
        client = _make_mock_client(markets=[market], books={"tok1": book})
        scanner = MarketScanner(
            client,
            ScanFilter(),
            LiquidityScreen(min_depth_usd=Decimal("10"), max_spread=Decimal("0.50")),
            ScannerConfig(max_tracked_markets=10, orderbook_batch_size=10),
        )
        result = await scanner.scan_once()
        assert len(result) == 1
        opp = result[0]
        assert opp.bid_depth_usd == Decimal("100")
        assert opp.ask_depth_usd == Decimal("180")

    @pytest.mark.asyncio()
    async def test_ws_update_sets_directional_depth(self) -> None:
        market = _make_market(tokens=[{"token_id": "tok1"}])
        book = _make_book(
            token_id="tok1",
            bids=[("0.50", "200")],
            asks=[("0.55", "200")],
        )
        client = _make_mock_client(markets=[market], books={"tok1": book})
        scanner = MarketScanner(
            client,
            ScanFilter(),
            LiquidityScreen(min_depth_usd=Decimal("10"), max_spread=Decimal("0.50")),
            ScannerConfig(max_tracked_markets=10, orderbook_batch_size=10),
        )
        await scanner.scan_once()

        updated_book = _make_book(
            token_id="tok1",
            bids=[("0.48", "400")],  # bid depth = 0.48*400 = 192
            asks=[("0.52", "500")],  # ask depth = 0.52*500 = 260
        )
        await scanner._on_book_update(updated_book)

        opp = scanner.opportunities["0xabc"]
        assert opp.bid_depth_usd == Decimal("192.0")
        assert opp.ask_depth_usd == Decimal("260.0")
