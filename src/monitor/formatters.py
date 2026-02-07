"""Pure functions that convert domain events into AlertMessage objects."""

from __future__ import annotations

from src.core.types import (
    ArbEvent,
    ArbEventType,
    FeedEvent,
    FeedEventType,
    OracleAlert,
    OracleEventType,
    RiskEvent,
    RiskEventType,
)
from src.monitor.types import AlertMessage, Severity

# ── Severity mappings ───────────────────────────────────────────

_ARB_SEVERITY: dict[ArbEventType, Severity] = {
    ArbEventType.SIGNAL_GENERATED: Severity.DEBUG,
    ArbEventType.TRADE_EXECUTED: Severity.INFO,
    ArbEventType.TRADE_FAILED: Severity.WARNING,
    ArbEventType.TRADE_SKIPPED: Severity.DEBUG,
    ArbEventType.RISK_REJECTED: Severity.DEBUG,
    ArbEventType.ENGINE_STARTED: Severity.INFO,
    ArbEventType.ENGINE_STOPPED: Severity.INFO,
}

_RISK_SEVERITY: dict[RiskEventType, Severity] = {
    RiskEventType.KILL_SWITCH_TRIGGERED: Severity.CRITICAL,
    RiskEventType.KILL_SWITCH_RESET: Severity.INFO,
    RiskEventType.RISK_GATE_REJECTED: Severity.DEBUG,
    RiskEventType.POSITION_OPENED: Severity.DEBUG,
    RiskEventType.POSITION_CLOSED: Severity.DEBUG,
    RiskEventType.DAILY_PNL_UPDATED: Severity.DEBUG,
    RiskEventType.DISPUTE_DETECTED: Severity.CRITICAL,
    RiskEventType.WHALE_ACTIVITY_DETECTED: Severity.WARNING,
}

_FEED_SEVERITY: dict[FeedEventType, Severity] = {
    FeedEventType.DATA_RELEASED: Severity.DEBUG,
    FeedEventType.FEED_CONNECTED: Severity.DEBUG,
    FeedEventType.FEED_DISCONNECTED: Severity.WARNING,
    FeedEventType.FEED_ERROR: Severity.WARNING,
}

_ORACLE_SEVERITY: dict[OracleEventType, Severity] = {
    OracleEventType.PROPOSAL_DETECTED: Severity.DEBUG,
    OracleEventType.DISPUTE_DETECTED: Severity.CRITICAL,
    OracleEventType.SETTLEMENT_DETECTED: Severity.INFO,
    OracleEventType.WHALE_ACTIVITY_DETECTED: Severity.WARNING,
    OracleEventType.HIGH_ORACLE_RISK: Severity.WARNING,
}


# ── Formatters ──────────────────────────────────────────────────


def format_arb_event(event: ArbEvent) -> AlertMessage:
    """Convert an ArbEvent to an AlertMessage."""
    severity = _ARB_SEVERITY.get(event.event_type, Severity.DEBUG)
    fields: dict[str, str] = {}

    if event.action is not None:
        fields["token_id"] = event.action.token_id
        fields["side"] = event.action.side.value
        fields["price"] = str(event.action.price)
        fields["size"] = str(event.action.size)
        fields["est_profit"] = str(event.action.estimated_profit_usd)

    if event.result is not None:
        fields["fill_price"] = str(event.result.fill_price or "")
        fields["fill_size"] = str(event.result.fill_size or "")
        fields["success"] = str(event.result.success)

    if event.signal is not None:
        fields["confidence"] = str(event.signal.confidence)
        fields["edge"] = str(event.signal.edge)

    title = event.event_type.value
    body = event.reason or ""

    return AlertMessage(
        severity=severity,
        title=title,
        body=body,
        fields=fields,
        source_event_type=event.event_type.value,
        timestamp=event.timestamp,
        raw=event.model_dump(mode="json"),
    )


def format_risk_event(event: RiskEvent) -> AlertMessage:
    """Convert a RiskEvent to an AlertMessage."""
    severity = _RISK_SEVERITY.get(event.event_type, Severity.DEBUG)
    fields: dict[str, str] = {}

    if event.position is not None:
        fields["token_id"] = event.position.token_id
        fields["condition_id"] = event.position.condition_id
        fields["entry_price"] = str(event.position.entry_price)
        fields["size"] = str(event.position.size)

    if event.verdict is not None:
        fields["approved"] = str(event.verdict.approved)
        if event.verdict.reason is not None:
            fields["rejection_reason"] = event.verdict.reason.value

    if event.daily_pnl is not None:
        fields["daily_pnl"] = str(event.daily_pnl)

    title = event.event_type.value
    body = event.reason or ""

    return AlertMessage(
        severity=severity,
        title=title,
        body=body,
        fields=fields,
        source_event_type=event.event_type.value,
        timestamp=event.timestamp,
        raw=event.model_dump(mode="json"),
    )


def format_feed_event(event: FeedEvent) -> AlertMessage:
    """Convert a FeedEvent to an AlertMessage."""
    severity = _FEED_SEVERITY.get(event.event_type, Severity.DEBUG)
    fields: dict[str, str] = {
        "feed_type": event.feed_type.value,
        "indicator": event.indicator,
    }
    if event.value:
        fields["value"] = event.value

    title = f"{event.feed_type.value} {event.event_type.value}"
    body = event.indicator if event.indicator else ""

    return AlertMessage(
        severity=severity,
        title=title,
        body=body,
        fields=fields,
        source_event_type=event.event_type.value,
        timestamp=event.received_at or event.released_at,
        raw=event.model_dump(mode="json"),
    )


def format_oracle_alert(alert: OracleAlert) -> AlertMessage:
    """Convert an OracleAlert to an AlertMessage."""
    severity = _ORACLE_SEVERITY.get(alert.event_type, Severity.DEBUG)
    fields: dict[str, str] = {
        "condition_id": alert.condition_id,
    }

    if alert.proposal is not None:
        fields["proposal_state"] = alert.proposal.state.value
        fields["proposer"] = alert.proposal.proposer

    if alert.whale_activity is not None:
        fields["whale_address"] = alert.whale_activity.address
        fields["whale_action"] = alert.whale_activity.action

    if alert.held_position_exposure:
        fields["exposure_usd"] = str(alert.held_position_exposure)

    title = alert.event_type.value
    body = alert.reason or ""

    return AlertMessage(
        severity=severity,
        title=title,
        body=body,
        fields=fields,
        source_event_type=alert.event_type.value,
        timestamp=alert.timestamp,
        raw=alert.model_dump(mode="json"),
    )
