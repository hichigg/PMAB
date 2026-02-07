"""Tests for MarketQualityFilter — Phase 4.3 market pre-screening."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.config import OracleConfig, RiskConfig
from src.core.types import (
    MarketCategory,
    MarketInfo,
    MarketOpportunity,
    OracleProposal,
    OracleProposalState,
    RiskRejectionReason,
    Side,
)
from src.risk.market_quality import MarketQualityFilter
from src.risk.oracle_monitor import OracleMonitor

# ── Helpers ─────────────────────────────────────────────────────


def _cfg(**overrides: object) -> RiskConfig:
    defaults: dict[str, object] = {
        "min_orderbook_depth_usd": 500.0,
        "max_spread": 0.10,
        "max_fee_rate_bps": 0,
        "fee_override_min_profit_usd": 100.0,
    }
    defaults.update(overrides)
    return RiskConfig(**defaults)  # type: ignore[arg-type]


def _opportunity(
    condition_id: str = "cond1",
    depth_usd: Decimal = Decimal("5000"),
    bid_depth_usd: Decimal = Decimal("2500"),
    ask_depth_usd: Decimal = Decimal("2500"),
    spread: Decimal | None = Decimal("0.05"),
    fee_rate_bps: int = 0,
    market_info: MarketInfo | None = None,
) -> MarketOpportunity:
    if market_info is None:
        market_info = MarketInfo(
            condition_id=condition_id,
            active=True,
            closed=False,
            flagged=False,
            accepting_orders=True,
        )
    return MarketOpportunity(
        condition_id=condition_id,
        question="Will CPI exceed 3.0%?",
        category=MarketCategory.ECONOMIC,
        depth_usd=depth_usd,
        bid_depth_usd=bid_depth_usd,
        ask_depth_usd=ask_depth_usd,
        spread=spread,
        fee_rate_bps=fee_rate_bps,
        market_info=market_info,
        best_bid=Decimal("0.45"),
        best_ask=Decimal("0.50"),
    )


def _oracle_with_dispute(condition_id: str = "cond1") -> OracleMonitor:
    monitor = OracleMonitor(config=OracleConfig(enabled=True))
    monitor._proposals[condition_id] = OracleProposal(
        condition_id=condition_id,
        state=OracleProposalState.DISPUTED,
    )
    return monitor


# ── Composite Check ──────────────────────────────────────────


class TestCompositeCheck:
    def test_all_pass(self) -> None:
        f = MarketQualityFilter(config=_cfg())
        v = f.check(_opportunity())
        assert v.approved is True

    def test_all_pass_no_market_info(self) -> None:
        """No market_info → status check passes, rest uses opportunity fields."""
        opp = _opportunity()
        opp.market_info = None
        f = MarketQualityFilter(config=_cfg())
        v = f.check(opp)
        assert v.approved is True

    def test_first_failure_returned(self) -> None:
        """When multiple checks fail, first rejection wins."""
        info = MarketInfo(
            condition_id="cond1",
            active=False,
            flagged=True,
        )
        opp = _opportunity(
            market_info=info,
            depth_usd=Decimal("10"),
            spread=Decimal("0.50"),
            fee_rate_bps=315,
        )
        f = MarketQualityFilter(config=_cfg())
        v = f.check(opp)
        assert v.approved is False
        # Market status is checked first
        assert v.reason == RiskRejectionReason.MARKET_NOT_ACTIVE


# ── check_all diagnostics ───────────────────────────────────


class TestCheckAll:
    def test_no_failures(self) -> None:
        f = MarketQualityFilter(config=_cfg())
        rejections = f.check_all(_opportunity())
        assert rejections == []

    def test_multiple_failures_returned(self) -> None:
        info = MarketInfo(
            condition_id="cond1",
            active=False,
        )
        opp = _opportunity(
            market_info=info,
            depth_usd=Decimal("10"),
            spread=Decimal("0.50"),
            fee_rate_bps=315,
        )
        oracle = _oracle_with_dispute("cond1")
        f = MarketQualityFilter(config=_cfg(), oracle_monitor=oracle)
        rejections = f.check_all(opp)
        # Should get: market_status, depth, spread, dispute, fee_rate
        assert len(rejections) == 5
        reasons = {r.reason for r in rejections}
        assert RiskRejectionReason.MARKET_NOT_ACTIVE in reasons
        assert RiskRejectionReason.ORDERBOOK_DEPTH in reasons
        assert RiskRejectionReason.SPREAD_TOO_WIDE in reasons
        assert RiskRejectionReason.UMA_EXPOSURE_LIMIT in reasons
        assert RiskRejectionReason.FEE_RATE_TOO_HIGH in reasons


# ── Market Status ─────────────────────────────────────────────


class TestMarketStatus:
    def test_active_market_passes(self) -> None:
        f = MarketQualityFilter(config=_cfg())
        v = f.check(_opportunity())
        assert v.approved is True

    def test_inactive_market_rejected(self) -> None:
        info = MarketInfo(condition_id="cond1", active=False)
        f = MarketQualityFilter(config=_cfg())
        v = f.check(_opportunity(market_info=info))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.MARKET_NOT_ACTIVE
        assert "not active" in v.detail

    def test_closed_market_rejected(self) -> None:
        info = MarketInfo(condition_id="cond1", active=True, closed=True)
        f = MarketQualityFilter(config=_cfg())
        v = f.check(_opportunity(market_info=info))
        assert v.approved is False
        assert "closed" in v.detail

    def test_flagged_market_rejected(self) -> None:
        info = MarketInfo(condition_id="cond1", flagged=True)
        f = MarketQualityFilter(config=_cfg())
        v = f.check(_opportunity(market_info=info))
        assert v.approved is False
        assert "flagged" in v.detail

    def test_not_accepting_orders_rejected(self) -> None:
        info = MarketInfo(condition_id="cond1", accepting_orders=False)
        f = MarketQualityFilter(config=_cfg())
        v = f.check(_opportunity(market_info=info))
        assert v.approved is False
        assert "not accepting orders" in v.detail

    def test_no_market_info_passes(self) -> None:
        opp = _opportunity()
        opp.market_info = None
        f = MarketQualityFilter(config=_cfg())
        v = f.check(opp)
        assert v.approved is True


# ── Orderbook Depth ───────────────────────────────────────────


class TestOrderbookDepth:
    def test_sufficient_total_depth(self) -> None:
        f = MarketQualityFilter(config=_cfg(min_orderbook_depth_usd=500.0))
        v = f.check(_opportunity(depth_usd=Decimal("5000")))
        assert v.approved is True

    def test_insufficient_total_depth(self) -> None:
        f = MarketQualityFilter(config=_cfg(min_orderbook_depth_usd=500.0))
        v = f.check(_opportunity(depth_usd=Decimal("100")))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.ORDERBOOK_DEPTH

    def test_boundary_at_limit_passes(self) -> None:
        f = MarketQualityFilter(config=_cfg(min_orderbook_depth_usd=500.0))
        v = f.check(_opportunity(depth_usd=Decimal("500")))
        assert v.approved is True

    def test_buy_uses_ask_depth(self) -> None:
        f = MarketQualityFilter(config=_cfg(min_orderbook_depth_usd=500.0))
        opp = _opportunity(
            ask_depth_usd=Decimal("600"),
            bid_depth_usd=Decimal("100"),
            depth_usd=Decimal("700"),
        )
        v = f.check(opp, side=Side.BUY)
        assert v.approved is True

    def test_buy_rejects_low_ask_depth(self) -> None:
        f = MarketQualityFilter(config=_cfg(min_orderbook_depth_usd=500.0))
        opp = _opportunity(
            ask_depth_usd=Decimal("200"),
            bid_depth_usd=Decimal("5000"),
            depth_usd=Decimal("5200"),
        )
        v = f.check(opp, side=Side.BUY)
        assert v.approved is False
        assert "ask" in v.detail

    def test_sell_uses_bid_depth(self) -> None:
        f = MarketQualityFilter(config=_cfg(min_orderbook_depth_usd=500.0))
        opp = _opportunity(
            bid_depth_usd=Decimal("600"),
            ask_depth_usd=Decimal("100"),
            depth_usd=Decimal("700"),
        )
        v = f.check(opp, side=Side.SELL)
        assert v.approved is True

    def test_sell_rejects_low_bid_depth(self) -> None:
        f = MarketQualityFilter(config=_cfg(min_orderbook_depth_usd=500.0))
        opp = _opportunity(
            bid_depth_usd=Decimal("200"),
            ask_depth_usd=Decimal("5000"),
            depth_usd=Decimal("5200"),
        )
        v = f.check(opp, side=Side.SELL)
        assert v.approved is False
        assert "bid" in v.detail

    def test_fallback_to_total_when_directional_zero(self) -> None:
        f = MarketQualityFilter(config=_cfg(min_orderbook_depth_usd=500.0))
        opp = _opportunity(
            ask_depth_usd=Decimal("0"),
            bid_depth_usd=Decimal("0"),
            depth_usd=Decimal("5000"),
        )
        v = f.check(opp, side=Side.BUY)
        assert v.approved is True

    def test_no_side_uses_total_depth(self) -> None:
        f = MarketQualityFilter(config=_cfg(min_orderbook_depth_usd=500.0))
        opp = _opportunity(
            ask_depth_usd=Decimal("100"),
            bid_depth_usd=Decimal("100"),
            depth_usd=Decimal("5000"),
        )
        v = f.check(opp)  # no side
        assert v.approved is True

    def test_detail_includes_condition_id(self) -> None:
        f = MarketQualityFilter(config=_cfg(min_orderbook_depth_usd=500.0))
        v = f.check(_opportunity(
            condition_id="test_cond",
            depth_usd=Decimal("10"),
        ))
        assert v.approved is False
        assert "test_cond" in v.detail


# ── Spread ─────────────────────────────────────────────────────


class TestSpread:
    def test_narrow_spread_passes(self) -> None:
        f = MarketQualityFilter(config=_cfg(max_spread=0.10))
        v = f.check(_opportunity(spread=Decimal("0.05")))
        assert v.approved is True

    def test_wide_spread_rejected(self) -> None:
        f = MarketQualityFilter(config=_cfg(max_spread=0.10))
        v = f.check(_opportunity(spread=Decimal("0.15")))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.SPREAD_TOO_WIDE

    def test_spread_at_boundary_passes(self) -> None:
        f = MarketQualityFilter(config=_cfg(max_spread=0.10))
        v = f.check(_opportunity(spread=Decimal("0.10")))
        assert v.approved is True

    def test_none_spread_passes(self) -> None:
        f = MarketQualityFilter(config=_cfg())
        v = f.check(_opportunity(spread=None))
        assert v.approved is True

    def test_detail_includes_values(self) -> None:
        f = MarketQualityFilter(config=_cfg(max_spread=0.10))
        v = f.check(_opportunity(spread=Decimal("0.20")))
        assert "0.20" in v.detail or "0.2" in v.detail


# ── UMA Disputes ──────────────────────────────────────────────


class TestDisputeCheck:
    def test_no_oracle_monitor_passes(self) -> None:
        f = MarketQualityFilter(config=_cfg(), oracle_monitor=None)
        v = f.check(_opportunity())
        assert v.approved is True

    def test_no_dispute_passes(self) -> None:
        oracle = OracleMonitor(config=OracleConfig(enabled=True))
        f = MarketQualityFilter(config=_cfg(), oracle_monitor=oracle)
        v = f.check(_opportunity())
        assert v.approved is True

    def test_active_dispute_rejected(self) -> None:
        oracle = _oracle_with_dispute("cond1")
        f = MarketQualityFilter(config=_cfg(), oracle_monitor=oracle)
        v = f.check(_opportunity(condition_id="cond1"))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.UMA_EXPOSURE_LIMIT
        assert "dispute" in v.detail.lower()

    def test_dispute_on_other_market_passes(self) -> None:
        oracle = _oracle_with_dispute("cond_other")
        f = MarketQualityFilter(config=_cfg(), oracle_monitor=oracle)
        v = f.check(_opportunity(condition_id="cond1"))
        assert v.approved is True

    def test_settled_dispute_passes(self) -> None:
        """Settled (not disputed) proposals should not trigger rejection."""
        oracle = OracleMonitor(config=OracleConfig(enabled=True))
        oracle._proposals["cond1"] = OracleProposal(
            condition_id="cond1",
            state=OracleProposalState.SETTLED,
        )
        f = MarketQualityFilter(config=_cfg(), oracle_monitor=oracle)
        v = f.check(_opportunity(condition_id="cond1"))
        assert v.approved is True


# ── Fee Rate ──────────────────────────────────────────────────


class TestFeeRate:
    def test_zero_fee_passes(self) -> None:
        f = MarketQualityFilter(config=_cfg(max_fee_rate_bps=0))
        v = f.check(_opportunity(fee_rate_bps=0))
        assert v.approved is True

    def test_nonzero_fee_rejected(self) -> None:
        f = MarketQualityFilter(config=_cfg(max_fee_rate_bps=0))
        v = f.check(_opportunity(fee_rate_bps=315))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.FEE_RATE_TOO_HIGH

    def test_fee_at_configured_max_passes(self) -> None:
        f = MarketQualityFilter(config=_cfg(max_fee_rate_bps=100))
        v = f.check(_opportunity(fee_rate_bps=100))
        assert v.approved is True

    def test_fee_above_configured_max_rejected(self) -> None:
        f = MarketQualityFilter(config=_cfg(max_fee_rate_bps=100))
        v = f.check(_opportunity(fee_rate_bps=315))
        assert v.approved is False

    def test_detail_includes_bps(self) -> None:
        f = MarketQualityFilter(config=_cfg(max_fee_rate_bps=0))
        v = f.check(_opportunity(fee_rate_bps=315))
        assert "315bps" in v.detail
        assert "0bps" in v.detail


# ── Integration ──────────────────────────────────────────────


class TestIntegration:
    def test_good_market_passes_all_checks(self) -> None:
        """A healthy market with good liquidity, no fees, no disputes."""
        oracle = OracleMonitor(config=OracleConfig(enabled=True))
        f = MarketQualityFilter(config=_cfg(), oracle_monitor=oracle)
        opp = _opportunity(
            depth_usd=Decimal("10000"),
            spread=Decimal("0.02"),
            fee_rate_bps=0,
        )
        v = f.check(opp, side=Side.BUY)
        assert v.approved is True

    def test_bad_market_fails_multiple_checks(self) -> None:
        """A market with multiple problems."""
        info = MarketInfo(
            condition_id="cond1",
            active=True,
            closed=False,
            flagged=True,
            accepting_orders=True,
        )
        oracle = _oracle_with_dispute("cond1")
        f = MarketQualityFilter(config=_cfg(), oracle_monitor=oracle)
        opp = _opportunity(
            condition_id="cond1",
            market_info=info,
            depth_usd=Decimal("50"),
            spread=Decimal("0.25"),
            fee_rate_bps=315,
        )
        # Single check returns first failure
        v = f.check(opp)
        assert v.approved is False
        assert v.reason == RiskRejectionReason.MARKET_NOT_ACTIVE

        # check_all returns all failures
        rejections = f.check_all(opp)
        assert len(rejections) == 5

    def test_check_priority_order(self) -> None:
        """Checks run in defined order: status → depth → spread → disputes → fees."""
        oracle = _oracle_with_dispute("cond1")
        f = MarketQualityFilter(config=_cfg(), oracle_monitor=oracle)

        # Market status is fine, depth is bad
        opp = _opportunity(
            condition_id="cond1",
            depth_usd=Decimal("10"),
            spread=Decimal("0.50"),
            fee_rate_bps=315,
        )
        v = f.check(opp)
        assert v.reason == RiskRejectionReason.ORDERBOOK_DEPTH
