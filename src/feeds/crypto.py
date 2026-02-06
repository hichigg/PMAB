"""Crypto data feed — Binance WS primary + Coinbase/Kraken WS cross-validation."""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
import websockets

from src.core.config import CryptoFeedConfig, get_settings
from src.core.types import (
    CryptoExchange,
    CryptoPair,
    CryptoTicker,
    FeedEvent,
    FeedEventType,
    FeedType,
    OutcomeType,
)
from src.feeds.base import BaseFeed

logger = structlog.stdlib.get_logger()


# ── Symbol Mapping Helpers ─────────────────────────────────────


def _pair_to_binance_symbol(pair: CryptoPair) -> str:
    """Map a CryptoPair to Binance stream symbol (e.g. BTC_USDT → 'btcusdt')."""
    return pair.value.lower().replace("_", "")


def _pair_to_coinbase_product(pair: CryptoPair) -> str:
    """Map a CryptoPair to Coinbase product ID (e.g. BTC_USDT → 'BTC-USD')."""
    mapping: dict[CryptoPair, str] = {
        CryptoPair.BTC_USDT: "BTC-USD",
        CryptoPair.ETH_USDT: "ETH-USD",
    }
    return mapping[pair]


def _pair_to_kraken_symbol(pair: CryptoPair) -> str:
    """Map a CryptoPair to Kraken symbol (e.g. BTC_USDT → 'BTC/USD')."""
    mapping: dict[CryptoPair, str] = {
        CryptoPair.BTC_USDT: "BTC/USD",
        CryptoPair.ETH_USDT: "ETH/USD",
    }
    return mapping[pair]


# ── Reverse Mappings (exchange symbol → CryptoPair) ───────────

_BINANCE_TO_PAIR: dict[str, CryptoPair] = {
    "BTCUSDT": CryptoPair.BTC_USDT,
    "ETHUSDT": CryptoPair.ETH_USDT,
}

_COINBASE_TO_PAIR: dict[str, CryptoPair] = {
    "BTC-USD": CryptoPair.BTC_USDT,
    "ETH-USD": CryptoPair.ETH_USDT,
}

_KRAKEN_TO_PAIR: dict[str, CryptoPair] = {
    "BTC/USD": CryptoPair.BTC_USDT,
    "ETH/USD": CryptoPair.ETH_USDT,
}


# ── Ticker Parsing ─────────────────────────────────────────────


def _parse_binance_ticker(data: dict[str, Any]) -> CryptoTicker | None:
    """Parse a Binance 24hr ticker message.

    Expected fields: ``s`` (symbol), ``c`` (last price), ``P`` (change %), ``E`` (event time).
    """
    symbol = str(data.get("s", "")).upper()
    pair = _BINANCE_TO_PAIR.get(symbol)
    if pair is None:
        return None

    try:
        price = Decimal(str(data["c"]))
        change_pct = Decimal(str(data.get("P", "0")))
    except (KeyError, InvalidOperation, ValueError):
        return None

    event_time = data.get("E", 0)
    timestamp = float(event_time) / 1000.0 if event_time else time.time()

    return CryptoTicker(
        pair=pair,
        exchange=CryptoExchange.BINANCE,
        price=price,
        change_pct=change_pct,
        timestamp=timestamp,
        raw=data,
    )


def _parse_coinbase_ticker(data: dict[str, Any]) -> CryptoTicker | None:
    """Parse a Coinbase ticker message.

    Expected structure::

        {
            "type": "ticker",
            "product_id": "BTC-USD",
            "price": "42000.00",
            ...
        }
    """
    product_id = str(data.get("product_id", ""))
    pair = _COINBASE_TO_PAIR.get(product_id)
    if pair is None:
        return None

    try:
        price = Decimal(str(data["price"]))
    except (KeyError, InvalidOperation, ValueError):
        return None

    change_pct = Decimal("0")
    if "price_percent_chg_24_h" in data:
        try:
            change_pct = Decimal(str(data["price_percent_chg_24_h"]))
        except (InvalidOperation, ValueError):
            pass

    return CryptoTicker(
        pair=pair,
        exchange=CryptoExchange.COINBASE,
        price=price,
        change_pct=change_pct,
        timestamp=time.time(),
        raw=data,
    )


