"""Tests for the monitor factory — wiring logic with various config combinations."""

from __future__ import annotations

from pydantic import SecretStr

from src.core.config import AlertsConfig, DiscordConfig, TelegramConfig
from src.monitor.channels import DiscordChannel, TelegramChannel
from src.monitor.daily_summary import DailySummaryScheduler
from src.monitor.dispatcher import AlertDispatcher
from src.monitor.factory import create_monitor_stack


# ── Helpers ─────────────────────────────────────────────────────


def _alerts(**kw: object) -> AlertsConfig:
    defaults: dict[str, object] = {}
    defaults.update(kw)
    return AlertsConfig(**defaults)  # type: ignore[arg-type]


def _snap() -> dict[str, object]:
    return {"realized_today": 42.0, "trade_count_today": 3}


# ── Config Combinations ────────────────────────────────────────


class TestFactoryWiring:
    def test_no_channels_enabled(self) -> None:
        config = _alerts()
        disp, sched = create_monitor_stack(config)
        assert isinstance(disp, AlertDispatcher)
        assert sched is None
        assert len(disp._channels) == 0

    def test_telegram_enabled(self) -> None:
        config = _alerts(
            telegram=TelegramConfig(
                enabled=True,
                bot_token=SecretStr("tok"),
                chat_id="123",
            )
        )
        disp, sched = create_monitor_stack(config)
        assert len(disp._channels) == 1
        assert isinstance(disp._channels[0], TelegramChannel)

    def test_discord_enabled(self) -> None:
        config = _alerts(
            discord=DiscordConfig(
                enabled=True,
                webhook_url=SecretStr("https://discord.com/webhook"),
            )
        )
        disp, sched = create_monitor_stack(config)
        assert len(disp._channels) == 1
        assert isinstance(disp._channels[0], DiscordChannel)

    def test_both_channels_enabled(self) -> None:
        config = _alerts(
            telegram=TelegramConfig(
                enabled=True,
                bot_token=SecretStr("tok"),
                chat_id="123",
            ),
            discord=DiscordConfig(
                enabled=True,
                webhook_url=SecretStr("https://discord.com/webhook"),
            ),
        )
        disp, sched = create_monitor_stack(config)
        assert len(disp._channels) == 2
        types = {type(ch) for ch in disp._channels}
        assert TelegramChannel in types
        assert DiscordChannel in types

    def test_disabled_channels_not_added(self) -> None:
        config = _alerts(
            telegram=TelegramConfig(enabled=False),
            discord=DiscordConfig(enabled=False),
        )
        disp, sched = create_monitor_stack(config)
        assert len(disp._channels) == 0

    def test_throttle_passed_to_dispatcher(self) -> None:
        config = _alerts(throttle_secs=60.0)
        disp, _ = create_monitor_stack(config)
        assert disp._throttle_secs == 60.0


# ── Scheduler ───────────────────────────────────────────────────


class TestFactoryScheduler:
    def test_no_snapshot_fn_no_scheduler(self) -> None:
        config = _alerts()
        _, sched = create_monitor_stack(config)
        assert sched is None

    def test_with_snapshot_fn_creates_scheduler(self) -> None:
        config = _alerts(daily_summary_hour_utc=14)
        disp, sched = create_monitor_stack(config, snapshot_fn=_snap)
        assert sched is not None
        assert isinstance(sched, DailySummaryScheduler)
        assert sched._hour_utc == 14

    def test_scheduler_references_dispatcher(self) -> None:
        config = _alerts()
        disp, sched = create_monitor_stack(config, snapshot_fn=_snap)
        assert sched is not None
        assert sched._dispatcher is disp
