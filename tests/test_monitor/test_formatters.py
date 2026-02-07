"""Tests for monitor formatters — severity mapping and field extraction."""

from __future__ import annotations

from decimal import Decimal

from src.core.types import (
    ArbEvent,
    ArbEventType,
    ExecutionResult,
    FeedEvent,
    FeedEventType,
    FeedType,
    MatchResult,
    MarketOpportunity,
    OracleAlert,
    OracleEventType,
    OracleProposal,
    OracleProposalState,
    OrderResponse,
    OutcomeType,
    RiskEvent,
    RiskEventType,
    RiskRejectionReason,
    RiskVerdict,
    Side,
    Signal,
    SignalDirection,
    TradeAction,
    WhaleActivity,
    Position,
)
from src.monitor.formatters import (
    format_arb_event,
    format_feed_event,
    format_oracle_alert,
    format_risk_event,
)
from src.monitor.types import Severity


# ── Helpers ─────────────────────────────────────────────────────


def _signal(**kw: object) -> Signal:
    defaults: dict[str, object] = {
        "match": MatchResult(
            feed_event=FeedEvent(
                feed_type=FeedType.ECONOMIC,
                event_type=FeedEventType.DATA_RELEASED,
            ),
            opportunity=MarketOpportunity(condition_id="cond1"),
        ),
        "confidence": 0.99,
        "edge": Decimal("0.05"),
        "direction": SignalDirection.BUY,
        "fair_value": Decimal("0.95"),
        "current_price": Decimal("0.90"),
    }
    defaults.update(kw)
    return Signal(**defaults)  # type: ignore[arg-type]


def _action(**kw: object) -> TradeAction:
    defaults: dict[str, object] = {
        "signal": _signal(),
        "token_id": "tok_abc",
        "side": Side.BUY,
        "price": Decimal("0.90"),
        "size": Decimal("100"),
        "estimated_profit_usd": Decimal("10"),
    }
    defaults.update(kw)
    return TradeAction(**defaults)  # type: ignore[arg-type]


def _result(**kw: object) -> ExecutionResult:
    defaults: dict[str, object] = {
        "action": _action(),
        "success": True,
        "fill_price": Decimal("0.90"),
        "fill_size": Decimal("100"),
        "executed_at": 1000.0,
    }
    defaults.update(kw)
    return ExecutionResult(**defaults)  # type: ignore[arg-type]


# ── ArbEvent Formatting ────────────────────────────────────────


class TestFormatArbEvent:
    def test_trade_executed_severity(self) -> None:
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_EXECUTED,
            action=_action(),
            result=_result(),
            timestamp=1000.0,
        )
        msg = format_arb_event(ev)
        assert msg.severity == Severity.INFO

    def test_trade_failed_severity(self) -> None:
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_FAILED,
            reason="timeout",
            timestamp=1000.0,
        )
        msg = format_arb_event(ev)
        assert msg.severity == Severity.WARNING

    def test_signal_generated_severity(self) -> None:
        ev = ArbEvent(
            event_type=ArbEventType.SIGNAL_GENERATED,
            signal=_signal(),
            timestamp=1000.0,
        )
        msg = format_arb_event(ev)
        assert msg.severity == Severity.DEBUG

    def test_trade_skipped_severity(self) -> None:
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_SKIPPED,
            timestamp=1000.0,
        )
        msg = format_arb_event(ev)
        assert msg.severity == Severity.DEBUG

    def test_risk_rejected_severity(self) -> None:
        ev = ArbEvent(
            event_type=ArbEventType.RISK_REJECTED,
            timestamp=1000.0,
        )
        msg = format_arb_event(ev)
        assert msg.severity == Severity.DEBUG

    def test_action_fields_extracted(self) -> None:
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_EXECUTED,
            action=_action(),
            result=_result(),
            timestamp=1000.0,
        )
        msg = format_arb_event(ev)
        assert msg.fields["token_id"] == "tok_abc"
        assert msg.fields["side"] == "BUY"
        assert msg.fields["price"] == "0.90"
        assert msg.fields["size"] == "100"
        assert msg.fields["est_profit"] == "10"

    def test_result_fields_extracted(self) -> None:
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_EXECUTED,
            action=_action(),
            result=_result(),
            timestamp=1000.0,
        )
        msg = format_arb_event(ev)
        assert msg.fields["fill_price"] == "0.90"
        assert msg.fields["success"] == "True"

    def test_signal_fields_extracted(self) -> None:
        ev = ArbEvent(
            event_type=ArbEventType.SIGNAL_GENERATED,
            signal=_signal(confidence=0.98, edge=Decimal("0.07")),
            timestamp=1000.0,
        )
        msg = format_arb_event(ev)
        assert msg.fields["confidence"] == "0.98"
        assert msg.fields["edge"] == "0.07"

    def test_title_is_event_type(self) -> None:
        ev = ArbEvent(
            event_type=ArbEventType.ENGINE_STARTED,
            timestamp=1000.0,
        )
        msg = format_arb_event(ev)
        assert msg.title == "ENGINE_STARTED"

    def test_body_from_reason(self) -> None:
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_FAILED,
            reason="bad fill",
            timestamp=1000.0,
        )
        msg = format_arb_event(ev)
        assert msg.body == "bad fill"

    def test_raw_contains_full_dump(self) -> None:
        ev = ArbEvent(
            event_type=ArbEventType.ENGINE_STARTED,
            timestamp=1000.0,
        )
        msg = format_arb_event(ev)
        assert msg.raw["event_type"] == "ENGINE_STARTED"

    def test_source_event_type_set(self) -> None:
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_EXECUTED,
            timestamp=1000.0,
        )
        msg = format_arb_event(ev)
        assert msg.source_event_type == "TRADE_EXECUTED"

    def test_no_action_no_result_no_signal(self) -> None:
        ev = ArbEvent(
            event_type=ArbEventType.ENGINE_STOPPED,
            timestamp=1000.0,
        )
        msg = format_arb_event(ev)
        assert msg.fields == {}


