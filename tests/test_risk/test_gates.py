"""Tests for risk gate functions — stateless checks returning RiskVerdict."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.core.config import KillSwitchConfig, OracleConfig, RiskConfig
from src.core.types import (
    FeedEvent,
    FeedEventType,
    FeedType,
    MarketCategory,
    MarketInfo,
    MarketOpportunity,
    MatchResult,
    OracleProposal,
    OracleProposalState,
    OutcomeType,
    Position,
    RiskRejectionReason,
    Side,
    Signal,
    SignalDirection,
    TradeAction,
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
from src.risk.pnl import PnLTracker

# ── Helpers ─────────────────────────────────────────────────────


def _tokens() -> list[dict[str, Any]]:
    return [{"token_id": "0xyes", "outcome": "Yes"}]


def _signal(
    condition_id: str = "cond1",
    depth_usd: Decimal = Decimal("5000"),
    spread: Decimal | None = Decimal("0.05"),
) -> Signal:
    return Signal(
        match=MatchResult(
            feed_event=FeedEvent(
                feed_type=FeedType.ECONOMIC,
                event_type=FeedEventType.DATA_RELEASED,
                indicator="CPI",
                outcome_type=OutcomeType.NUMERIC,
            ),
            opportunity=MarketOpportunity(
                condition_id=condition_id,
                tokens=_tokens(),
                category=MarketCategory.ECONOMIC,
                depth_usd=depth_usd,
                spread=spread,
                best_bid=Decimal("0.45"),
                best_ask=Decimal("0.50"),
            ),
        ),
        direction=SignalDirection.BUY,
        fair_value=Decimal("0.90"),
    )


def _action(
    condition_id: str = "cond1",
    price: Decimal = Decimal("0.50"),
    size: Decimal = Decimal("100"),
    depth_usd: Decimal = Decimal("5000"),
    spread: Decimal | None = Decimal("0.05"),
) -> TradeAction:
    return TradeAction(
        signal=_signal(condition_id, depth_usd, spread),
        token_id="0xyes",
        side=Side.BUY,
        price=price,
        size=size,
    )


def _cfg(**overrides: object) -> RiskConfig:
    defaults: dict[str, object] = {
        "max_daily_loss_usd": 500.0,
        "max_position_usd": 5000.0,
        "max_bankroll_pct_per_event": 0.20,
        "bankroll_usd": 10000.0,
        "max_concurrent_positions": 10,
        "min_orderbook_depth_usd": 500.0,
        "max_spread": 0.10,
    }
    defaults.update(overrides)
    return RiskConfig(**defaults)  # type: ignore[arg-type]


# ── Kill Switch ────────────────────────────────────────────────


class TestKillSwitch:
    def test_approved_when_not_killed(self) -> None:
        v = check_kill_switch(False)
        assert v.approved is True

    def test_rejected_when_killed(self) -> None:
        v = check_kill_switch(True)
        assert v.approved is False

    def test_reason(self) -> None:
        v = check_kill_switch(True)
        assert v.reason == RiskRejectionReason.KILL_SWITCH_ACTIVE


# ── Daily Loss ─────────────────────────────────────────────────


class TestDailyLoss:
    def test_approved_within_limit(self) -> None:
        pnl = PnLTracker()
        pnl.realized_today = Decimal("-400")
        v = check_daily_loss(pnl, _cfg(max_daily_loss_usd=500.0))
        assert v.approved is True

    def test_rejected_over_limit(self) -> None:
        pnl = PnLTracker()
        pnl.realized_today = Decimal("-600")
        v = check_daily_loss(pnl, _cfg(max_daily_loss_usd=500.0))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.DAILY_LOSS_LIMIT

    def test_boundary_at_limit(self) -> None:
        """Exactly at limit should pass (not strictly exceeded)."""
        pnl = PnLTracker()
        pnl.realized_today = Decimal("-500")
        v = check_daily_loss(pnl, _cfg(max_daily_loss_usd=500.0))
        assert v.approved is True


# ── Position Concentration ─────────────────────────────────────


class TestPositionConcentration:
    def test_approved_within_limit(self) -> None:
        action = _action(price=Decimal("0.50"), size=Decimal("100"))
        v = check_position_concentration(action, {}, _cfg())
        assert v.approved is True

    def test_rejected_over_limit(self) -> None:
        # bankroll=10000, pct=0.20 → limit=2000
        action = _action(price=Decimal("0.50"), size=Decimal("5000"))
        # 0.50*5000 = 2500 > 2000
        v = check_position_concentration(action, {}, _cfg())
        assert v.approved is False
        assert v.reason == RiskRejectionReason.POSITION_CONCENTRATION

    def test_includes_existing_exposure(self) -> None:
        existing = {
            "0xa": Position(
                token_id="0xa",
                condition_id="cond1",
                side=Side.BUY,
                entry_price=Decimal("0.50"),
                size=Decimal("3000"),  # exposure = 1500
            ),
        }
        # New: 0.50 * 1500 = 750, total = 2250 > 2000
        action = _action(price=Decimal("0.50"), size=Decimal("1500"))
        v = check_position_concentration(action, existing, _cfg())
        assert v.approved is False


# ── Max Concurrent Positions ──────────────────────────────────


class TestMaxConcurrentPositions:
    def test_below_limit(self) -> None:
        positions: dict[str, Position] = {}
        v = check_max_concurrent_positions(positions, _cfg(max_concurrent_positions=10))
        assert v.approved is True

    def test_at_limit(self) -> None:
        positions = {
            f"tok{i}": Position(token_id=f"tok{i}", side=Side.BUY) for i in range(10)
        }
        v = check_max_concurrent_positions(positions, _cfg(max_concurrent_positions=10))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.MAX_CONCURRENT_POSITIONS


# ── Orderbook Depth ───────────────────────────────────────────


class TestOrderbookDepth:
    def test_sufficient_depth(self) -> None:
        action = _action(depth_usd=Decimal("5000"))
        v = check_orderbook_depth(action, _cfg(min_orderbook_depth_usd=500.0))
        assert v.approved is True

    def test_insufficient_depth(self) -> None:
        action = _action(depth_usd=Decimal("100"))
        v = check_orderbook_depth(action, _cfg(min_orderbook_depth_usd=500.0))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.ORDERBOOK_DEPTH


# ── Spread ─────────────────────────────────────────────────────


class TestSpread:
    def test_narrow_spread(self) -> None:
        action = _action(spread=Decimal("0.05"))
        v = check_spread(action, _cfg(max_spread=0.10))
        assert v.approved is True

    def test_wide_spread(self) -> None:
        action = _action(spread=Decimal("0.15"))
        v = check_spread(action, _cfg(max_spread=0.10))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.SPREAD_TOO_WIDE

    def test_none_spread_passes(self) -> None:
        action = _action(spread=None)
        v = check_spread(action, _cfg())
        assert v.approved is True


# ── Reason / Detail ───────────────────────────────────────────


class TestReasonDetail:
    def test_rejection_has_detail(self) -> None:
        v = check_kill_switch(True)
        assert v.detail != ""

    def test_approval_no_reason(self) -> None:
        v = check_kill_switch(False)
        assert v.reason is None


# ── Oracle Risk ──────────────────────────────────────────────


class TestOracleRisk:
    def test_approved_no_match(self) -> None:
        cfg = KillSwitchConfig(oracle_blacklist_patterns=["at discretion of"])
        v = check_oracle_risk("Will CPI exceed 3.0% in January?", cfg)
        assert v.approved is True

    def test_rejected_at_discretion_of(self) -> None:
        cfg = KillSwitchConfig()
        v = check_oracle_risk(
            "Resolution at discretion of the committee", cfg,
        )
        assert v.approved is False
        assert v.reason == RiskRejectionReason.ORACLE_RISK

    def test_rejected_as_determined_by(self) -> None:
        cfg = KillSwitchConfig()
        v = check_oracle_risk(
            "Winner as determined by the panel", cfg,
        )
        assert v.approved is False
        assert v.reason == RiskRejectionReason.ORACLE_RISK

    def test_case_insensitive(self) -> None:
        cfg = KillSwitchConfig(
            oracle_blacklist_patterns=["subject to interpretation"],
        )
        v = check_oracle_risk("This is SUBJECT TO INTERPRETATION by experts", cfg)
        assert v.approved is False

    def test_empty_patterns_passes(self) -> None:
        cfg = KillSwitchConfig(oracle_blacklist_patterns=[])
        v = check_oracle_risk("at discretion of someone", cfg)
        assert v.approved is True


# ── UMA Exposure ─────────────────────────────────────────────


class TestUmaExposure:
    def test_approved_no_dispute(self) -> None:
        proposals: dict[str, OracleProposal] = {}
        v = check_uma_exposure(_action(), {}, proposals, _cfg())
        assert v.approved is True

    def test_rejected_when_disputed(self) -> None:
        proposals = {
            "cond1": OracleProposal(
                condition_id="cond1",
                state=OracleProposalState.DISPUTED,
            ),
        }
        cfg = _cfg()
        cfg.oracle = OracleConfig(dispute_auto_reject=True)
        v = check_uma_exposure(_action(), {}, proposals, cfg)
        assert v.approved is False
        assert v.reason == RiskRejectionReason.UMA_EXPOSURE_LIMIT

    def test_approved_within_limit(self) -> None:
        proposals = {
            "cond1": OracleProposal(
                condition_id="cond1",
                state=OracleProposalState.PROPOSED,
            ),
        }
        cfg = _cfg()
        cfg.oracle = OracleConfig(
            max_uma_exposure_usd=5000.0,
            max_uma_exposure_pct=0.50,
        )
        # action: 0.50 * 100 = 50 < 5000
        v = check_uma_exposure(_action(), {}, proposals, cfg)
        assert v.approved is True

    def test_rejected_over_usd_limit(self) -> None:
        cfg = _cfg()
        cfg.oracle = OracleConfig(
            max_uma_exposure_usd=40.0,
            max_uma_exposure_pct=1.0,  # pct limit = 10000 (not binding)
        )
        # action: 0.50 * 100 = 50 > 40
        v = check_uma_exposure(_action(), {}, {}, cfg)
        assert v.approved is False
        assert v.reason == RiskRejectionReason.UMA_EXPOSURE_LIMIT

    def test_rejected_over_pct_limit(self) -> None:
        cfg = _cfg(bankroll_usd=1000.0)
        cfg.oracle = OracleConfig(
            max_uma_exposure_usd=99999.0,  # usd limit not binding
            max_uma_exposure_pct=0.01,  # pct limit = 10
        )
        # action: 0.50 * 100 = 50 > 10
        v = check_uma_exposure(_action(), {}, {}, cfg)
        assert v.approved is False

    def test_uses_stricter_limit(self) -> None:
        cfg = _cfg(bankroll_usd=10000.0)
        cfg.oracle = OracleConfig(
            max_uma_exposure_usd=100.0,  # usd = 100
            max_uma_exposure_pct=0.005,  # pct = 50
        )
        # action: 0.50 * 100 = 50 — within pct(50) but fails at usd(100)?
        # No, 50 < 100 — passes usd but 50 <= 50 exact boundary passes
        # Let's make it clearly breach: size=300 → 0.50*300=150 > min(100,50)=50
        v = check_uma_exposure(
            _action(size=Decimal("300")), {}, {}, cfg,
        )
        assert v.approved is False

    def test_empty_condition_id_passes(self) -> None:
        action = _action(condition_id="")
        cfg = _cfg()
        cfg.oracle = OracleConfig(max_uma_exposure_usd=1.0)
        v = check_uma_exposure(action, {}, {}, cfg)
        assert v.approved is True

    def test_detail_message_includes_amounts(self) -> None:
        cfg = _cfg()
        cfg.oracle = OracleConfig(
            max_uma_exposure_usd=10.0,
            max_uma_exposure_pct=1.0,
        )
        v = check_uma_exposure(_action(), {}, {}, cfg)
        assert v.approved is False
        assert "$" in v.detail
        assert "limit" in v.detail.lower()


# ── Directional Orderbook Depth ──────────────────────────────


class TestOrderbookDepthDirectional:
    def _action_with_depth(
        self,
        side: Side = Side.BUY,
        bid_depth: Decimal = Decimal("0"),
        ask_depth: Decimal = Decimal("0"),
        total_depth: Decimal = Decimal("5000"),
    ) -> TradeAction:
        sig = _signal(depth_usd=total_depth)
        sig.match.opportunity.bid_depth_usd = bid_depth
        sig.match.opportunity.ask_depth_usd = ask_depth
        return TradeAction(
            signal=sig,
            token_id="0xyes",
            side=side,
            price=Decimal("0.50"),
            size=Decimal("100"),
        )

    def test_buy_uses_ask_depth(self) -> None:
        """BUY side checks ask depth (we buy from asks)."""
        action = self._action_with_depth(
            side=Side.BUY,
            ask_depth=Decimal("600"),
            bid_depth=Decimal("100"),
            total_depth=Decimal("700"),
        )
        v = check_orderbook_depth(action, _cfg(min_orderbook_depth_usd=500.0))
        assert v.approved is True

    def test_buy_rejects_low_ask_depth(self) -> None:
        action = self._action_with_depth(
            side=Side.BUY,
            ask_depth=Decimal("200"),
            bid_depth=Decimal("5000"),
            total_depth=Decimal("5200"),
        )
        v = check_orderbook_depth(action, _cfg(min_orderbook_depth_usd=500.0))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.ORDERBOOK_DEPTH

    def test_sell_uses_bid_depth(self) -> None:
        """SELL side checks bid depth (we sell into bids)."""
        action = self._action_with_depth(
            side=Side.SELL,
            bid_depth=Decimal("600"),
            ask_depth=Decimal("100"),
            total_depth=Decimal("700"),
        )
        v = check_orderbook_depth(action, _cfg(min_orderbook_depth_usd=500.0))
        assert v.approved is True

    def test_fallback_to_total_when_zero(self) -> None:
        """Falls back to total depth when directional depth is 0."""
        action = self._action_with_depth(
            side=Side.BUY,
            ask_depth=Decimal("0"),
            bid_depth=Decimal("0"),
            total_depth=Decimal("5000"),
        )
        v = check_orderbook_depth(action, _cfg(min_orderbook_depth_usd=500.0))
        assert v.approved is True


# ── Market Status ─────────────────────────────────────────────


class TestMarketStatus:
    def _action_with_market_info(
        self,
        market_info: MarketInfo | None = None,
    ) -> TradeAction:
        sig = _signal()
        sig.match.opportunity.market_info = market_info
        return TradeAction(
            signal=sig,
            token_id="0xyes",
            side=Side.BUY,
            price=Decimal("0.50"),
            size=Decimal("100"),
        )

    def test_active_market_passes(self) -> None:
        info = MarketInfo(
            condition_id="cond1", active=True, closed=False,
            accepting_orders=True, flagged=False,
        )
        v = check_market_status(self._action_with_market_info(info), _cfg())
        assert v.approved is True

    def test_none_market_info_passes(self) -> None:
        v = check_market_status(self._action_with_market_info(None), _cfg())
        assert v.approved is True

    def test_inactive_market_rejected(self) -> None:
        info = MarketInfo(condition_id="cond1", active=False)
        v = check_market_status(self._action_with_market_info(info), _cfg())
        assert v.approved is False
        assert v.reason == RiskRejectionReason.MARKET_NOT_ACTIVE

    def test_closed_market_rejected(self) -> None:
        info = MarketInfo(condition_id="cond1", active=True, closed=True)
        v = check_market_status(self._action_with_market_info(info), _cfg())
        assert v.approved is False
        assert v.reason == RiskRejectionReason.MARKET_NOT_ACTIVE

    def test_flagged_market_rejected(self) -> None:
        info = MarketInfo(condition_id="cond1", flagged=True)
        v = check_market_status(self._action_with_market_info(info), _cfg())
        assert v.approved is False
        assert v.reason == RiskRejectionReason.MARKET_NOT_ACTIVE

    def test_not_accepting_orders_rejected(self) -> None:
        info = MarketInfo(condition_id="cond1", accepting_orders=False)
        v = check_market_status(self._action_with_market_info(info), _cfg())
        assert v.approved is False
        assert v.reason == RiskRejectionReason.MARKET_NOT_ACTIVE
        assert "not accepting orders" in v.detail


# ── Fee Rate ──────────────────────────────────────────────────


class TestFeeRate:
    def _action_with_fee(
        self,
        fee_bps: int = 0,
        estimated_profit: Decimal = Decimal("50"),
    ) -> TradeAction:
        sig = _signal()
        sig.match.opportunity.fee_rate_bps = fee_bps
        return TradeAction(
            signal=sig,
            token_id="0xyes",
            side=Side.BUY,
            price=Decimal("0.50"),
            size=Decimal("100"),
            estimated_profit_usd=estimated_profit,
        )

    def test_zero_fee_passes(self) -> None:
        v = check_fee_rate(self._action_with_fee(fee_bps=0), _cfg())
        assert v.approved is True

    def test_nonzero_fee_rejected(self) -> None:
        """Market with fees rejected when max_fee_rate_bps=0."""
        v = check_fee_rate(
            self._action_with_fee(fee_bps=315),
            _cfg(max_fee_rate_bps=0),
        )
        assert v.approved is False
        assert v.reason == RiskRejectionReason.FEE_RATE_TOO_HIGH

    def test_fee_at_boundary_passes(self) -> None:
        """Fee exactly at max should pass."""
        v = check_fee_rate(
            self._action_with_fee(fee_bps=100),
            _cfg(max_fee_rate_bps=100),
        )
        assert v.approved is True

    def test_override_passes_with_high_profit(self) -> None:
        """High-profit trade bypasses fee limit."""
        v = check_fee_rate(
            self._action_with_fee(fee_bps=315, estimated_profit=Decimal("200")),
            _cfg(max_fee_rate_bps=0, fee_override_min_profit_usd=100.0),
        )
        assert v.approved is True

    def test_override_fails_with_low_profit(self) -> None:
        """Low-profit trade does NOT bypass fee limit."""
        v = check_fee_rate(
            self._action_with_fee(fee_bps=315, estimated_profit=Decimal("50")),
            _cfg(max_fee_rate_bps=0, fee_override_min_profit_usd=100.0),
        )
        assert v.approved is False
        assert v.reason == RiskRejectionReason.FEE_RATE_TOO_HIGH


# ── Position Size ────────────────────────────────────────────


class TestPositionSize:
    def test_within_limit(self) -> None:
        """Trade value within max_position_usd passes."""
        action = _action(price=Decimal("0.50"), size=Decimal("100"))
        # 0.50 * 100 = 50 < 5000
        v = check_position_size(action, _cfg(max_position_usd=5000.0))
        assert v.approved is True

    def test_exceeds_limit(self) -> None:
        """Trade value over max_position_usd rejected."""
        action = _action(price=Decimal("0.50"), size=Decimal("20000"))
        # 0.50 * 20000 = 10000 > 5000
        v = check_position_size(action, _cfg(max_position_usd=5000.0))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.POSITION_SIZE_EXCEEDED

    def test_at_boundary_passes(self) -> None:
        """Trade value exactly at limit passes."""
        action = _action(price=Decimal("0.50"), size=Decimal("10000"))
        # 0.50 * 10000 = 5000 == 5000
        v = check_position_size(action, _cfg(max_position_usd=5000.0))
        assert v.approved is True

    def test_detail_message(self) -> None:
        action = _action(price=Decimal("0.50"), size=Decimal("20000"))
        v = check_position_size(action, _cfg(max_position_usd=5000.0))
        assert "$" in v.detail
        assert "max position" in v.detail.lower()
