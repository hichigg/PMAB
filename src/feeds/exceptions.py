"""Exception hierarchy for data feed connectors."""

from __future__ import annotations


class FeedError(Exception):
    """Base exception for all feed errors."""


class FeedConnectionError(FeedError):
    """Failed to connect to a data source (HTTP/WS)."""


class FeedParseError(FeedError):
    """Failed to parse a response from a data source."""


class FeedRateLimitError(FeedError):
    """Rate limited by the data source."""
