"""Tests for CryptoFeed — symbol mapping, parsing, validation, WS lifecycle."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.config import CryptoFeedConfig
from src.core.types import (
    CryptoExchange,
    CryptoPair,
    CryptoTicker,
    FeedEventType,
    FeedType,
    OutcomeType,
)
from src.feeds.crypto import (
    CryptoFeed,
    _is_price_validated,
    _pair_to_binance_symbol,
    _pair_to_coinbase_product,
    _pair_to_kraken_symbol,
    _parse_binance_ticker,
    _parse_coinbase_ticker,
    _parse_kraken_ticker,
)

# ── Helpers ─────────────────────────────────────────────────────


def _cfg(**overrides: object) -> CryptoFeedConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "exchanges": ["binance", "coinbase", "kraken"],
        "pairs": ["BTC_USDT", "ETH_USDT"],
        "poll_interval_ms": 50,
        "price_move_threshold_pct": 2.0,
        "cross_validation_threshold_pct": 1.0,
        "binance_ws_url": "wss://test.binance.com/ws",
        "coinbase_ws_url": "wss://test.coinbase.com/ws",
        "kraken_ws_url": "wss://test.kraken.com/v2",
        "reconnect_base_secs": 0.01,
        "reconnect_cap_secs": 0.05,
        "ping_interval_secs": 1.0,
    }
    defaults.update(overrides)
    return CryptoFeedConfig(**defaults)  # type: ignore[arg-type]


# ── Pair symbol mapping ──────────────────────────────────────


class TestPairSymbolMapping:
    def test_binance_btc(self) -> None:
        assert _pair_to_binance_symbol(CryptoPair.BTC_USDT) == "btcusdt"

    def test_binance_eth(self) -> None:
        assert _pair_to_binance_symbol(CryptoPair.ETH_USDT) == "ethusdt"

    def test_coinbase_btc(self) -> None:
        assert _pair_to_coinbase_product(CryptoPair.BTC_USDT) == "BTC-USD"

    def test_coinbase_eth(self) -> None:
        assert _pair_to_coinbase_product(CryptoPair.ETH_USDT) == "ETH-USD"

    def test_kraken_btc(self) -> None:
        assert _pair_to_kraken_symbol(CryptoPair.BTC_USDT) == "BTC/USD"

    def test_kraken_eth(self) -> None:
        assert _pair_to_kraken_symbol(CryptoPair.ETH_USDT) == "ETH/USD"


# ── Ticker parsing ────────────────────────────────────────────


class TestParseBinanceTicker:
    def test_valid_btc(self) -> None:
        data: dict[str, Any] = {"s": "BTCUSDT", "c": "42000.50", "P": "1.5", "E": 1700000000000}
        ticker = _parse_binance_ticker(data)
        assert ticker is not None
        assert ticker.pair == CryptoPair.BTC_USDT
        assert ticker.exchange == CryptoExchange.BINANCE
        assert ticker.price == Decimal("42000.50")
        assert ticker.change_pct == Decimal("1.5")

    def test_unknown_symbol_returns_none(self) -> None:
        data: dict[str, Any] = {"s": "DOGEUSDT", "c": "0.1", "P": "0"}
        assert _parse_binance_ticker(data) is None

    def test_missing_price_returns_none(self) -> None:
        data: dict[str, Any] = {"s": "BTCUSDT", "P": "1.0"}
        assert _parse_binance_ticker(data) is None


class TestParseCoinbaseTicker:
    def test_valid_btc(self) -> None:
        data: dict[str, Any] = {
            "type": "ticker",
            "product_id": "BTC-USD",
            "price": "42000.50",
            "price_percent_chg_24_h": "1.5",
        }
        ticker = _parse_coinbase_ticker(data)
        assert ticker is not None
        assert ticker.pair == CryptoPair.BTC_USDT
        assert ticker.exchange == CryptoExchange.COINBASE
        assert ticker.price == Decimal("42000.50")

    def test_unknown_product_returns_none(self) -> None:
        data: dict[str, Any] = {"type": "ticker", "product_id": "DOGE-USD", "price": "0.1"}
        assert _parse_coinbase_ticker(data) is None

    def test_missing_price_returns_none(self) -> None:
        data: dict[str, Any] = {"type": "ticker", "product_id": "BTC-USD"}
        assert _parse_coinbase_ticker(data) is None


class TestParseKrakenTicker:
    def test_valid_btc(self) -> None:
        data: dict[str, Any] = {
            "channel": "ticker",
            "data": [{"symbol": "BTC/USD", "last": 42000.50, "change_pct": 1.5}],
        }
        ticker = _parse_kraken_ticker(data)
        assert ticker is not None
        assert ticker.pair == CryptoPair.BTC_USDT
        assert ticker.exchange == CryptoExchange.KRAKEN
        assert ticker.price == Decimal("42000.5")

    def test_unknown_symbol_returns_none(self) -> None:
        data: dict[str, Any] = {"channel": "ticker", "data": [{"symbol": "DOGE/USD", "last": 0.1}]}
        assert _parse_kraken_ticker(data) is None

    def test_empty_data_returns_none(self) -> None:
        data: dict[str, Any] = {"channel": "ticker", "data": []}
        assert _parse_kraken_ticker(data) is None

    def test_missing_data_key_returns_none(self) -> None:
        data: dict[str, Any] = {"channel": "ticker"}
        assert _parse_kraken_ticker(data) is None


# ── _is_price_validated ───────────────────────────────────────


class TestIsPriceValidated:
    def test_within_threshold(self) -> None:
        primary = CryptoTicker(
            pair=CryptoPair.BTC_USDT, exchange=CryptoExchange.BINANCE,
            price=Decimal("42000"),
        )
        validator = CryptoTicker(
            pair=CryptoPair.BTC_USDT, exchange=CryptoExchange.COINBASE,
            price=Decimal("42100"),  # ~0.24% diff
        )
        assert _is_price_validated(primary, [validator], 1.0) is True

    def test_outside_threshold(self) -> None:
        primary = CryptoTicker(
            pair=CryptoPair.BTC_USDT, exchange=CryptoExchange.BINANCE,
            price=Decimal("42000"),
        )
        validator = CryptoTicker(
            pair=CryptoPair.BTC_USDT, exchange=CryptoExchange.COINBASE,
            price=Decimal("43000"),  # ~2.38% diff
        )
        assert _is_price_validated(primary, [validator], 1.0) is False

    def test_empty_validators_returns_true(self) -> None:
        primary = CryptoTicker(
            pair=CryptoPair.BTC_USDT, exchange=CryptoExchange.BINANCE,
            price=Decimal("42000"),
        )
        assert _is_price_validated(primary, [], 1.0) is True

    def test_zero_primary_price_returns_false(self) -> None:
        primary = CryptoTicker(
            pair=CryptoPair.BTC_USDT, exchange=CryptoExchange.BINANCE,
            price=Decimal("0"),
        )
        validator = CryptoTicker(
            pair=CryptoPair.BTC_USDT, exchange=CryptoExchange.COINBASE,
            price=Decimal("42000"),
        )
        assert _is_price_validated(primary, [validator], 1.0) is False


# ── CryptoFeed start/stop ────────────────────────────────────


class TestCryptoFeedStartStop:
    async def test_start_creates_ws_tasks(self) -> None:
        feed = CryptoFeed(config=_cfg())
        with patch("src.feeds.crypto.websockets.connect", new_callable=AsyncMock):
            await feed.start()
            # Should have 3 WS tasks (binance, coinbase, kraken)
            assert len(feed._ws_tasks) == 3
            await feed.stop()
            assert len(feed._ws_tasks) == 0

    async def test_stop_cancels_ws_tasks(self) -> None:
        feed = CryptoFeed(config=_cfg())
        with patch("src.feeds.crypto.websockets.connect", new_callable=AsyncMock):
            await feed.start()
            tasks = list(feed._ws_tasks)
            await feed.stop()
            for task in tasks:
                assert task.cancelled() or task.done()

    async def test_context_manager(self) -> None:
        feed = CryptoFeed(config=_cfg())
        with patch("src.feeds.crypto.websockets.connect", new_callable=AsyncMock):
            async with feed:
                assert feed.running
                assert len(feed._ws_tasks) == 3
            assert not feed.running
            assert len(feed._ws_tasks) == 0

    async def test_single_exchange_config(self) -> None:
        feed = CryptoFeed(config=_cfg(exchanges=["binance"]))
        with patch("src.feeds.crypto.websockets.connect", new_callable=AsyncMock):
            await feed.start()
            assert len(feed._ws_tasks) == 1
            await feed.stop()


# ── CryptoFeed poll (cross-validation) ────────────────────────


class TestCryptoFeedPoll:
    async def test_poll_no_data_returns_empty(self) -> None:
        feed = CryptoFeed(config=_cfg())
        events = await feed.poll()
        assert events == []

    async def test_poll_sets_initial_baseline(self) -> None:
        feed = CryptoFeed(config=_cfg())
        feed._tickers[CryptoExchange.BINANCE] = {
            CryptoPair.BTC_USDT: CryptoTicker(
                pair=CryptoPair.BTC_USDT,
                exchange=CryptoExchange.BINANCE,
                price=Decimal("42000"),
            ),
        }
        events = await feed.poll()
        assert events == []
        assert feed._baseline_prices[CryptoPair.BTC_USDT] == Decimal("42000")

    async def test_poll_emits_on_significant_move(self) -> None:
        feed = CryptoFeed(config=_cfg(price_move_threshold_pct=2.0))
        # Set baseline
        feed._baseline_prices[CryptoPair.BTC_USDT] = Decimal("40000")
        # Set current price: 3% move
        feed._tickers[CryptoExchange.BINANCE] = {
            CryptoPair.BTC_USDT: CryptoTicker(
                pair=CryptoPair.BTC_USDT,
                exchange=CryptoExchange.BINANCE,
                price=Decimal("41200"),
            ),
        }
        events = await feed.poll()
        assert len(events) == 1
        assert events[0].feed_type == FeedType.CRYPTO
        assert events[0].event_type == FeedEventType.DATA_RELEASED
        assert events[0].indicator == "BTC_USDT_PRICE"
        assert events[0].outcome_type == OutcomeType.NUMERIC

    async def test_poll_no_event_below_threshold(self) -> None:
        feed = CryptoFeed(config=_cfg(price_move_threshold_pct=5.0))
        feed._baseline_prices[CryptoPair.BTC_USDT] = Decimal("40000")
        feed._tickers[CryptoExchange.BINANCE] = {
            CryptoPair.BTC_USDT: CryptoTicker(
                pair=CryptoPair.BTC_USDT,
                exchange=CryptoExchange.BINANCE,
                price=Decimal("40500"),  # 1.25% move, below 5% threshold
            ),
        }
        events = await feed.poll()
        assert events == []

    async def test_poll_event_metadata_has_validated_flag(self) -> None:
        feed = CryptoFeed(config=_cfg(price_move_threshold_pct=2.0))
        feed._baseline_prices[CryptoPair.BTC_USDT] = Decimal("40000")
        feed._tickers[CryptoExchange.BINANCE] = {
            CryptoPair.BTC_USDT: CryptoTicker(
                pair=CryptoPair.BTC_USDT,
                exchange=CryptoExchange.BINANCE,
                price=Decimal("41200"),
            ),
        }
        feed._tickers[CryptoExchange.COINBASE] = {
            CryptoPair.BTC_USDT: CryptoTicker(
                pair=CryptoPair.BTC_USDT,
                exchange=CryptoExchange.COINBASE,
                price=Decimal("41180"),  # close to Binance
            ),
        }
        events = await feed.poll()
        assert len(events) == 1
        assert events[0].metadata["validated"] is True


# ── CryptoFeed ticker state ──────────────────────────────────


class TestCryptoFeedTickerState:
    def test_update_ticker(self) -> None:
        feed = CryptoFeed(config=_cfg())
        ticker = CryptoTicker(
            pair=CryptoPair.BTC_USDT,
            exchange=CryptoExchange.BINANCE,
            price=Decimal("42000"),
        )
        feed._update_ticker(ticker)
        assert feed.get_ticker(CryptoExchange.BINANCE, CryptoPair.BTC_USDT) == ticker

    def test_get_ticker_returns_none_for_missing(self) -> None:
        feed = CryptoFeed(config=_cfg())
        assert feed.get_ticker(CryptoExchange.BINANCE, CryptoPair.BTC_USDT) is None

    def test_tickers_property_returns_copy(self) -> None:
        feed = CryptoFeed(config=_cfg())
        ticker = CryptoTicker(
            pair=CryptoPair.BTC_USDT,
            exchange=CryptoExchange.BINANCE,
            price=Decimal("42000"),
        )
        feed._update_ticker(ticker)
        tickers = feed.tickers
        # Mutating the copy should not affect the feed
        tickers[CryptoExchange.BINANCE].pop(CryptoPair.BTC_USDT)
        assert feed.get_ticker(CryptoExchange.BINANCE, CryptoPair.BTC_USDT) is not None


# ── CryptoFeed WS handlers ───────────────────────────────────


class _AsyncIterator:
    """Async iterator over a list of messages."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self._index = 0

    def __aiter__(self) -> _AsyncIterator:
        return self

    async def __anext__(self) -> str:
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        msg = self._messages[self._index]
        self._index += 1
        return msg


