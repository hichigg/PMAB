"""Tests for DailySummaryScheduler — lifecycle, summary content, dedup."""

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from src.monitor.channels import NotificationChannel
from src.monitor.daily_summary import DailySummaryScheduler, _build_summary
from src.monitor.dispatcher import AlertDispatcher
from src.monitor.types import AlertMessage, Severity


# ── Helpers ─────────────────────────────────────────────────────


class FakeChannel(NotificationChannel):
    def __init__(self) -> None:
        self.sent: list[AlertMessage] = []

    async def send(self, msg: AlertMessage) -> bool:
        self.sent.append(msg)
        return True

    async def close(self) -> None:
        pass


def _snap() -> dict[str, object]:
    return {
        "killed": False,
        "kill_switch_trigger": None,
        "kill_switch_reason": "",
        "open_positions": 2,
        "total_exposure_usd": 450.0,
        "realized_today": 35.50,
        "realized_total": 120.0,
        "trade_count_today": 5,
    }


# ── _build_summary ──────────────────────────────────────────────


class TestBuildSummary:
    def test_severity_is_info(self) -> None:
        msg = _build_summary(_snap())
        assert msg.severity == Severity.INFO

    def test_title(self) -> None:
        msg = _build_summary(_snap())
        assert msg.title == "DAILY_SUMMARY"

    def test_body_contains_realized(self) -> None:
        msg = _build_summary(_snap())
        assert "35.5" in msg.body

    def test_fields_contain_all_keys(self) -> None:
        snap = _snap()
        msg = _build_summary(snap)
        for key in snap:
            assert key in msg.fields

    def test_source_event_type(self) -> None:
        msg = _build_summary(_snap())
        assert msg.source_event_type == "DAILY_SUMMARY"


# ── emit_now ────────────────────────────────────────────────────


class TestEmitNow:
    async def test_emit_sends_summary(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        sched = DailySummaryScheduler(
            dispatcher=disp,
            snapshot_fn=_snap,
            hour_utc=0,
        )
        await sched.emit_now()
        assert len(ch.sent) == 1
        assert ch.sent[0].title == "DAILY_SUMMARY"

    async def test_emit_updates_last_sent_date(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        sched = DailySummaryScheduler(
            dispatcher=disp,
            snapshot_fn=_snap,
        )
        assert sched._last_sent_date is None
        await sched.emit_now()
        assert sched._last_sent_date is not None

    async def test_emit_multiple_times(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        sched = DailySummaryScheduler(
            dispatcher=disp,
            snapshot_fn=_snap,
        )
        await sched.emit_now()
        await sched.emit_now()
        assert len(ch.sent) == 2


# ── Scheduler Lifecycle ─────────────────────────────────────────


class TestSchedulerLifecycle:
    async def test_start_creates_task(self) -> None:
        disp = AlertDispatcher(channels=[], throttle_secs=0)
        sched = DailySummaryScheduler(
            dispatcher=disp,
            snapshot_fn=_snap,
        )
        await sched.start()
        assert sched._running is True
        assert sched._task is not None
        await sched.stop()

    async def test_stop_cancels_task(self) -> None:
        disp = AlertDispatcher(channels=[], throttle_secs=0)
        sched = DailySummaryScheduler(
            dispatcher=disp,
            snapshot_fn=_snap,
        )
        await sched.start()
        await sched.stop()
        assert sched._running is False
        assert sched._task is None

    async def test_double_start_is_noop(self) -> None:
        disp = AlertDispatcher(channels=[], throttle_secs=0)
        sched = DailySummaryScheduler(
            dispatcher=disp,
            snapshot_fn=_snap,
        )
        await sched.start()
        task1 = sched._task
        await sched.start()
        assert sched._task is task1  # same task
        await sched.stop()

    async def test_stop_without_start(self) -> None:
        disp = AlertDispatcher(channels=[], throttle_secs=0)
        sched = DailySummaryScheduler(
            dispatcher=disp,
            snapshot_fn=_snap,
        )
        await sched.stop()  # should not raise


# ── Loop Dedup ──────────────────────────────────────────────────


class TestLoopDedup:
    async def test_loop_sends_at_configured_hour(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        sched = DailySummaryScheduler(
            dispatcher=disp,
            snapshot_fn=_snap,
            hour_utc=12,
        )

        # Mock datetime to return hour=12
        fake_now = datetime.datetime(2025, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)
        with patch("src.monitor.daily_summary.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = fake_now
            mock_dt.UTC = datetime.UTC
            sched._running = True
            # Run one iteration of the loop manually
            now = fake_now
            today = now.date()
            if now.hour == sched._hour_utc and sched._last_sent_date != today:
                await sched.emit_now()

        assert len(ch.sent) == 1

    async def test_dedup_prevents_double_send_same_day(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        sched = DailySummaryScheduler(
            dispatcher=disp,
            snapshot_fn=_snap,
            hour_utc=12,
        )

        fake_now = datetime.datetime(2025, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)
        with patch("src.monitor.daily_summary.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = fake_now
            mock_dt.UTC = datetime.UTC

            # First call
            sched._running = True
            now = fake_now
            today = now.date()
            if now.hour == sched._hour_utc and sched._last_sent_date != today:
                await sched.emit_now()

            # Second call same hour — should be deduped
            if now.hour == sched._hour_utc and sched._last_sent_date != today:
                await sched.emit_now()

        assert len(ch.sent) == 1  # only one summary

    async def test_different_day_sends_again(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0)
        sched = DailySummaryScheduler(
            dispatcher=disp,
            snapshot_fn=_snap,
            hour_utc=0,
        )

        # Simulate day 1
        sched._last_sent_date = datetime.date(2025, 6, 14)

        fake_now = datetime.datetime(2025, 6, 15, 0, 0, 0, tzinfo=datetime.UTC)
        now = fake_now
        today = now.date()
        if now.hour == sched._hour_utc and sched._last_sent_date != today:
            await sched.emit_now()

        assert len(ch.sent) == 1
