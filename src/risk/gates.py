"""Pure risk gate functions — each returns a RiskVerdict."""

from __future__ import annotations

from decimal import Decimal

from src.core.config import KillSwitchConfig, RiskConfig
from src.core.types import (
    OracleProposal,
    OracleProposalState,
    Position,
    RiskRejectionReason,
    RiskVerdict,
    Side,
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


def check_oracle_risk(
    question: str,
    config: KillSwitchConfig,
) -> RiskVerdict:
    """Reject if the market question matches oracle ambiguity patterns."""
    lower_question = question.lower()
    for pattern in config.oracle_blacklist_patterns:
        if pattern.lower() in lower_question:
            return RiskVerdict(
                approved=False,
                reason=RiskRejectionReason.ORACLE_RISK,
                detail=f"Market question matches blacklist pattern: '{pattern}'",
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
    """Reject if orderbook depth is below minimum.

    Uses directional depth when available: BUY checks ask depth (we buy
    from asks), SELL checks bid depth.  Falls back to total depth_usd
    when directional data is zero (backward compat).
    """
    opp = action.signal.match.opportunity
    min_depth = Decimal(str(config.min_orderbook_depth_usd))

    # Pick directional depth based on trade side
    if action.side == Side.BUY and opp.ask_depth_usd > 0:
        depth = opp.ask_depth_usd
    elif action.side == Side.SELL and opp.bid_depth_usd > 0:
        depth = opp.bid_depth_usd
    else:
        depth = opp.depth_usd

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


def check_uma_exposure(
    action: TradeAction,
    positions: dict[str, Position],
    oracle_proposals: dict[str, OracleProposal],
    config: RiskConfig,
) -> RiskVerdict:
    """Reject trades into disputed markets or exceeding UMA exposure limits."""
    condition_id = ""
    if action.signal and action.signal.match:
        condition_id = action.signal.match.opportunity.condition_id

    if not condition_id:
        return RiskVerdict(approved=True)

    oracle_cfg = config.oracle
    proposal = oracle_proposals.get(condition_id)

    # Auto-reject disputed markets
    if (
        proposal is not None
        and proposal.state == OracleProposalState.DISPUTED
        and oracle_cfg.dispute_auto_reject
    ):
        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.UMA_EXPOSURE_LIMIT,
            detail=f"Market {condition_id} has an active UMA dispute",
        )

    # Check UMA exposure limits
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

    usd_limit = Decimal(str(oracle_cfg.max_uma_exposure_usd))
    pct_limit = Decimal(str(config.bankroll_usd)) * Decimal(
        str(oracle_cfg.max_uma_exposure_pct)
    )
    effective_limit = min(usd_limit, pct_limit)

    if total > effective_limit:
        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.UMA_EXPOSURE_LIMIT,
            detail=(
                f"UMA exposure ${total} would exceed"
                f" ${effective_limit} limit"
                f" (usd={usd_limit}, pct={pct_limit})"
            ),
        )

    return RiskVerdict(approved=True)


def check_position_size(
    action: TradeAction,
    config: RiskConfig,
) -> RiskVerdict:
    """Reject if the individual trade value exceeds max_position_usd."""
    trade_value = action.price * action.size
    limit = Decimal(str(config.max_position_usd))
    if trade_value > limit:
        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.POSITION_SIZE_EXCEEDED,
            detail=(
                f"Trade value ${trade_value} exceeds"
                f" ${limit} max position limit"
            ),
        )
    return RiskVerdict(approved=True)


def check_market_status(
    action: TradeAction,
    config: RiskConfig,
) -> RiskVerdict:
    """Reject if the market is not in a tradeable state.

    Checks market_info for: active, closed, flagged, accepting_orders.
    Passes if market_info is None (no data to check).
    """
    market_info = action.signal.match.opportunity.market_info
    if market_info is None:
        return RiskVerdict(approved=True)

    if not market_info.active:
        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.MARKET_NOT_ACTIVE,
            detail="Market is not active",
        )
    if market_info.closed:
        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.MARKET_NOT_ACTIVE,
            detail="Market is closed",
        )
    if market_info.flagged:
        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.MARKET_NOT_ACTIVE,
            detail="Market is flagged",
        )
    if not market_info.accepting_orders:
        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.MARKET_NOT_ACTIVE,
            detail="Market is not accepting orders",
        )
    return RiskVerdict(approved=True)


def check_fee_rate(
    action: TradeAction,
    config: RiskConfig,
) -> RiskVerdict:
    """Reject if the market fee rate exceeds the configured maximum.

    Allows non-zero fees when estimated profit exceeds the override
    threshold (fee_override_min_profit_usd).
    """
    fee_bps = action.signal.match.opportunity.fee_rate_bps
    if fee_bps <= config.max_fee_rate_bps:
        return RiskVerdict(approved=True)

    # Check override: high-profit trades can bypass fee limit
    override_threshold = Decimal(str(config.fee_override_min_profit_usd))
    if action.estimated_profit_usd >= override_threshold:
        return RiskVerdict(approved=True)

    return RiskVerdict(
        approved=False,
        reason=RiskRejectionReason.FEE_RATE_TOO_HIGH,
        detail=(
            f"Fee rate {fee_bps}bps > {config.max_fee_rate_bps}bps limit"
            f" and profit ${action.estimated_profit_usd}"
            f" < ${override_threshold} override"
        ),
    )
