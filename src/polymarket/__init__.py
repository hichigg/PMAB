"""Polymarket CLOB client wrapper."""

from src.polymarket.client import PolymarketClient
from src.polymarket.exceptions import (
    ClobClientError,
    ClobConnectionError,
    ClobOrderError,
    ClobPresignError,
    ClobRateLimitError,
    ClobWebSocketError,
)
from src.polymarket.market_params import MarketParams, MarketParamsCache
from src.polymarket.order_pool import PreSignedOrderPool
from src.polymarket.presigner import OrderPreSigner, PreSignedOrder
from src.polymarket.rate_limiter import RateLimiter
from src.polymarket.scanner import MarketScanner
from src.polymarket.ws import OrderBookSubscription

__all__ = [
    "ClobClientError",
    "ClobConnectionError",
    "ClobOrderError",
    "ClobPresignError",
    "ClobRateLimitError",
    "ClobWebSocketError",
    "MarketParams",
    "MarketParamsCache",
    "MarketScanner",
    "OrderBookSubscription",
    "OrderPreSigner",
    "PolymarketClient",
    "PreSignedOrder",
    "PreSignedOrderPool",
    "RateLimiter",
]