# ── RiskEvent Formatting ───────────────────────────────────────


class TestFormatRiskEvent:
    def test_kill_switch_triggered_severity(self) -> None:
        ev = RiskEvent(
            event_type=RiskEventType.KILL_SWITCH_TRIGGERED,
            reason="too many losses",
            timestamp=1000.0,
        )
        msg = format_risk_event(ev)
        assert msg.severity == Severity.CRITICAL

    def test_dispute_detected_severity(self) -> None:
        ev = RiskEvent(
            event_type=RiskEventType.DISPUTE_DETECTED,
            timestamp=1000.0,
        )
        msg = format_risk_event(ev)
        assert msg.severity == Severity.CRITICAL

    def test_kill_switch_reset_severity(self) -> None:
        ev = RiskEvent(
            event_type=RiskEventType.KILL_SWITCH_RESET,
            timestamp=1000.0,
        )
        msg = format_risk_event(ev)
        assert msg.severity == Severity.INFO

    def test_risk_gate_rejected_severity(self) -> None:
        ev = RiskEvent(
            event_type=RiskEventType.RISK_GATE_REJECTED,
            timestamp=1000.0,
        )
        msg = format_risk_event(ev)
        assert msg.severity == Severity.DEBUG

    def test_position_fields_extracted(self) -> None:
        pos = Position(
            token_id="tok_x",
            condition_id="cond_y",
            entry_price=Decimal("0.85"),
            size=Decimal("200"),
        )
        ev = RiskEvent(
            event_type=RiskEventType.POSITION_OPENED,
            position=pos,
            timestamp=1000.0,
        )
        msg = format_risk_event(ev)
        assert msg.fields["token_id"] == "tok_x"
        assert msg.fields["condition_id"] == "cond_y"
        assert msg.fields["entry_price"] == "0.85"
        assert msg.fields["size"] == "200"

    def test_verdict_fields_extracted(self) -> None:
        verdict = RiskVerdict(
            approved=False,
            reason=RiskRejectionReason.DAILY_LOSS_LIMIT,
        )
        ev = RiskEvent(
            event_type=RiskEventType.RISK_GATE_REJECTED,
            verdict=verdict,
            timestamp=1000.0,
        )
        msg = format_risk_event(ev)
        assert msg.fields["approved"] == "False"
        assert msg.fields["rejection_reason"] == "DAILY_LOSS_LIMIT"

    def test_daily_pnl_field(self) -> None:
        ev = RiskEvent(
            event_type=RiskEventType.DAILY_PNL_UPDATED,
            daily_pnl=Decimal("-42.50"),
            timestamp=1000.0,
        )
        msg = format_risk_event(ev)
        assert msg.fields["daily_pnl"] == "-42.50"

    def test_whale_activity_severity(self) -> None:
        ev = RiskEvent(
            event_type=RiskEventType.WHALE_ACTIVITY_DETECTED,
            timestamp=1000.0,
        )
        msg = format_risk_event(ev)
        assert msg.severity == Severity.WARNING


# ── FeedEvent Formatting ───────────────────────────────────────


