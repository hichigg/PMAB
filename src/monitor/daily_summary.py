"""Scheduled daily P&L summary."""

from __future__ import annotations

import asyncio
import datetime
from typing import Callable

import structlog

from src.monitor.dispatcher import AlertDispatcher
from src.monitor.types import AlertMessage, Severity

logger = structlog.get_logger(__name__)

# Type alias for the snapshot callable (e.g. RiskMonitor.snapshot).
SnapshotFn = Callable[[], dict[str, object]]


class DailySummaryScheduler:
    """Background task that emits a daily P&L summary at a configured UTC hour.

    Usage::

        scheduler = DailySummaryScheduler(
            dispatcher=dispatcher,
            snapshot_fn=risk_monitor.snapshot,
            hour_utc=0,
        )
        await scheduler.start()
        # ...
        await scheduler.stop()
    """

    def __init__(
        self,
        dispatcher: AlertDispatcher,
        snapshot_fn: SnapshotFn,
        hour_utc: int = 0,
    ) -> None:
        self._dispatcher = dispatcher
        self._snapshot_fn = snapshot_fn
        self._hour_utc = hour_utc
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._last_sent_date: datetime.date | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def emit_now(self) -> None:
        """Build and send the daily summary immediately (useful for testing)."""
        snap = self._snapshot_fn()
        msg = _build_summary(snap)
        await self._dispatcher.send(msg)
        self._last_sent_date = datetime.datetime.now(datetime.UTC).date()

    # ── Internal loop ───────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            try:
                now = datetime.datetime.now(datetime.UTC)
                today = now.date()
                if (
                    now.hour == self._hour_utc
                    and self._last_sent_date != today
                ):
                    await self.emit_now()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("daily_summary_loop_error")
            await asyncio.sleep(60)


def _build_summary(snap: dict[str, object]) -> AlertMessage:
    fields: dict[str, str] = {k: str(v) for k, v in snap.items()}
    realized = snap.get("realized_today", 0)
    body = f"Realized today: ${realized}"
    return AlertMessage(
        severity=Severity.INFO,
        title="DAILY_SUMMARY",
        body=body,
        fields=fields,
        source_event_type="DAILY_SUMMARY",
    )