def _parse_kraken_ticker(data: dict[str, Any]) -> CryptoTicker | None:
    """Parse a Kraken v2 ticker message.

    Expected structure::

        {
            "channel": "ticker",
            "data": [{"symbol": "BTC/USD", "last": 42000.0, ...}]
        }
    """
    items = data.get("data")
    if not isinstance(items, list) or not items:
        return None

    item = items[0]
    if not isinstance(item, dict):
        return None

    symbol = str(item.get("symbol", ""))
    pair = _KRAKEN_TO_PAIR.get(symbol)
    if pair is None:
        return None

    try:
        price = Decimal(str(item["last"]))
    except (KeyError, InvalidOperation, ValueError):
        return None

    change_pct = Decimal("0")
    if "change_pct" in item:
        try:
            change_pct = Decimal(str(item["change_pct"]))
        except (InvalidOperation, ValueError):
            pass

    return CryptoTicker(
        pair=pair,
        exchange=CryptoExchange.KRAKEN,
        price=price,
        change_pct=change_pct,
        timestamp=time.time(),
        raw=data,
    )


# ── Cross-Validation ──────────────────────────────────────────


def _is_price_validated(
    primary: CryptoTicker,
    validators: list[CryptoTicker],
    threshold_pct: float,
) -> bool:
    """Check if the primary price is within threshold of all validator prices.

    Returns True if all validator prices are within ``threshold_pct`` of
    the primary price. Returns True if validators is empty (no validation
    available).
    """
    if not validators:
        return True

    for validator in validators:
        if primary.price == Decimal("0"):
            return False
        diff_pct = abs(
            (validator.price - primary.price) / primary.price * Decimal("100")
        )
        if diff_pct > Decimal(str(threshold_pct)):
            return False
    return True


# ── CryptoFeed ────────────────────────────────────────────────


