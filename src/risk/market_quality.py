"""MarketQualityFilter — pre-screens opportunities before they enter the
trading pipeline.

Per CLAUDE.md Phase 4.3, every market must pass these checks before trading:
1. Orderbook depth > $500 on the side you'd trade against
2. Bid-ask spread < 10 cents (otherwise slippage eats profits)
3. Market is not flagged/paused by Polymarket
4. No active UMA disputes on this market
5. Fee rate is 0% (reject markets with dynamic fees unless margin is huge)

Unlike the per-trade risk gates in gates.py (which check TradeAction objects
at execution time), this filter operates on MarketOpportunity objects at the
scanner/engine level — catching bad markets early.
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from src.core.config import RiskConfig
from src.core.types import (
    MarketOpportunity,
    RiskRejectionReason,
    RiskVerdict,
    Side,
)
from src.risk.oracle_monitor import OracleMonitor

logger = structlog.stdlib.get_logger()


class MarketQualityFilter:
    """Consolidated market quality filter for pre-screening opportunities.

    Usage::

        quality_filter = MarketQualityFilter(risk_config, oracle_monitor)

        verdict = quality_filter.check(opportunity)
        if not verdict.approved:
            logger.info("market_rejected", reason=verdict.detail)

        # Or check with trade direction for directional depth
        verdict = quality_filter.check(opportunity, side=Side.BUY)
    """

    def __init__(
        self,
        config: RiskConfig | None = None,
        oracle_monitor: OracleMonitor | None = None,
    ) -> None:
        from src.core.config import get_settings

        self._config = config or get_settings().risk
        self._oracle = oracle_monitor

    def check(
        self,
        opportunity: MarketOpportunity,
        side: Side | None = None,
    ) -> RiskVerdict:
        """Run all quality checks on a market opportunity.

        Args:
            opportunity: The market to screen.
            side: Optional trade direction for directional depth checks.
                  If None, uses total depth.

        Returns:
            RiskVerdict — approved=True if all checks pass, otherwise
            the first rejection with reason and detail.
        """
        # 1. Market status (not flagged/paused)
        verdict = self._check_market_status(opportunity)
        if not verdict.approved:
            return verdict

        # 2. Orderbook depth
        verdict = self._check_depth(opportunity, side)
        if not verdict.approved:
            return verdict

        # 3. Bid-ask spread
        verdict = self._check_spread(opportunity)
        if not verdict.approved:
            return verdict

        # 4. UMA disputes
        verdict = self._check_disputes(opportunity)
        if not verdict.approved:
            return verdict

        # 5. Fee rate
        verdict = self._check_fee_rate(opportunity)
        if not verdict.approved:
            return verdict

        return RiskVerdict(approved=True)

    def check_all(
        self,
        opportunity: MarketOpportunity,
        side: Side | None = None,
    ) -> list[RiskVerdict]:
        """Run all checks and return ALL rejections (not just the first).

        Useful for diagnostics and logging.
        """
        checks = [
            self._check_market_status(opportunity),
            self._check_depth(opportunity, side),
            self._check_spread(opportunity),
            self._check_disputes(opportunity),
            self._check_fee_rate(opportunity),
        ]
        return [v for v in checks if not v.approved]

    def _check_market_status(
        self, opportunity: MarketOpportunity,
    ) -> RiskVerdict:
        """Reject if the market is flagged, paused, closed, or inactive."""
        info = opportunity.market_info
        if info is None:
            return RiskVerdict(approved=True)

        if not info.active:
            return RiskVerdict(
                approved=False,
                reason=RiskRejectionReason.MARKET_NOT_ACTIVE,
                detail=f"Market {opportunity.condition_id} is not active",
            )
        if info.closed:
            return RiskVerdict(
                approved=False,
                reason=RiskRejectionReason.MARKET_NOT_ACTIVE,
                detail=f"Market {opportunity.condition_id} is closed",
            )
        if info.flagged:
            return RiskVerdict(
                approved=False,
                reason=RiskRejectionReason.MARKET_NOT_ACTIVE,
                detail=f"Market {opportunity.condition_id} is flagged",
            )
        if not info.accepting_orders:
            return RiskVerdict(
                approved=False,
                reason=RiskRejectionReason.MARKET_NOT_ACTIVE,
                detail=(
                    f"Market {opportunity.condition_id} is not accepting orders"
                ),
            )
        return RiskVerdict(approved=True)

    def _check_depth(
        self,
        opportunity: MarketOpportunity,
        side: Side | None = None,
    ) -> RiskVerdict:
        """Reject if orderbook depth is below $500 minimum.

        Uses directional depth when side is specified:
        BUY checks ask depth (we buy from asks),
        SELL checks bid depth (we sell into bids).
        Falls back to total depth when no side or directional data is zero.
        """
        min_depth = Decimal(str(self._config.min_orderbook_depth_usd))

        if side == Side.BUY and opportunity.ask_depth_usd > 0:
            depth = opportunity.ask_depth_usd
            depth_label = "ask"
        elif side == Side.SELL and opportunity.bid_depth_usd > 0:
            depth = opportunity.bid_depth_usd
            depth_label = "bid"
        else:
            depth = opportunity.depth_usd
            depth_label = "total"

        if depth < min_depth:
            return RiskVerdict(
                approved=False,
                reason=RiskRejectionReason.ORDERBOOK_DEPTH,
                detail=(
                    f"Market {opportunity.condition_id} "
                    f"{depth_label} depth ${depth} < ${min_depth} minimum"
                ),
            )
        return RiskVerdict(approved=True)

    def _check_spread(self, opportunity: MarketOpportunity) -> RiskVerdict:
        """Reject if bid-ask spread exceeds 10 cents."""
        spread = opportunity.spread
        if spread is None:
            return RiskVerdict(approved=True)

        max_spread = Decimal(str(self._config.max_spread))
        if spread > max_spread:
            return RiskVerdict(
                approved=False,
                reason=RiskRejectionReason.SPREAD_TOO_WIDE,
                detail=(
                    f"Market {opportunity.condition_id} "
                    f"spread {spread} > {max_spread} maximum"
                ),
            )
        return RiskVerdict(approved=True)

    def _check_disputes(self, opportunity: MarketOpportunity) -> RiskVerdict:
        """Reject if the market has an active UMA dispute."""
        if self._oracle is None:
            return RiskVerdict(approved=True)

        if self._oracle.is_disputed(opportunity.condition_id):
            return RiskVerdict(
                approved=False,
                reason=RiskRejectionReason.UMA_EXPOSURE_LIMIT,
                detail=(
                    f"Market {opportunity.condition_id} "
                    f"has an active UMA dispute"
                ),
            )
        return RiskVerdict(approved=True)

    def _check_fee_rate(self, opportunity: MarketOpportunity) -> RiskVerdict:
        """Reject if fee rate exceeds configured maximum.

        Default max is 0 bps (zero fees). Markets with dynamic fees
        are rejected unless the fee override threshold is met — but at
        the opportunity level we don't yet know estimated profit, so we
        apply the strict check. The per-trade gate in gates.py handles
        the profit override at execution time.
        """
        if opportunity.fee_rate_bps <= self._config.max_fee_rate_bps:
            return RiskVerdict(approved=True)

        return RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.FEE_RATE_TOO_HIGH,
            detail=(
                f"Market {opportunity.condition_id} "
                f"fee rate {opportunity.fee_rate_bps}bps "
                f"> {self._config.max_fee_rate_bps}bps limit"
            ),
        )
