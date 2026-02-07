"""Tests for AlertDispatcher — routing, throttling, CRITICAL bypass, decision logging."""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from src.core.types import (
    ArbEvent,
    ArbEventType,
    FeedEvent,
    FeedEventType,
    FeedType,
    OracleAlert,
    OracleEventType,
    RiskEvent,
    RiskEventType,
)
from src.monitor.channels import NotificationChannel
from src.monitor.dispatcher import AlertDispatcher
from src.monitor.types import AlertMessage, Severity


# ── Helpers ─────────────────────────────────────────────────────


class FakeChannel(NotificationChannel):
    """In-memory channel for testing."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[AlertMessage] = []
        self._fail = fail
        self.closed = False

    async def send(self, msg: AlertMessage) -> bool:
        if self._fail:
            raise ConnectionError("fake error")
        self.sent.append(msg)
        return True

    async def close(self) -> None:
        self.closed = True


# ── Routing ─────────────────────────────────────────────────────


class TestRouting:
    async def test_arb_event_routed(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_EXECUTED,
            timestamp=1000.0,
        )
        await disp.on_arb_event(ev)
        assert len(ch.sent) == 1
        assert ch.sent[0].title == "TRADE_EXECUTED"

    async def test_risk_event_routed(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        ev = RiskEvent(
            event_type=RiskEventType.KILL_SWITCH_TRIGGERED,
            reason="max loss",
            timestamp=1000.0,
        )
        await disp.on_risk_event(ev)
        assert len(ch.sent) == 1
        assert ch.sent[0].severity == Severity.CRITICAL

    async def test_feed_event_routed(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        ev = FeedEvent(
            feed_type=FeedType.SPORTS,
            event_type=FeedEventType.FEED_DISCONNECTED,
            received_at=1000.0,
        )
        await disp.on_feed_event(ev)
        assert len(ch.sent) == 1
        assert ch.sent[0].severity == Severity.WARNING

    async def test_oracle_alert_routed(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        alert = OracleAlert(
            event_type=OracleEventType.DISPUTE_DETECTED,
            condition_id="cond_1",
            timestamp=1000.0,
        )
        await disp.on_oracle_alert(alert)
        assert len(ch.sent) == 1
        assert ch.sent[0].severity == Severity.CRITICAL

    async def test_multiple_channels(self) -> None:
        ch1 = FakeChannel()
        ch2 = FakeChannel()
        disp = AlertDispatcher(channels=[ch1, ch2], throttle_secs=0)
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_EXECUTED,
            timestamp=1000.0,
        )
        await disp.on_arb_event(ev)
        assert len(ch1.sent) == 1
        assert len(ch2.sent) == 1


# ── DEBUG Events (Log Only) ────────────────────────────────────


class TestDebugLogOnly:
    async def test_debug_not_sent_to_channels(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        ev = ArbEvent(
            event_type=ArbEventType.SIGNAL_GENERATED,
            timestamp=1000.0,
        )
        await disp.on_arb_event(ev)
        assert len(ch.sent) == 0

    async def test_debug_risk_not_sent(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        ev = RiskEvent(
            event_type=RiskEventType.RISK_GATE_REJECTED,
            timestamp=1000.0,
        )
        await disp.on_risk_event(ev)
        assert len(ch.sent) == 0

    async def test_debug_feed_not_sent(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        ev = FeedEvent(
            feed_type=FeedType.ECONOMIC,
            event_type=FeedEventType.DATA_RELEASED,
            received_at=1000.0,
        )
        await disp.on_feed_event(ev)
        assert len(ch.sent) == 0


# ── Throttling ──────────────────────────────────────────────────


class TestThrottling:
    async def test_throttle_suppresses_repeat(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=9999)
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_EXECUTED,
            timestamp=1000.0,
        )
        await disp.on_arb_event(ev)
        await disp.on_arb_event(ev)
        await disp.on_arb_event(ev)
        assert len(ch.sent) == 1  # only the first one

    async def test_different_event_types_not_throttled(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=9999)
        ev1 = ArbEvent(
            event_type=ArbEventType.TRADE_EXECUTED,
            timestamp=1000.0,
        )
        ev2 = ArbEvent(
            event_type=ArbEventType.TRADE_FAILED,
            timestamp=1000.0,
        )
        await disp.on_arb_event(ev1)
        await disp.on_arb_event(ev2)
        assert len(ch.sent) == 2

    async def test_zero_throttle_allows_all(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_EXECUTED,
            timestamp=1000.0,
        )
        await disp.on_arb_event(ev)
        await disp.on_arb_event(ev)
        assert len(ch.sent) == 2


# ── CRITICAL Bypass ─────────────────────────────────────────────


class TestCriticalBypass:
    async def test_critical_bypasses_throttle(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=9999)
        ev = RiskEvent(
            event_type=RiskEventType.KILL_SWITCH_TRIGGERED,
            reason="loss",
            timestamp=1000.0,
        )
        await disp.on_risk_event(ev)
        await disp.on_risk_event(ev)
        await disp.on_risk_event(ev)
        assert len(ch.sent) == 3  # all bypass

    async def test_critical_oracle_bypasses_throttle(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=9999)
        alert = OracleAlert(
            event_type=OracleEventType.DISPUTE_DETECTED,
            condition_id="c1",
            timestamp=1000.0,
        )
        await disp.on_oracle_alert(alert)
        await disp.on_oracle_alert(alert)
        assert len(ch.sent) == 2


# ── Decision Logging ────────────────────────────────────────────


class TestDecisionLogging:
    async def test_decision_logger_called_for_debug(self) -> None:
        disp = AlertDispatcher(channels=[], throttle_secs=0)
        with patch("src.monitor.dispatcher.decision_logger") as mock_log:
            ev = ArbEvent(
                event_type=ArbEventType.SIGNAL_GENERATED,
                timestamp=1000.0,
            )
            await disp.on_arb_event(ev)
            mock_log.info.assert_called_once()
            call_kwargs = mock_log.info.call_args
            assert call_kwargs[0][0] == "decision"

    async def test_decision_logger_called_for_info(self) -> None:
        disp = AlertDispatcher(channels=[], throttle_secs=0)
        with patch("src.monitor.dispatcher.decision_logger") as mock_log:
            ev = ArbEvent(
                event_type=ArbEventType.TRADE_EXECUTED,
                timestamp=1000.0,
            )
            await disp.on_arb_event(ev)
            mock_log.info.assert_called_once()

    async def test_decision_log_contains_raw(self) -> None:
        disp = AlertDispatcher(channels=[], throttle_secs=0)
        with patch("src.monitor.dispatcher.decision_logger") as mock_log:
            ev = ArbEvent(
                event_type=ArbEventType.ENGINE_STARTED,
                timestamp=1000.0,
            )
            await disp.on_arb_event(ev)
            call_kwargs = mock_log.info.call_args[1]
            assert "raw" in call_kwargs
            assert call_kwargs["raw"]["event_type"] == "ENGINE_STARTED"

    async def test_decision_log_contains_severity(self) -> None:
        disp = AlertDispatcher(channels=[], throttle_secs=0)
        with patch("src.monitor.dispatcher.decision_logger") as mock_log:
            ev = RiskEvent(
                event_type=RiskEventType.KILL_SWITCH_TRIGGERED,
                timestamp=1000.0,
            )
            await disp.on_risk_event(ev)
            call_kwargs = mock_log.info.call_args[1]
            assert call_kwargs["severity"] == "CRITICAL"


# ── Channel Exception Handling ──────────────────────────────────


class TestChannelExceptions:
    async def test_channel_error_does_not_propagate(self) -> None:
        ch = FakeChannel(fail=True)
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_EXECUTED,
            timestamp=1000.0,
        )
        # Should not raise
        await disp.on_arb_event(ev)

    async def test_one_channel_error_does_not_block_others(self) -> None:
        ch_fail = FakeChannel(fail=True)
        ch_ok = FakeChannel()
        disp = AlertDispatcher(channels=[ch_fail, ch_ok], throttle_secs=0)
        ev = ArbEvent(
            event_type=ArbEventType.TRADE_EXECUTED,
            timestamp=1000.0,
        )
        await disp.on_arb_event(ev)
        assert len(ch_ok.sent) == 1


# ── Direct Send ─────────────────────────────────────────────────


class TestDirectSend:
    async def test_send_bypasses_throttle(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=9999)
        msg = AlertMessage(
            severity=Severity.INFO,
            title="DAILY_SUMMARY",
            source_event_type="DAILY_SUMMARY",
        )
        await disp.send(msg)
        await disp.send(msg)
        assert len(ch.sent) == 2

    async def test_send_logs_decision(self) -> None:
        disp = AlertDispatcher(channels=[], throttle_secs=0)
        with patch("src.monitor.dispatcher.decision_logger") as mock_log:
            msg = AlertMessage(
                severity=Severity.INFO,
                title="DAILY_SUMMARY",
                source_event_type="DAILY_SUMMARY",
            )
            await disp.send(msg)
            mock_log.info.assert_called_once()


# ── Lifecycle ───────────────────────────────────────────────────


class TestLifecycle:
    async def test_close_channels(self) -> None:
        ch1 = FakeChannel()
        ch2 = FakeChannel()
        disp = AlertDispatcher(channels=[ch1, ch2])
        await disp.close()
        assert ch1.closed
        assert ch2.closed

    async def test_close_empty(self) -> None:
        disp = AlertDispatcher(channels=[])
        await disp.close()  # should not raise
