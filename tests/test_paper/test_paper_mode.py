"""Tests for paper_mode across dispatcher, factory, and dashboard."""

from __future__ import annotations

from src.core.config import AlertsConfig, PaperTradingConfig, Settings
from src.monitor.dispatcher import AlertDispatcher
from src.monitor.factory import create_monitor_stack
from src.monitor.types import AlertMessage, Severity


# ── Helpers ─────────────────────────────────────────────────────


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[AlertMessage] = []

    async def send(self, msg: AlertMessage) -> bool:
        self.sent.append(msg)
        return True

    async def close(self) -> None:
        pass


# ── AlertDispatcher paper_mode ─────────────────────────────────


class TestDispatcherPaperMode:
    async def test_paper_mode_prefixes_title(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0, paper_mode=True)
        msg = AlertMessage(
            severity=Severity.INFO,
            title="Trade Executed",
            source_event_type="trade",
        )
        await disp._handle(msg)
        assert ch.sent[0].title == "[PAPER] Trade Executed"

    async def test_paper_mode_off_no_prefix(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0, paper_mode=False)
        msg = AlertMessage(
            severity=Severity.INFO,
            title="Trade Executed",
            source_event_type="trade",
        )
        await disp._handle(msg)
        assert ch.sent[0].title == "Trade Executed"

    async def test_paper_mode_no_double_prefix(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0, paper_mode=True)
        msg = AlertMessage(
            severity=Severity.INFO,
            title="[PAPER] Already prefixed",
            source_event_type="trade",
        )
        await disp._handle(msg)
        assert ch.sent[0].title == "[PAPER] Already prefixed"

    async def test_paper_mode_critical_also_prefixed(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0, paper_mode=True)
        msg = AlertMessage(
            severity=Severity.CRITICAL,
            title="Kill Switch",
            source_event_type="risk",
        )
        await disp._handle(msg)
        assert ch.sent[0].title == "[PAPER] Kill Switch"

    async def test_paper_mode_debug_not_dispatched(self) -> None:
        ch = FakeChannel()
        disp = AlertDispatcher(channels=[ch], throttle_secs=0, paper_mode=True)
        msg = AlertMessage(
            severity=Severity.DEBUG,
            title="Debug Event",
            source_event_type="debug",
        )
        await disp._handle(msg)
        assert len(ch.sent) == 0


# ── Factory paper_mode ─────────────────────────────────────────


class TestFactoryPaperMode:
    def test_factory_passes_paper_mode(self) -> None:
        config = AlertsConfig()
        dispatcher, _scheduler = create_monitor_stack(
            config=config, paper_mode=True,
        )
        assert dispatcher._paper_mode is True

    def test_factory_default_no_paper_mode(self) -> None:
        config = AlertsConfig()
        dispatcher, _scheduler = create_monitor_stack(config=config)
        assert dispatcher._paper_mode is False


# ── PaperTradingConfig ─────────────────────────────────────────


class TestPaperTradingConfig:
    def test_defaults(self) -> None:
        cfg = PaperTradingConfig()
        assert cfg.fill_probability == 1.0
        assert cfg.slippage_bps == 5
        assert cfg.orderbook_refresh_secs == 30.0

    def test_custom_values(self) -> None:
        cfg = PaperTradingConfig(
            fill_probability=0.95,
            slippage_bps=10,
            orderbook_refresh_secs=15.0,
        )
        assert cfg.fill_probability == 0.95
        assert cfg.slippage_bps == 10
        assert cfg.orderbook_refresh_secs == 15.0

    def test_settings_includes_paper_trading(self) -> None:
        s = Settings()
        assert hasattr(s, "paper_trading")
        assert isinstance(s.paper_trading, PaperTradingConfig)
        assert s.paper_trading.fill_probability == 1.0


# ── Dashboard paper_mode ───────────────────────────────────────


class TestDashboardPaperMode:
    def test_header_includes_paper_mode(self) -> None:
        from scripts.dashboard import render_header, _strip_ansi
        output = _strip_ansi(render_header(paper_mode=True))
        assert "PAPER MODE" in output

    def test_header_without_paper_mode(self) -> None:
        from scripts.dashboard import render_header, _strip_ansi
        output = _strip_ansi(render_header(paper_mode=False))
        assert "PAPER MODE" not in output
