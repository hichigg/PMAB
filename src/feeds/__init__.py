"""Data feed connectors — external sources for real-world event outcomes."""

from src.feeds.base import BaseFeed, FeedEventCallback
from src.feeds.crypto import CryptoFeed
from src.feeds.economic import EconomicFeed
from src.feeds.exceptions import FeedConnectionError, FeedError, FeedParseError, FeedRateLimitError
from src.feeds.sports import SportsFeed

__all__ = [
    "BaseFeed",
    "CryptoFeed",
    "EconomicFeed",
    "FeedConnectionError",
    "FeedError",
    "FeedEventCallback",
    "FeedParseError",
    "FeedRateLimitError",
    "SportsFeed",
]
