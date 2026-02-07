"""Central alert dispatcher — routes events to channels with throttling."""

from __future__ import annotations

import time

import structlog

from src.core.types import ArbEvent, FeedEvent, OracleAlert, RiskEvent
from src.monitor.channels import NotificationChannel
from src.monitor.formatters import (
    format_arb_event,
    format_feed_event,
    format_oracle_alert,
    format_risk_event,
)
from src.monitor.types import AlertMessage, Severity

# Dedicated structured logger for decision records.
decision_logger = structlog.get_logger("decision_log")

logger = structlog.get_logger(__name__)


class AlertDispatcher:
    """Routes domain events to notification channels.

    - Every event is logged via *decision_logger* (full model dump).
    - DEBUG events are log-only — never sent to channels.
    - INFO/WARNING events are dispatched subject to per-event-type throttling.
    - CRITICAL events bypass the throttle and are dispatched immediately.
    """

    def __init__(
        self,
        channels: list[NotificationChannel] | None = None,
        throttle_secs: float = 30.0,
    ) -> None:
        self._channels: list[NotificationChannel] = channels or []
        self._throttle_secs = throttle_secs
        # Tracks the last dispatch time per source_event_type.
        self._last_sent: dict[str, float] = {}

    # ── Callback entry points ───────────────────────────────────

    async def on_arb_event(self, event: ArbEvent) -> None:
        msg = format_arb_event(event)
        await self._handle(msg)

    async def on_risk_event(self, event: RiskEvent) -> None:
        msg = format_risk_event(event)
        await self._handle(msg)

    async def on_feed_event(self, event: FeedEvent) -> None:
        msg = format_feed_event(event)
        await self._handle(msg)

    async def on_oracle_alert(self, alert: OracleAlert) -> None:
        msg = format_oracle_alert(alert)
        await self._handle(msg)

    # ── Direct send (used by daily summary, etc.) ───────────────

    async def send(self, msg: AlertMessage) -> None:
        """Dispatch an AlertMessage directly (bypasses throttle)."""
        self._log_decision(msg)
        await self._dispatch_to_channels(msg)

    # ── Internal routing ────────────────────────────────────────

    async def _handle(self, msg: AlertMessage) -> None:
        # Always log the full decision.
        self._log_decision(msg)

        # DEBUG → log only, do not dispatch to channels.
        if msg.severity == Severity.DEBUG:
            return

        # CRITICAL → bypass throttle.
        if msg.severity == Severity.CRITICAL:
            self._last_sent[msg.source_event_type] = time.monotonic()
            await self._dispatch_to_channels(msg)
            return

        # INFO / WARNING → throttle per event type.
        now = time.monotonic()
        last = self._last_sent.get(msg.source_event_type, -float("inf"))
        if now - last < self._throttle_secs:
            return

        self._last_sent[msg.source_event_type] = now
        await self._dispatch_to_channels(msg)

    def _log_decision(self, msg: AlertMessage) -> None:
        decision_logger.info(
            "decision",
            severity=msg.severity.name,
            title=msg.title,
            body=msg.body,
            source_event_type=msg.source_event_type,
            fields=msg.fields,
            raw=msg.raw,
        )

    async def _dispatch_to_channels(self, msg: AlertMessage) -> None:
        for ch in self._channels:
            try:
                await ch.send(msg)
            except Exception:
                logger.exception(
                    "channel_dispatch_error",
                    channel=type(ch).__name__,
                    title=msg.title,
                )

    # ── Lifecycle ───────────────────────────────────────────────

    async def close(self) -> None:
        for ch in self._channels:
            try:
                await ch.close()
            except Exception:
                logger.exception("channel_close_error", channel=type(ch).__name__)
