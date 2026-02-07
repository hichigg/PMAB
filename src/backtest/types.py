"""Data types for the backtesting framework."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel

from src.core.config import RiskConfig, StrategyConfig
from src.core.types import (
    ExecutionResult,
    FeedEvent,
    MarketOpportunity,
    OrderBook,
)


class HistoricalEvent(BaseModel):
    """A historical feed event paired with the orderbook snapshot at that moment.

    Represents the market state visible to the bot when the event occurred.
    """

    feed_event: FeedEvent
    orderbooks: dict[str, OrderBook] = {}


class Scenario(BaseModel):
    """A complete backtest scenario — a sequence of historical events.

    Provides the market opportunities (static or per-event) and the
    ordered list of events to replay.
    """

    name: str = "unnamed"
    description: str = ""
    opportunities: dict[str, MarketOpportunity] = {}
    events: list[HistoricalEvent] = []


class BacktestConfig(BaseModel):
    """Configuration overrides for a backtest run."""

    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig = RiskConfig()
    fill_probability: float = 1.0
    slippage_bps: int = 0


@dataclass
class BacktestResult:
    """Aggregated results from a backtest run."""

    scenario_name: str = ""
    total_events: int = 0
    total_trades: int = 0
    successful_trades: int = 0
    failed_trades: int = 0
    signals_generated: int = 0
    trades_skipped: int = 0
    risk_rejected: int = 0
    cumulative_pnl: Decimal = Decimal(0)
    win_rate: float = 0.0
    execution_results: list[ExecutionResult] = field(default_factory=list)
