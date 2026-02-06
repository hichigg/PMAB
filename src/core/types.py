"""Domain types for Polymarket interactions — all prices/sizes use Decimal."""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Side(StrEnum):
    """Order side."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    """Order type."""

    GTC = "GTC"  # Good till cancelled
    FOK = "FOK"  # Fill or kill
    GTD = "GTD"  # Good till date


class PriceLevel(BaseModel):
    """A single price level in the order book."""

    price: Decimal
    size: Decimal


class OrderBook(BaseModel):
    """Order book snapshot with computed properties."""

    token_id: str
    bids: list[PriceLevel] = Field(default_factory=list)
    asks: list[PriceLevel] = Field(default_factory=list)
    timestamp: float = 0.0

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        if self.best_ask is not None and self.best_bid is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def depth_usd(self) -> Decimal:
        bid_depth = sum((lvl.price * lvl.size for lvl in self.bids), Decimal(0))
        ask_depth = sum((lvl.price * lvl.size for lvl in self.asks), Decimal(0))
        return bid_depth + ask_depth


class OrderRequest(BaseModel):
    """Input type for placing a limit order."""

    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    order_type: OrderType = OrderType.GTC
    expiration: int | None = None


class MarketOrderRequest(BaseModel):
    """Input type for placing a market order (FOK)."""

    token_id: str
    side: Side
    size: Decimal
    worst_price: Decimal | None = None


class OrderResponse(BaseModel):
    """Response from placing an order."""

    order_id: str = ""
    success: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class CancelResponse(BaseModel):
    """Response from cancelling an order."""

    order_id: str = ""
    success: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class MarketInfo(BaseModel):
    """Simplified market metadata."""

    condition_id: str
    question: str = ""
    description: str = ""
    tokens: list[dict[str, Any]] = Field(default_factory=list)
    active: bool = True
    closed: bool = False
    end_date_iso: str = ""
    tags: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


# ── Scanner Types ────────────────────────────────────────────────


class MarketCategory(StrEnum):
    """Category for classifying markets."""

    ECONOMIC = "ECONOMIC"
    SPORTS = "SPORTS"
    CRYPTO = "CRYPTO"
    POLITICS = "POLITICS"
    OTHER = "OTHER"


class ScanEventType(StrEnum):
    """Type of scanner event."""

    OPPORTUNITY_FOUND = "OPPORTUNITY_FOUND"
    OPPORTUNITY_UPDATED = "OPPORTUNITY_UPDATED"
    OPPORTUNITY_LOST = "OPPORTUNITY_LOST"


class ScanFilter(BaseModel):
    """Filter criteria for market scanning."""

    categories: list[MarketCategory] = Field(default_factory=list)
    tag_allowlist: list[str] = Field(default_factory=list)
    tag_blocklist: list[str] = Field(default_factory=list)
    question_patterns: list[str] = Field(default_factory=list)
    require_active: bool = True
    exclude_closed: bool = True
    min_hours_to_expiry: float | None = None
    max_hours_to_expiry: float | None = None

    def compiled_patterns(self) -> list[re.Pattern[str]]:
        """Return compiled regex patterns for question matching."""
        return [re.compile(p, re.IGNORECASE) for p in self.question_patterns]


class LiquidityScreen(BaseModel):
    """Liquidity thresholds for filtering markets."""

    min_depth_usd: Decimal = Decimal("100")
    max_spread: Decimal = Decimal("0.10")
    min_bid_depth_usd: Decimal = Decimal("0")
    min_ask_depth_usd: Decimal = Decimal("0")


class MarketOpportunity(BaseModel):
    """A scored market opportunity tracked by the scanner."""

    condition_id: str
    question: str = ""
    category: MarketCategory = MarketCategory.OTHER
    tokens: list[dict[str, Any]] = Field(default_factory=list)
    token_id: str = ""
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    spread: Decimal | None = None
    depth_usd: Decimal = Decimal("0")
    score: float = 0.0
    first_seen: float = 0.0
    last_updated: float = 0.0
    market_info: MarketInfo | None = None


class ScanEvent(BaseModel):
    """Event emitted by the scanner."""

    event_type: ScanEventType
    opportunity: MarketOpportunity
    timestamp: float = 0.0


# ── Feed Types ──────────────────────────────────────────────────


class FeedType(StrEnum):
    """Type of data feed."""

    ECONOMIC = "ECONOMIC"
    SPORTS = "SPORTS"
    CRYPTO = "CRYPTO"


class OutcomeType(StrEnum):
    """How a feed event outcome is expressed."""

    NUMERIC = "NUMERIC"  # e.g. CPI = 3.2%
    BOOLEAN = "BOOLEAN"  # e.g. did X happen?
    CATEGORICAL = "CATEGORICAL"  # e.g. winner = TeamA


class FeedEventType(StrEnum):
    """Type of feed event."""

    DATA_RELEASED = "DATA_RELEASED"
    FEED_CONNECTED = "FEED_CONNECTED"
    FEED_DISCONNECTED = "FEED_DISCONNECTED"
    FEED_ERROR = "FEED_ERROR"


class FeedEvent(BaseModel):
    """Source-agnostic event emitted by a data feed."""

    feed_type: FeedType
    event_type: FeedEventType
    indicator: str = ""
    value: str = ""
    numeric_value: Decimal | None = None
    outcome_type: OutcomeType = OutcomeType.NUMERIC
    released_at: float = 0.0
    received_at: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class EconomicIndicator(StrEnum):
    """Known economic indicators tracked by the economic feed."""

    CPI = "CPI"
    CORE_CPI = "CORE_CPI"
    NFP = "NFP"
    UNEMPLOYMENT = "UNEMPLOYMENT"
    GDP = "GDP"
    PPI = "PPI"
    PCE = "PCE"
    FED_RATE = "FED_RATE"
    INITIAL_CLAIMS = "INITIAL_CLAIMS"


class EconomicRelease(BaseModel):
    """A single economic data release from a feed source."""

    indicator: EconomicIndicator
    value: str = ""
    numeric_value: Decimal | None = None
    prior_value: str = ""
    forecast_value: str = ""
    released_at: float = 0.0
    source: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


# ── Sports Types ───────────────────────────────────────────────


class SportLeague(StrEnum):
    """Supported sport leagues."""

    NFL = "NFL"
    NBA = "NBA"
    MLB = "MLB"
    NHL = "NHL"


class GameStatus(StrEnum):
    """Status of a sporting event."""

    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    FINAL = "FINAL"
    DELAYED = "DELAYED"
    CANCELLED = "CANCELLED"


class GameResult(BaseModel):
    """Result of a completed (or in-progress) sporting event."""

    game_id: str
    league: SportLeague
    home_team: str = ""
    away_team: str = ""
    home_score: int = 0
    away_score: int = 0
    winner: str = ""
    status: GameStatus = GameStatus.SCHEDULED
    start_time: float = 0.0
    completed_at: float = 0.0
    raw: dict[str, Any] = Field(default_factory=dict)


# ── Crypto Types ───────────────────────────────────────────────


class CryptoPair(StrEnum):
    """Crypto trading pairs tracked by the crypto feed."""

    BTC_USDT = "BTC_USDT"
    ETH_USDT = "ETH_USDT"


class CryptoExchange(StrEnum):
    """Supported cryptocurrency exchanges."""

    BINANCE = "BINANCE"
    COINBASE = "COINBASE"
    KRAKEN = "KRAKEN"


class CryptoTicker(BaseModel):
    """Price ticker from a cryptocurrency exchange."""

    pair: CryptoPair
    exchange: CryptoExchange
    price: Decimal
    change_pct: Decimal = Decimal("0")
    timestamp: float = 0.0
    raw: dict[str, Any] = Field(default_factory=dict)


# ── Strategy Types ─────────────────────────────────────────────


class SignalDirection(StrEnum):
    """Direction of a trading signal."""

    BUY = "BUY"
    SELL = "SELL"


class MatchResult(BaseModel):
    """Result of matching a feed event to a market opportunity."""

    feed_event: FeedEvent
    opportunity: MarketOpportunity
    target_token_id: str = ""
    target_outcome: str = ""
    match_confidence: float = 0.0
    match_reason: str = ""


class PrioritizedMatch(BaseModel):
    """A match ranked by composite priority score."""

    match: MatchResult
    priority_score: float = 0.0
    score_components: dict[str, float] = Field(default_factory=dict)
    rank: int = 0


class Signal(BaseModel):
    """A trading signal derived from a match between event and market."""

    match: MatchResult
    fair_value: Decimal = Decimal("0")
    confidence: float = 0.0
    direction: SignalDirection = SignalDirection.BUY
    edge: Decimal = Decimal("0")
    current_price: Decimal = Decimal("0")
    created_at: float = 0.0


class TradeAction(BaseModel):
    """A sized, executable trade derived from a signal."""

    signal: Signal
    token_id: str = ""
    side: Side = Side.BUY
    price: Decimal = Decimal("0")
    size: Decimal = Decimal("0")
    order_type: OrderType = OrderType.FOK
    max_slippage: Decimal = Decimal("0.02")
    estimated_profit_usd: Decimal = Decimal("0")
    reason: str = ""


class ExecutionResult(BaseModel):
    """Result of attempting to execute a trade action."""

    action: TradeAction
    order_response: OrderResponse | None = None
    success: bool = False
    fill_price: Decimal | None = None
    fill_size: Decimal | None = None
    executed_at: float = 0.0
    error: str = ""


class ArbEventType(StrEnum):
    """Type of arbitrage engine event."""

    SIGNAL_GENERATED = "SIGNAL_GENERATED"
    TRADE_EXECUTED = "TRADE_EXECUTED"
    TRADE_FAILED = "TRADE_FAILED"
    TRADE_SKIPPED = "TRADE_SKIPPED"
    RISK_REJECTED = "RISK_REJECTED"
    ENGINE_STARTED = "ENGINE_STARTED"
    ENGINE_STOPPED = "ENGINE_STOPPED"


class ArbEvent(BaseModel):
    """Event emitted by the arbitrage engine."""

    event_type: ArbEventType
    signal: Signal | None = None
    action: TradeAction | None = None
    result: ExecutionResult | None = None
    reason: str = ""
    timestamp: float = 0.0


# ── Risk Types ─────────────────────────────────────────────────


class RiskEventType(StrEnum):
    """Type of risk management event."""

    KILL_SWITCH_TRIGGERED = "KILL_SWITCH_TRIGGERED"
    KILL_SWITCH_RESET = "KILL_SWITCH_RESET"
    RISK_GATE_REJECTED = "RISK_GATE_REJECTED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CLOSED = "POSITION_CLOSED"
    DAILY_PNL_UPDATED = "DAILY_PNL_UPDATED"


class RiskRejectionReason(StrEnum):
    """Reason a trade was rejected by a risk gate."""

    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    POSITION_CONCENTRATION = "POSITION_CONCENTRATION"
    MAX_CONCURRENT_POSITIONS = "MAX_CONCURRENT_POSITIONS"
    ORDERBOOK_DEPTH = "ORDERBOOK_DEPTH"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"


class Position(BaseModel):
    """An open position tracked by the risk system."""

    token_id: str
    condition_id: str = ""
    side: Side = Side.BUY
    entry_price: Decimal = Decimal("0")
    size: Decimal = Decimal("0")
    opened_at: float = 0.0
    last_updated: float = 0.0


class RiskVerdict(BaseModel):
    """Result of a risk gate check."""

    approved: bool = True
    reason: RiskRejectionReason | None = None
    detail: str = ""


class RiskEvent(BaseModel):
    """Event emitted by the risk monitor."""

    event_type: RiskEventType
    position: Position | None = None
    verdict: RiskVerdict | None = None
    daily_pnl: Decimal | None = None
    reason: str = ""
    timestamp: float = 0.0
