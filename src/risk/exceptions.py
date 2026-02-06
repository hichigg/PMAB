"""Risk management exceptions."""

from __future__ import annotations


class RiskError(Exception):
    """Base exception for risk management errors."""


class RiskLimitBreachedError(RiskError):
    """A risk limit has been breached (informational)."""


class KillSwitchActiveError(RiskError):
    """Trading has been halted by the kill switch."""
