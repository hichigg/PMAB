"""Strategy-layer exceptions."""

from __future__ import annotations


class StrategyError(Exception):
    """Base exception for strategy errors."""


class MatchError(StrategyError):
    """Raised when event-to-market matching fails."""


class SignalError(StrategyError):
    """Raised when signal generation fails."""


class SizingError(StrategyError):
    """Raised when position sizing fails."""


class PrioritizationError(StrategyError):
    """Raised when opportunity prioritization fails."""


class ExecutionError(StrategyError):
    """Raised when order execution fails in the strategy layer."""
