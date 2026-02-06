"""Risk management module — position tracking, P&L, gates, and monitoring."""

from src.risk.exceptions import KillSwitchActiveError, RiskError, RiskLimitBreachedError
from src.risk.gates import (
    check_daily_loss,
    check_kill_switch,
    check_max_concurrent_positions,
    check_orderbook_depth,
    check_position_concentration,
    check_spread,
)
from src.risk.monitor import RiskEventCallback, RiskMonitor
from src.risk.pnl import PnLTracker
from src.risk.positions import PositionTracker

__all__ = [
    "KillSwitchActiveError",
    "PnLTracker",
    "PositionTracker",
    "RiskError",
    "RiskEventCallback",
    "RiskLimitBreachedError",
    "RiskMonitor",
    "check_daily_loss",
    "check_kill_switch",
    "check_max_concurrent_positions",
    "check_orderbook_depth",
    "check_position_concentration",
    "check_spread",
]
