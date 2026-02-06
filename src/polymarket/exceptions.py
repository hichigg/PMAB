"""Exception hierarchy for the Polymarket CLOB client."""

from __future__ import annotations


class ClobClientError(Exception):
    """Base exception for all CLOB client errors."""


class ClobConnectionError(ClobClientError):
    """Failed to connect to the CLOB API."""


class ClobRateLimitError(ClobClientError):
    """Rate limit exceeded."""


class ClobOrderError(ClobClientError):
    """Order placement or cancellation failed."""


class ClobWebSocketError(ClobClientError):
    """WebSocket connection or message error."""
