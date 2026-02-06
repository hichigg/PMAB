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
