"""Convenience factory for wiring the monitoring stack."""

from __future__ import annotations

from typing import Callable

from src.core.config import AlertsConfig
from src.monitor.channels import (
    DiscordChannel,
    NotificationChannel,
    TelegramChannel,
)
from src.monitor.daily_summary import DailySummaryScheduler
from src.monitor.dispatcher import AlertDispatcher

SnapshotFn = Callable[[], dict[str, object]]


def create_monitor_stack(
    config: AlertsConfig,
    snapshot_fn: SnapshotFn | None = None,
) -> tuple[AlertDispatcher, DailySummaryScheduler | None]:
    """Build a dispatcher + optional daily scheduler from config.

    Returns:
        (dispatcher, scheduler_or_None)
    """
    channels: list[NotificationChannel] = []

    if config.telegram.enabled:
        channels.append(TelegramChannel(config.telegram))

    if config.discord.enabled:
        channels.append(DiscordChannel(config.discord))

    dispatcher = AlertDispatcher(
        channels=channels,
        throttle_secs=config.throttle_secs,
    )

    scheduler: DailySummaryScheduler | None = None
    if snapshot_fn is not None:
        scheduler = DailySummaryScheduler(
            dispatcher=dispatcher,
            snapshot_fn=snapshot_fn,
            hour_utc=config.daily_summary_hour_utc,
        )

    return dispatcher, scheduler
