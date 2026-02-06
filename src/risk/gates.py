"""Pure risk gate functions — each returns a RiskVerdict."""

from __future__ import annotations

from decimal import Decimal

from src.core.config import RiskConfig
from src.core.types import (
    Position,
    RiskRejectionReason,
    RiskVerdict,
    TradeAction,
)
from src.risk.pnl import PnLTracker


def check_kill_switch(killed: bool) -> RiskVerdict:
    """Reject if the kill switch is active."""
    if killed:
        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.KILL_SWITCH_ACTIVE,
            detail="Kill switch is active — all trading halted",
        )
    return RiskVerdict(approved=True)


def check_daily_loss(pnl: PnLTracker, config: RiskConfig) -> RiskVerdict:
    """Reject if daily realized loss exceeds limit."""
    pnl._maybe_reset_day()
    limit = Decimal(str(config.max_daily_loss_usd))
    if pnl.realized_today < -limit:
        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.DAILY_LOSS_LIMIT,
            detail=(
                f"Daily loss ${pnl.realized_today} exceeds"
                f" -${limit} limit"
            ),
        )
    return RiskVerdict(approved=True)


def check_position_concentration(
    action: TradeAction,
    positions: dict[str, Position],
    config: RiskConfig,
) -> RiskVerdict:
    """Reject if event exposure would exceed bankroll concentration limit."""
    condition_id = ""
    if action.signal and action.signal.match:
        condition_id = action.signal.match.opportunity.condition_id

    if not condition_id:
        return RiskVerdict(approved=True)

    existing_exposure = sum(
        (
            p.entry_price * p.size
            for p in positions.values()
            if p.condition_id == condition_id
        ),
        Decimal(0),
    )
    new_exposure = action.price * action.size
    total = existing_exposure + new_exposure
    limit = Decimal(str(config.bankroll_usd)) * Decimal(
        str(config.max_bankroll_pct_per_event)
    )

    if total > limit:
        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.POSITION_CONCENTRATION,
            detail=(
                f"Event exposure ${total} would exceed"
                f" ${limit} limit ({config.max_bankroll_pct_per_event:.0%}"
                f" of ${config.bankroll_usd})"
            ),
        )
    return RiskVerdict(approved=True)


def check_max_concurrent_positions(
    positions: dict[str, Position],
    config: RiskConfig,
) -> RiskVerdict:
    """Reject if at or above the concurrent position limit."""
    if len(positions) >= config.max_concurrent_positions:
        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.MAX_CONCURRENT_POSITIONS,
            detail=(
                f"{len(positions)} open positions"
                f" >= {config.max_concurrent_positions} limit"
            ),
        )
    return RiskVerdict(approved=True)


def check_orderbook_depth(
    action: TradeAction,
    config: RiskConfig,
) -> RiskVerdict:
    """Reject if orderbook depth is below minimum."""
    depth = action.signal.match.opportunity.depth_usd
    min_depth = Decimal(str(config.min_orderbook_depth_usd))

    if depth < min_depth:
        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.ORDERBOOK_DEPTH,
            detail=f"Depth ${depth} < ${min_depth} minimum",
        )
    return RiskVerdict(approved=True)


def check_spread(
    action: TradeAction,
    config: RiskConfig,
) -> RiskVerdict:
    """Reject if spread is too wide. None spread passes."""
    spread = action.signal.match.opportunity.spread
    if spread is None:
        return RiskVerdict(approved=True)

    max_spread = Decimal(str(config.max_spread))
    if spread > max_spread:
        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.SPREAD_TOO_WIDE,
            detail=f"Spread {spread} > {max_spread} maximum",
        )
    return RiskVerdict(approved=True)
