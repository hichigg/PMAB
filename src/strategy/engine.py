"""ArbEngine — orchestrates the event→match→signal→size→execute pipeline."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import structlog

from src.core.config import RiskConfig, StrategyConfig, get_settings
from src.core.types import (
    ArbEvent,
    ArbEventType,
    ExecutionResult,
    FeedEvent,
    FeedEventType,
    MarketOrderRequest,
    MatchResult,
    OrderRequest,
    OrderType,
    Side,
    TradeAction,
)
from src.polymarket.client import PolymarketClient
from src.polymarket.scanner import MarketScanner
from src.strategy.matcher import MarketMatcher
from src.strategy.signals import SignalGenerator
from src.strategy.sizer import PositionSizer

logger = structlog.stdlib.get_logger()

ArbEventCallback = Callable[[ArbEvent], Awaitable[None] | None]


class ArbEngine:
    """Core arbitrage engine — consumes feed events and produces trades.

    The engine does NOT own feed or scanner lifecycle. It registers as a
    callback consumer via ``on_feed_event`` which can be passed to
    ``feed.on_event()``.

    Usage::

        engine = ArbEngine(client, scanner)
        engine.on_event(my_callback)
        await engine.start()

        # Wire up feeds:
        economic_feed.on_event(engine.on_feed_event)

        # ... later ...
        await engine.stop()
    """

    def __init__(
        self,
        client: PolymarketClient,
        scanner: MarketScanner,
        config: StrategyConfig | None = None,
        risk_config: RiskConfig | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client
        self._scanner = scanner
        self._config = config or settings.strategy
        self._risk = risk_config or settings.risk

        self._matcher = MarketMatcher(self._config.match_confidence_threshold)
        self._signal_gen = SignalGenerator(self._config)
        self._sizer = PositionSizer(self._config, self._risk)

        self._callbacks: list[ArbEventCallback] = []
        self._lock = asyncio.Lock()
        self._running = False

        # Stats
        self._signals_generated = 0
        self._trades_executed = 0
        self._trades_failed = 0
        self._trades_skipped = 0

    @property
    def stats(self) -> dict[str, int]:
        """Current engine statistics."""
        return {
            "signals_generated": self._signals_generated,
            "trades_executed": self._trades_executed,
            "trades_failed": self._trades_failed,
            "trades_skipped": self._trades_skipped,
        }

    @property
    def running(self) -> bool:
        """Whether the engine is currently running."""
        return self._running

    def on_event(self, callback: ArbEventCallback) -> None:
        """Register a callback for arb engine events."""
        self._callbacks.append(callback)

    async def _emit(self, event: ArbEvent) -> None:
        """Dispatch an arb event to all registered callbacks."""
        for cb in self._callbacks:
            try:
                result = cb(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("arb_event_callback_error", event_type=event.event_type)

    async def start(self) -> None:
        """Start the engine."""
        if self._running:
            return
        self._running = True
        logger.info("arb_engine_started")
        await self._emit(ArbEvent(
            event_type=ArbEventType.ENGINE_STARTED,
            reason="Engine started",
            timestamp=time.time(),
        ))

    async def stop(self) -> None:
        """Stop the engine."""
        self._running = False
        logger.info("arb_engine_stopped", stats=self.stats)
        await self._emit(ArbEvent(
            event_type=ArbEventType.ENGINE_STOPPED,
            reason="Engine stopped",
            timestamp=time.time(),
        ))

    async def on_feed_event(self, event: FeedEvent) -> None:
        """Primary entry point — designed to be passed to ``feed.on_event()``.

        Ignores non-DATA_RELEASED events. Uses asyncio.Lock to serialize
        processing so overlapping events don't cause race conditions.
        """
        if not self._running:
            return

        if event.event_type != FeedEventType.DATA_RELEASED:
            return

        async with self._lock:
            await self._process_event_internal(event)

    async def process_event(self, event: FeedEvent) -> list[ExecutionResult]:
        """Process a feed event and return execution results.

        Public method for testing — returns results instead of just emitting.
        """
        results: list[ExecutionResult] = []

        # Match event to opportunities
        opportunities = self._scanner.opportunities
        matches = self._matcher.match(event, opportunities)

        if not matches:
            logger.debug("no_matches", indicator=event.indicator)
            return results

        for match in matches:
            result = await self._process_match(match)
            if result is not None:
                results.append(result)

        return results

    async def _process_event_internal(self, event: FeedEvent) -> None:
        """Internal event processing with callback emission."""
        opportunities = self._scanner.opportunities
        matches = self._matcher.match(event, opportunities)

        if not matches:
            logger.debug("no_matches", indicator=event.indicator)
            return

        for match in matches:
            await self._process_match(match)

    async def _process_match(self, match: MatchResult) -> ExecutionResult | None:
        """Signal → size → execute pipeline for a single match."""
        # Generate signal
        signal = self._signal_gen.evaluate(match)
        if signal is None:
            self._trades_skipped += 1
            await self._emit(ArbEvent(
                event_type=ArbEventType.TRADE_SKIPPED,
                reason="No actionable signal",
                timestamp=time.time(),
            ))
            return None

        self._signals_generated += 1
        await self._emit(ArbEvent(
            event_type=ArbEventType.SIGNAL_GENERATED,
            signal=signal,
            reason=match.match_reason,
            timestamp=time.time(),
        ))

        # Size position
        action = self._sizer.size(signal)
        if action is None:
            self._trades_skipped += 1
            await self._emit(ArbEvent(
                event_type=ArbEventType.TRADE_SKIPPED,
                signal=signal,
                reason="Position sizing returned None",
                timestamp=time.time(),
            ))
            return None

        # Execute
        result = await self._execute_action(action)
        return result

    async def _execute_action(self, action: TradeAction) -> ExecutionResult:
        """Execute a trade action via the Polymarket client."""
        now = time.time()

        try:
            if action.order_type == OrderType.FOK:
                # Market order (FOK)
                worst_price = (
                    action.price + action.max_slippage
                    if action.side == Side.BUY
                    else action.price - action.max_slippage
                )
                req = MarketOrderRequest(
                    token_id=action.token_id,
                    side=action.side,
                    size=action.size,
                    worst_price=worst_price,
                )
                response = await self._client.place_market_order(req)
            else:
                # Limit order (GTC)
                req_limit = OrderRequest(
                    token_id=action.token_id,
                    side=action.side,
                    price=action.price,
                    size=action.size,
                    order_type=action.order_type,
                )
                response = await self._client.place_order(req_limit)

            result = ExecutionResult(
                action=action,
                order_response=response,
                success=response.success,
                fill_price=action.price if response.success else None,
                fill_size=action.size if response.success else None,
                executed_at=now,
            )

            if response.success:
                self._trades_executed += 1
                logger.info(
                    "trade_executed",
                    token_id=action.token_id,
                    side=action.side,
                    size=float(action.size),
                    price=float(action.price),
                    edge=float(action.signal.edge),
                )
                await self._emit(ArbEvent(
                    event_type=ArbEventType.TRADE_EXECUTED,
                    signal=action.signal,
                    action=action,
                    result=result,
                    reason=action.reason,
                    timestamp=now,
                ))
            else:
                self._trades_failed += 1
                logger.warning(
                    "trade_failed",
                    token_id=action.token_id,
                    response=response.raw,
                )
                await self._emit(ArbEvent(
                    event_type=ArbEventType.TRADE_FAILED,
                    signal=action.signal,
                    action=action,
                    result=result,
                    reason="Order not successful",
                    timestamp=now,
                ))

            return result

        except Exception as exc:
            self._trades_failed += 1
            error_msg = str(exc)
            logger.exception("trade_execution_error", error=error_msg)

            result = ExecutionResult(
                action=action,
                success=False,
                executed_at=now,
                error=error_msg,
            )
            await self._emit(ArbEvent(
                event_type=ArbEventType.TRADE_FAILED,
                signal=action.signal,
                action=action,
                result=result,
                reason=error_msg,
                timestamp=now,
            ))
            return result
