"""Domain types for Polymarket interactions — all prices/sizes use Decimal."""

from __future__ import annotations

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