class CryptoFeed(BaseFeed):
    """Multi-exchange crypto feed with WS streaming and cross-validation.

    Binance is the primary price source (real-time WS). Coinbase and Kraken
    provide cross-validation. The poll loop runs periodic cross-validation
    checks instead of data fetching.

    Usage::

        feed = CryptoFeed()
        feed.on_event(my_callback)
        async with feed:
            await asyncio.sleep(60)
    """

    def __init__(self, config: CryptoFeedConfig | None = None) -> None:
        cfg = config or get_settings().feeds.crypto
        super().__init__(
            feed_type=FeedType.CRYPTO,
            poll_interval_ms=cfg.poll_interval_ms,
        )
        self._config = cfg
        self._tickers: dict[CryptoExchange, dict[CryptoPair, CryptoTicker]] = {}
        self._baseline_prices: dict[CryptoPair, Decimal] = {}
        self._ws_tasks: list[asyncio.Task[None]] = []

    @property
    def tickers(self) -> dict[CryptoExchange, dict[CryptoPair, CryptoTicker]]:
        """Current ticker state across all exchanges."""
        return {ex: dict(pairs) for ex, pairs in self._tickers.items()}

    def get_ticker(
        self, exchange: CryptoExchange, pair: CryptoPair
    ) -> CryptoTicker | None:
        """Return the latest ticker for a given exchange and pair."""
        return self._tickers.get(exchange, {}).get(pair)

    async def connect(self) -> None:
        """No-op — WS connections are managed in start()."""

    async def close(self) -> None:
        """No-op — WS connections are managed in stop()."""

    async def start(self) -> None:
        """Start the poll loop and WS connections."""
        await super().start()
        # Launch WS tasks for each configured exchange
        for exchange_str in self._config.exchanges:
            exchange_str_lower = exchange_str.lower()
            if exchange_str_lower == "binance":
                task = asyncio.create_task(self._run_binance_ws())
                self._ws_tasks.append(task)
            elif exchange_str_lower == "coinbase":
                task = asyncio.create_task(self._run_coinbase_ws())
                self._ws_tasks.append(task)
            elif exchange_str_lower == "kraken":
                task = asyncio.create_task(self._run_kraken_ws())
                self._ws_tasks.append(task)

    async def stop(self) -> None:
        """Cancel WS tasks, then stop the poll loop."""
        for task in self._ws_tasks:
            task.cancel()
        for task in self._ws_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._ws_tasks.clear()
        await super().stop()

    async def poll(self) -> list[FeedEvent]:
        """Periodic cross-validation: check primary (Binance) against others."""
        events: list[FeedEvent] = []
        binance_tickers = self._tickers.get(CryptoExchange.BINANCE, {})

        for pair_str in self._config.pairs:
            try:
                pair = CryptoPair(pair_str)
            except ValueError:
                continue

            primary = binance_tickers.get(pair)
            if primary is None:
                continue

            # Collect validator tickers
            validators: list[CryptoTicker] = []
            for exchange_str in self._config.exchanges:
                exchange_str_lower = exchange_str.lower()
                if exchange_str_lower == "binance":
                    continue
                try:
                    exchange = CryptoExchange(exchange_str.upper())
                except ValueError:
                    continue
                ticker = self._tickers.get(exchange, {}).get(pair)
                if ticker is not None:
                    validators.append(ticker)

            validated = _is_price_validated(
                primary, validators, self._config.cross_validation_threshold_pct
            )

            # Check for significant price move from baseline
            baseline = self._baseline_prices.get(pair)
            if baseline is not None and baseline != Decimal("0"):
                move_pct = (
                    (primary.price - baseline) / baseline * Decimal("100")
                )
                if abs(move_pct) >= Decimal(str(self._config.price_move_threshold_pct)):
                    events.append(self._to_feed_event(
                        primary, move_pct, validated,
                    ))
                    # Reset baseline after emitting
                    self._baseline_prices[pair] = primary.price
            elif baseline is None:
                # Set initial baseline
                self._baseline_prices[pair] = primary.price

        return events

    def _update_ticker(self, ticker: CryptoTicker) -> None:
        """Update internal ticker state."""
        if ticker.exchange not in self._tickers:
            self._tickers[ticker.exchange] = {}
        self._tickers[ticker.exchange][ticker.pair] = ticker

    async def _emit_on_significant_move(self, ticker: CryptoTicker) -> None:
        """Emit a FeedEvent if the Binance price moved significantly."""
        if ticker.exchange != CryptoExchange.BINANCE:
            return

        baseline = self._baseline_prices.get(ticker.pair)
        if baseline is None:
            self._baseline_prices[ticker.pair] = ticker.price
            return

        if baseline == Decimal("0"):
            return

        move_pct = (ticker.price - baseline) / baseline * Decimal("100")
        if abs(move_pct) >= Decimal(str(self._config.price_move_threshold_pct)):
            # Collect validators for cross-validation
            validators: list[CryptoTicker] = []
            for exchange_str in self._config.exchanges:
                ex_lower = exchange_str.lower()
                if ex_lower == "binance":
                    continue
                try:
                    exchange = CryptoExchange(exchange_str.upper())
                except ValueError:
                    continue
                vt = self._tickers.get(exchange, {}).get(ticker.pair)
                if vt is not None:
                    validators.append(vt)

            validated = _is_price_validated(
                ticker, validators, self._config.cross_validation_threshold_pct
            )
            event = self._to_feed_event(ticker, move_pct, validated)
            await self._emit(event)
            self._baseline_prices[ticker.pair] = ticker.price

    # ── WebSocket Runners ─────────────────────────────────────

    async def _run_binance_ws(self) -> None:
        """Connect to Binance WS, subscribe to tickers, process messages."""
        delay = self._config.reconnect_base_secs
        while self._running:
            try:
                await self._binance_session()
                delay = self._config.reconnect_base_secs
            except asyncio.CancelledError:
                break
            except Exception:
                if not self._running:
                    break
                logger.warning("binance_ws_reconnecting", delay=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._config.reconnect_cap_secs)

    async def _binance_session(self) -> None:
        """Single Binance WS session."""
        ws = await websockets.connect(self._config.binance_ws_url)
        try:
            # Subscribe
            symbols = [
                f"{_pair_to_binance_symbol(CryptoPair(p))}@ticker"
                for p in self._config.pairs
            ]
            subscribe_msg = json.dumps({
                "method": "SUBSCRIBE",
                "params": symbols,
                "id": 1,
            })
            await ws.send(subscribe_msg)

            ping_task = asyncio.create_task(
                self._ping_loop(ws)
            )
            try:
                async for raw_msg in ws:
                    if not self._running:
                        break
                    try:
                        data = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict) or "s" not in data:
                        continue
                    ticker = _parse_binance_ticker(data)
                    if ticker is not None:
                        self._update_ticker(ticker)
                        await self._emit_on_significant_move(ticker)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
        finally:
            await ws.close()

    async def _run_coinbase_ws(self) -> None:
        """Connect to Coinbase WS, subscribe to tickers, update state."""
        delay = self._config.reconnect_base_secs
        while self._running:
            try:
                await self._coinbase_session()
                delay = self._config.reconnect_base_secs
            except asyncio.CancelledError:
                break
            except Exception:
                if not self._running:
                    break
                logger.warning("coinbase_ws_reconnecting", delay=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._config.reconnect_cap_secs)

    async def _coinbase_session(self) -> None:
        """Single Coinbase WS session."""
        ws = await websockets.connect(self._config.coinbase_ws_url)
        try:
            products = [
                _pair_to_coinbase_product(CryptoPair(p))
                for p in self._config.pairs
            ]
            subscribe_msg = json.dumps({
                "type": "subscribe",
                "product_ids": products,
                "channel": "ticker",
            })
            await ws.send(subscribe_msg)

            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    data = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("type") != "ticker":
                    continue
                ticker = _parse_coinbase_ticker(data)
                if ticker is not None:
                    self._update_ticker(ticker)
        finally:
            await ws.close()

    async def _run_kraken_ws(self) -> None:
        """Connect to Kraken WS v2, subscribe to tickers, update state."""
        delay = self._config.reconnect_base_secs
        while self._running:
            try:
                await self._kraken_session()
                delay = self._config.reconnect_base_secs
            except asyncio.CancelledError:
                break
            except Exception:
                if not self._running:
                    break
                logger.warning("kraken_ws_reconnecting", delay=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._config.reconnect_cap_secs)

    async def _kraken_session(self) -> None:
        """Single Kraken WS session."""
        ws = await websockets.connect(self._config.kraken_ws_url)
        try:
            symbols = [
                _pair_to_kraken_symbol(CryptoPair(p))
                for p in self._config.pairs
            ]
            subscribe_msg = json.dumps({
                "method": "subscribe",
                "params": {"channel": "ticker", "symbol": symbols},
            })
            await ws.send(subscribe_msg)

            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    data = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("channel") != "ticker":
                    continue
                ticker = _parse_kraken_ticker(data)
                if ticker is not None:
                    self._update_ticker(ticker)
        finally:
            await ws.close()

    async def _ping_loop(self, ws: Any) -> None:
        """Send PING frames to keep the connection alive."""
        while self._running:
            try:
                await asyncio.sleep(self._config.ping_interval_secs)
                await ws.ping()
            except asyncio.CancelledError:
                break
            except Exception:
                break

    @staticmethod
    def _to_feed_event(
        ticker: CryptoTicker,
        change_pct: Decimal,
        validated: bool,
    ) -> FeedEvent:
        """Convert a CryptoTicker to a generic FeedEvent."""
        return FeedEvent(
            feed_type=FeedType.CRYPTO,
            event_type=FeedEventType.DATA_RELEASED,
            indicator=f"{ticker.pair}_PRICE",
            value=str(ticker.price),
            numeric_value=ticker.price,
            outcome_type=OutcomeType.NUMERIC,
            released_at=ticker.timestamp,
            received_at=time.time(),
            metadata={
                "pair": ticker.pair.value,
                "exchange": ticker.exchange.value,
                "change_pct": str(change_pct),
                "validated": validated,
            },
            raw=ticker.raw,
        )
