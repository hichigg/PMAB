"""Risk management module — position tracking, P&L, gates, and monitoring."""

from src.risk.exceptions import (
    KillSwitchActiveError,
    OracleRiskError,
    RiskError,
    RiskLimitBreachedError,
)
from src.risk.gates import (
    check_daily_loss,
    check_fee_rate,
    check_kill_switch,
    check_market_status,
    check_max_concurrent_positions,
    check_oracle_risk,
    check_orderbook_depth,
    check_position_concentration,
    check_position_size,
    check_spread,
    check_uma_exposure,
)
from src.risk.kill_switch import KillSwitchManager
from src.risk.monitor import RiskEventCallback, RiskMonitor
from src.risk.oracle_monitor import OracleAlertCallback, OracleMonitor
from src.risk.pnl import PnLTracker
from src.risk.positions import PositionTracker

__all__ = [
    "KillSwitchActiveError",
    "KillSwitchManager",
    "OracleAlertCallback",
    "OracleMonitor",
    "OracleRiskError",
    "PnLTracker",
    "PositionTracker",
    "RiskError",
    "RiskEventCallback",
    "RiskLimitBreachedError",
    "RiskMonitor",
    "check_daily_loss",
    "check_fee_rate",
    "check_kill_switch",
    "check_market_status",
    "check_max_concurrent_positions",
    "check_oracle_risk",
    "check_orderbook_depth",
    "check_position_concentration",
    "check_position_size",
    "check_spread",
    "check_uma_exposure",
]