class TestFormatFeedEvent:
    def test_data_released_severity(self) -> None:
        ev = FeedEvent(
            feed_type=FeedType.ECONOMIC,
            event_type=FeedEventType.DATA_RELEASED,
            indicator="CPI",
            value="3.2",
            received_at=1000.0,
        )
        msg = format_feed_event(ev)
        assert msg.severity == Severity.DEBUG

    def test_feed_disconnected_severity(self) -> None:
        ev = FeedEvent(
            feed_type=FeedType.SPORTS,
            event_type=FeedEventType.FEED_DISCONNECTED,
            received_at=1000.0,
        )
        msg = format_feed_event(ev)
        assert msg.severity == Severity.WARNING

    def test_feed_error_severity(self) -> None:
        ev = FeedEvent(
            feed_type=FeedType.CRYPTO,
            event_type=FeedEventType.FEED_ERROR,
            received_at=1000.0,
        )
        msg = format_feed_event(ev)
        assert msg.severity == Severity.WARNING

    def test_title_includes_feed_type(self) -> None:
        ev = FeedEvent(
            feed_type=FeedType.ECONOMIC,
            event_type=FeedEventType.DATA_RELEASED,
            received_at=1000.0,
        )
        msg = format_feed_event(ev)
        assert "ECONOMIC" in msg.title
        assert "DATA_RELEASED" in msg.title

    def test_fields_extracted(self) -> None:
        ev = FeedEvent(
            feed_type=FeedType.CRYPTO,
            event_type=FeedEventType.DATA_RELEASED,
            indicator="BTC_USDT",
            value="100000",
            received_at=1000.0,
        )
        msg = format_feed_event(ev)
        assert msg.fields["feed_type"] == "CRYPTO"
        assert msg.fields["indicator"] == "BTC_USDT"
        assert msg.fields["value"] == "100000"

    def test_timestamp_from_received_at(self) -> None:
        ev = FeedEvent(
            feed_type=FeedType.ECONOMIC,
            event_type=FeedEventType.DATA_RELEASED,
            received_at=5555.0,
        )
        msg = format_feed_event(ev)
        assert msg.timestamp == 5555.0


# ── OracleAlert Formatting ─────────────────────────────────────


class TestFormatOracleAlert:
    def test_dispute_detected_severity(self) -> None:
        alert = OracleAlert(
            event_type=OracleEventType.DISPUTE_DETECTED,
            condition_id="cond_1",
            timestamp=1000.0,
        )
        msg = format_oracle_alert(alert)
        assert msg.severity == Severity.CRITICAL

    def test_whale_activity_severity(self) -> None:
        alert = OracleAlert(
            event_type=OracleEventType.WHALE_ACTIVITY_DETECTED,
            condition_id="cond_2",
            whale_activity=WhaleActivity(address="0xabc", action="vote"),
            timestamp=1000.0,
        )
        msg = format_oracle_alert(alert)
        assert msg.severity == Severity.WARNING

    def test_settlement_severity(self) -> None:
        alert = OracleAlert(
            event_type=OracleEventType.SETTLEMENT_DETECTED,
            condition_id="cond_3",
            timestamp=1000.0,
        )
        msg = format_oracle_alert(alert)
        assert msg.severity == Severity.INFO

    def test_proposal_fields(self) -> None:
        proposal = OracleProposal(
            condition_id="cond_1",
            proposer="0xprop",
            state=OracleProposalState.DISPUTED,
        )
        alert = OracleAlert(
            event_type=OracleEventType.DISPUTE_DETECTED,
            condition_id="cond_1",
            proposal=proposal,
            timestamp=1000.0,
        )
        msg = format_oracle_alert(alert)
        assert msg.fields["proposal_state"] == "DISPUTED"
        assert msg.fields["proposer"] == "0xprop"

    def test_whale_fields(self) -> None:
        alert = OracleAlert(
            event_type=OracleEventType.WHALE_ACTIVITY_DETECTED,
            condition_id="cond_2",
            whale_activity=WhaleActivity(address="0xwhale", action="dispute"),
            timestamp=1000.0,
        )
        msg = format_oracle_alert(alert)
        assert msg.fields["whale_address"] == "0xwhale"
        assert msg.fields["whale_action"] == "dispute"

    def test_exposure_field(self) -> None:
        alert = OracleAlert(
            event_type=OracleEventType.HIGH_ORACLE_RISK,
            condition_id="cond_3",
            held_position_exposure=Decimal("1500"),
            timestamp=1000.0,
        )
        msg = format_oracle_alert(alert)
        assert msg.fields["exposure_usd"] == "1500"

    def test_condition_id_always_present(self) -> None:
        alert = OracleAlert(
            event_type=OracleEventType.PROPOSAL_DETECTED,
            condition_id="cond_99",
            timestamp=1000.0,
        )
        msg = format_oracle_alert(alert)
        assert msg.fields["condition_id"] == "cond_99"
