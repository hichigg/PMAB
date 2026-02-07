"""MetricsCollector — real-time trade performance tracking.

Subscribes to ``ArbEngine.on_event()`` and aggregates:
- Per-category win rate and P&L
- Latency samples (event release → order fill)
- Cumulative P&L curve
- Liquidity captured vs. available
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from src.core.types import ArbEvent, ArbEventType, MarketCategory


@dataclass
class TradeRecord:
    """Immutable record of a single executed trade."""

    category: MarketCategory
    token_id: str
    side: str
    price: Decimal
    size: Decimal
    fill_price: Decimal
    fill_size: Decimal
    estimated_profit: Decimal
    edge: Decimal
    confidence: float
    success: bool
    feed_released_at: float
    signal_created_at: float
    executed_at: float
    available_depth_usd: Decimal
    timestamp: float


@dataclass
class CategoryStats:
    """Aggregated statistics for a single market category."""

    category: MarketCategory
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_profit: Decimal = Decimal(0)
    total_volume: Decimal = Decimal(0)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades

    @property
    def avg_profit(self) -> Decimal:
        if self.total_trades == 0:
            return Decimal(0)
        return self.total_profit / self.total_trades


@dataclass
class PnLPoint:
    """A single point on the cumulative P&L curve."""

    timestamp: float
    cumulative_pnl: Decimal
    trade_index: int


@dataclass
class LatencySample:
    """Latency breakdown for a single trade."""

    total_secs: float
    feed_latency_secs: float
    processing_secs: float
    category: MarketCategory


class MetricsCollector:
    """Collects real-time performance metrics from ArbEngine events.

    Usage::

        collector = MetricsCollector()
        engine.on_event(collector.on_arb_event)

        # Query metrics at any time:
        summary = collector.summary()
        stats = collector.category_stats()
    """

    def __init__(self, max_latency_samples: int = 10_000) -> None:
        self._trades: list[TradeRecord] = []
        self._category_stats: dict[MarketCategory, CategoryStats] = {}
        self._pnl_curve: list[PnLPoint] = []
        self._cumulative_pnl: Decimal = Decimal(0)
        self._latency_samples: list[LatencySample] = []
        self._max_latency_samples = max_latency_samples

        # Counters for all event types (including non-trade)
        self._signals_generated = 0
        self._trades_executed = 0
        self._trades_failed = 0
        self._trades_skipped = 0
        self._risk_rejected = 0

    # ── Callback entry point ────────────────────────────────────

    def on_arb_event(self, event: ArbEvent) -> None:
        """Callback for ``ArbEngine.on_event()``."""
        etype = event.event_type

        if etype == ArbEventType.SIGNAL_GENERATED:
            self._signals_generated += 1
        elif etype == ArbEventType.TRADE_SKIPPED:
            self._trades_skipped += 1
        elif etype == ArbEventType.RISK_REJECTED:
            self._risk_rejected += 1
        elif etype == ArbEventType.TRADE_EXECUTED:
            self._trades_executed += 1
            self._record_trade(event, success=True)
        elif etype == ArbEventType.TRADE_FAILED:
            self._trades_failed += 1
            self._record_trade(event, success=False)

    # ── Query methods ───────────────────────────────────────────

    @property
    def trades(self) -> list[TradeRecord]:
        return list(self._trades)

    def category_stats(self) -> dict[MarketCategory, CategoryStats]:
        """Return per-category aggregate stats."""
        return dict(self._category_stats)

    def pnl_curve(self) -> list[PnLPoint]:
        """Return the cumulative P&L curve."""
        return list(self._pnl_curve)

    def latency_samples(self) -> list[LatencySample]:
        """Return raw latency samples."""
        return list(self._latency_samples)

    def latency_percentiles(self) -> dict[str, float]:
        """Return latency percentiles (p50, p90, p99) in seconds."""
        if not self._latency_samples:
            return {"p50": 0.0, "p90": 0.0, "p99": 0.0, "min": 0.0, "max": 0.0}

        values = sorted(s.total_secs for s in self._latency_samples)
        n = len(values)
        return {
            "p50": values[int(n * 0.50)],
            "p90": values[min(int(n * 0.90), n - 1)],
            "p99": values[min(int(n * 0.99), n - 1)],
            "min": values[0],
            "max": values[-1],
        }

    def latency_histogram(self, buckets: int = 10) -> list[tuple[float, float, int]]:
        """Return a histogram of total latency.

        Returns list of (bucket_low, bucket_high, count).
        """
        if not self._latency_samples:
            return []

        values = [s.total_secs for s in self._latency_samples]
        lo, hi = min(values), max(values)
        if lo == hi:
            return [(lo, hi, len(values))]

        width = (hi - lo) / buckets
        result: list[tuple[float, float, int]] = []
        for i in range(buckets):
            b_lo = lo + i * width
            b_hi = lo + (i + 1) * width
            count = sum(1 for v in values if b_lo <= v < b_hi)
            if i == buckets - 1:
                count += sum(1 for v in values if v == b_hi)
            result.append((round(b_lo, 4), round(b_hi, 4), count))
        return result

    def liquidity_stats(self) -> dict[str, object]:
        """Return aggregate liquidity captured vs available."""
        if not self._trades:
            return {
                "total_captured_usd": Decimal(0),
                "total_available_usd": Decimal(0),
                "capture_ratio": 0.0,
            }

        captured = sum(
            (t.fill_price * t.fill_size for t in self._trades if t.success),
            Decimal(0),
        )
        available = sum(
            (t.available_depth_usd for t in self._trades if t.success),
            Decimal(0),
        )
        ratio = float(captured / available) if available else 0.0
        return {
            "total_captured_usd": captured,
            "total_available_usd": available,
            "capture_ratio": round(ratio, 4),
        }

    def summary(self) -> dict[str, object]:
        """Return a comprehensive summary of all metrics."""
        total_trades = len(self._trades)
        successful = sum(1 for t in self._trades if t.success)
        win_rate = successful / total_trades if total_trades else 0.0
        avg_profit = (
            self._cumulative_pnl / total_trades if total_trades else Decimal(0)
        )

        return {
            "total_trades": total_trades,
            "successful_trades": successful,
            "failed_trades": total_trades - successful,
            "win_rate": round(win_rate, 4),
            "cumulative_pnl": self._cumulative_pnl,
            "avg_profit_per_trade": avg_profit,
            "signals_generated": self._signals_generated,
            "trades_executed": self._trades_executed,
            "trades_failed": self._trades_failed,
            "trades_skipped": self._trades_skipped,
            "risk_rejected": self._risk_rejected,
            "latency": self.latency_percentiles(),
            "liquidity": self.liquidity_stats(),
        }

    # ── Internal ────────────────────────────────────────────────

    def _record_trade(self, event: ArbEvent, *, success: bool) -> None:
        """Extract data from ArbEvent and record as a TradeRecord."""
        action = event.action
        result = event.result
        signal = event.signal or (action.signal if action else None)

        if action is None:
            return  # Can't record without action data

        # Extract category from the signal's match opportunity
        category = MarketCategory.OTHER
        feed_released_at = 0.0
        signal_created_at = 0.0
        available_depth = Decimal(0)

        if signal is not None:
            signal_created_at = signal.created_at
            match = signal.match
            category = match.opportunity.category
            available_depth = match.opportunity.depth_usd
            feed_released_at = match.feed_event.released_at

        fill_price = action.price
        fill_size = action.size
        if result is not None:
            fill_price = result.fill_price or action.price
            fill_size = result.fill_size or action.size

        executed_at = event.timestamp
        if result is not None and result.executed_at:
            executed_at = result.executed_at

        # Compute estimated P&L for this trade
        # For a successful buy: profit ≈ (1.0 - fill_price) * fill_size (binary market)
        estimated_pnl = action.estimated_profit_usd if success else -action.price * action.size

        record = TradeRecord(
            category=category,
            token_id=action.token_id,
            side=action.side.value,
            price=action.price,
            size=action.size,
            fill_price=fill_price,
            fill_size=fill_size,
            estimated_profit=action.estimated_profit_usd,
            edge=signal.edge if signal else Decimal(0),
            confidence=signal.confidence if signal else 0.0,
            success=success,
            feed_released_at=feed_released_at,
            signal_created_at=signal_created_at,
            executed_at=executed_at,
            available_depth_usd=available_depth,
            timestamp=event.timestamp,
        )
        self._trades.append(record)

        # Update category stats
        if category not in self._category_stats:
            self._category_stats[category] = CategoryStats(category=category)
        stats = self._category_stats[category]
        stats.total_trades += 1
        if success:
            stats.wins += 1
            stats.total_profit += estimated_pnl
        else:
            stats.losses += 1
            stats.total_profit += estimated_pnl
        stats.total_volume += fill_price * fill_size

        # Update cumulative P&L curve
        self._cumulative_pnl += estimated_pnl
        self._pnl_curve.append(PnLPoint(
            timestamp=event.timestamp,
            cumulative_pnl=self._cumulative_pnl,
            trade_index=len(self._trades),
        ))

        # Record latency sample (only for successful trades with timing data)
        if success and feed_released_at > 0 and executed_at > feed_released_at:
            total = executed_at - feed_released_at
            feed_latency = (
                signal.match.feed_event.received_at - feed_released_at
                if signal and signal.match.feed_event.received_at > feed_released_at
                else 0.0
            )
            processing = total - feed_latency
            sample = LatencySample(
                total_secs=total,
                feed_latency_secs=feed_latency,
                processing_secs=processing,
                category=category,
            )
            self._latency_samples.append(sample)
            if len(self._latency_samples) > self._max_latency_samples:
                self._latency_samples = self._latency_samples[-self._max_latency_samples:]