def _make_mock_ws(messages: list[str]) -> MagicMock:
    """Create a mock websocket that yields messages then stops."""
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.ping = AsyncMock()
    ws.close = AsyncMock()
    aiter_obj = _AsyncIterator(messages)
    ws.__aiter__ = MagicMock(return_value=aiter_obj)
    return ws


def _patch_ws_connect(mock_ws: MagicMock) -> AsyncMock:
    """Create an AsyncMock for websockets.connect that returns mock_ws."""
    connect_mock = AsyncMock(return_value=mock_ws)
    return connect_mock


class TestCryptoFeedWSHandlers:
    async def test_binance_session_updates_ticker(self) -> None:
        import json
        msg = json.dumps({"s": "BTCUSDT", "c": "42000", "P": "1.0", "E": 1700000000000})
        mock_ws = _make_mock_ws([msg])
        feed = CryptoFeed(config=_cfg(price_move_threshold_pct=99.0))
        feed._running = True

        with patch("src.feeds.crypto.websockets.connect", _patch_ws_connect(mock_ws)):
            await feed._binance_session()

        ticker = feed.get_ticker(CryptoExchange.BINANCE, CryptoPair.BTC_USDT)
        assert ticker is not None
        assert ticker.price == Decimal("42000")

    async def test_coinbase_session_updates_ticker(self) -> None:
        import json
        msg = json.dumps({
            "type": "ticker",
            "product_id": "BTC-USD",
            "price": "42500",
        })
        mock_ws = _make_mock_ws([msg])
        feed = CryptoFeed(config=_cfg())
        feed._running = True

        with patch("src.feeds.crypto.websockets.connect", _patch_ws_connect(mock_ws)):
            await feed._coinbase_session()

        ticker = feed.get_ticker(CryptoExchange.COINBASE, CryptoPair.BTC_USDT)
        assert ticker is not None
        assert ticker.price == Decimal("42500")

    async def test_kraken_session_updates_ticker(self) -> None:
        import json
        msg = json.dumps({
            "channel": "ticker",
            "data": [{"symbol": "BTC/USD", "last": 42800}],
        })
        mock_ws = _make_mock_ws([msg])
        feed = CryptoFeed(config=_cfg())
        feed._running = True

        with patch("src.feeds.crypto.websockets.connect", _patch_ws_connect(mock_ws)):
            await feed._kraken_session()

        ticker = feed.get_ticker(CryptoExchange.KRAKEN, CryptoPair.BTC_USDT)
        assert ticker is not None
        assert ticker.price == Decimal("42800")

    async def test_binance_reconnects_on_failure(self) -> None:
        """Verify the reconnect loop retries after WS connect failure."""
        feed = CryptoFeed(config=_cfg())
        feed._running = True
        call_count = 0

        async def failing_connect(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                feed._running = False
            raise ConnectionError("connection failed")

        with patch("src.feeds.crypto.websockets.connect", side_effect=failing_connect):
            await feed._run_binance_ws()

        assert call_count >= 2
