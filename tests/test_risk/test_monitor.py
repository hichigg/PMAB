"""Tests for RiskMonitor — orchestration, state, events."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import pytest

from src.core.config import KillSwitchConfig, RiskConfig
from src.core.types import (
    ExecutionResult,
    FeedEvent,
    FeedEventType,
    FeedType,
    KillSwitchTrigger,
    MarketCategory,
    MarketOpportunity,
    MatchResult,
    OutcomeType,
    Position,
    RiskEvent,
    RiskEventType,
    RiskRejectionReason,
    Side,
    Signal,
    SignalDirection,
    TradeAction,
)
from src.risk.monitor import RiskMonitor

# ── Helpers ─────────────────────────────────────────────────────


def _tokens() -> list[dict[str, Any]]:
    return [{"token_id": "0xyes", "outcome": "Yes"}]


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


def _signal(
    condition_id: str = "cond1",
    depth_usd: Decimal = Decimal("5000"),
    spread: Decimal | None = Decimal("0.05"),
    question: str = "Will CPI exceed 3.0%?",
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
                question=question,
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


def _result(
    token_id: str = "0xyes",
    side: Side = Side.BUY,
    price: Decimal = Decimal("0.50"),
    size: Decimal = Decimal("100"),
    condition_id: str = "cond1",
) -> ExecutionResult:
    action = TradeAction(
        signal=_signal(condition_id),
        token_id=token_id,
        side=side,
        price=price,
        size=size,
    )
    return ExecutionResult(
        action=action,
        success=True,
        fill_price=price,
        fill_size=size,
        executed_at=time.time(),
    )


# ── check_trade ────────────────────────────────────────────────


class TestCheckTrade:
    def test_all_pass(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        v = monitor.check_trade(_action())
        assert v.approved is True

    def test_kill_switch_rejects(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        monitor._kill_switch.trigger("test", KillSwitchTrigger.MANUAL)
        v = monitor.check_trade(_action())
        assert v.approved is False
        assert v.reason == RiskRejectionReason.KILL_SWITCH_ACTIVE

    def test_daily_loss_rejects(self) -> None:
        monitor = RiskMonitor(config=_cfg(max_daily_loss_usd=100.0))
        monitor._pnl.realized_today = Decimal("-200")
        v = monitor.check_trade(_action())
        assert v.approved is False
        assert v.reason == RiskRejectionReason.DAILY_LOSS_LIMIT

    def test_concentration_rejects(self) -> None:
        monitor = RiskMonitor(config=_cfg(bankroll_usd=1000.0, max_bankroll_pct_per_event=0.10))
        # limit = 100, action = 0.50*100 = 50; now add existing
        monitor._positions._positions["0xa"] = Position(
            token_id="0xa", condition_id="cond1", side=Side.BUY,
            entry_price=Decimal("0.50"), size=Decimal("200"),  # exposure = 100
        )
        v = monitor.check_trade(_action())
        assert v.approved is False
        assert v.reason == RiskRejectionReason.POSITION_CONCENTRATION

    def test_max_positions_rejects(self) -> None:
        monitor = RiskMonitor(config=_cfg(max_concurrent_positions=2))
        for i in range(2):
            monitor._positions._positions[f"tok{i}"] = Position(
                token_id=f"tok{i}", side=Side.BUY,
            )
        v = monitor.check_trade(_action())
        assert v.approved is False
        assert v.reason == RiskRejectionReason.MAX_CONCURRENT_POSITIONS

    def test_depth_rejects(self) -> None:
        monitor = RiskMonitor(config=_cfg(min_orderbook_depth_usd=10000.0))
        v = monitor.check_trade(_action(depth_usd=Decimal("100")))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.ORDERBOOK_DEPTH

    def test_spread_rejects(self) -> None:
        monitor = RiskMonitor(config=_cfg(max_spread=0.01))
        v = monitor.check_trade(_action(spread=Decimal("0.05")))
        assert v.approved is False
        assert v.reason == RiskRejectionReason.SPREAD_TOO_WIDE

    def test_first_rejection_wins(self) -> None:
        """Kill switch should be checked before daily loss."""
        monitor = RiskMonitor(config=_cfg(max_daily_loss_usd=100.0))
        monitor._kill_switch.trigger("test", KillSwitchTrigger.MANUAL)
        monitor._pnl.realized_today = Decimal("-200")
        v = monitor.check_trade(_action())
        assert v.reason == RiskRejectionReason.KILL_SWITCH_ACTIVE

    def test_gate_priority_order(self) -> None:
        """Kill switch first, then daily loss (not other gates)."""
        monitor = RiskMonitor(config=_cfg(max_daily_loss_usd=100.0))
        monitor._pnl.realized_today = Decimal("-200")
        # Not killed, but daily loss breached → daily loss reason
        v = monitor.check_trade(_action())
        assert v.reason == RiskRejectionReason.DAILY_LOSS_LIMIT


# ── record_fill ────────────────────────────────────────────────


class TestRecordFill:
    @pytest.mark.asyncio
    async def test_updates_positions(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        await monitor.record_fill(_result())
        assert monitor.positions.count == 1

    @pytest.mark.asyncio
    async def test_updates_pnl(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        await monitor.record_fill(_result())
        assert monitor.pnl.trade_count_today == 1

    @pytest.mark.asyncio
    async def test_emits_position_opened(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        events: list[RiskEvent] = []
        monitor.on_event(lambda e: events.append(e))
        await monitor.record_fill(_result())
        assert any(e.event_type == RiskEventType.POSITION_OPENED for e in events)

    @pytest.mark.asyncio
    async def test_emits_position_closed(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        events: list[RiskEvent] = []
        monitor.on_event(lambda e: events.append(e))
        # Open then close
        await monitor.record_fill(_result(side=Side.BUY))
        events.clear()
        await monitor.record_fill(_result(side=Side.SELL))
        assert any(e.event_type == RiskEventType.POSITION_CLOSED for e in events)

    @pytest.mark.asyncio
    async def test_auto_triggers_kill_switch(self) -> None:
        monitor = RiskMonitor(config=_cfg(max_daily_loss_usd=10.0))
        # Open buy position first
        monitor._positions._positions["0xyes"] = Position(
            token_id="0xyes", condition_id="cond1", side=Side.BUY,
            entry_price=Decimal("0.50"), size=Decimal("100"),
        )
        # Close at a loss: sell at 0.30, entry was 0.50 → loss = (0.30-0.50)*100 = -20
        events: list[RiskEvent] = []
        monitor.on_event(lambda e: events.append(e))
        await monitor.record_fill(_result(side=Side.SELL, price=Decimal("0.30")))
        assert monitor.killed is True
        assert any(e.event_type == RiskEventType.KILL_SWITCH_TRIGGERED for e in events)


# ── Kill Switch ────────────────────────────────────────────────


class TestKillSwitch:
    def test_starts_false(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        assert monitor.killed is False

    @pytest.mark.asyncio
    async def test_triggered_by_loss(self) -> None:
        monitor = RiskMonitor(config=_cfg(max_daily_loss_usd=10.0))
        monitor._positions._positions["0xyes"] = Position(
            token_id="0xyes", condition_id="cond1", side=Side.BUY,
            entry_price=Decimal("0.50"), size=Decimal("100"),
        )
        await monitor.record_fill(_result(side=Side.SELL, price=Decimal("0.30")))
        assert monitor.killed is True

    @pytest.mark.asyncio
    async def test_triggered_emits_event(self) -> None:
        monitor = RiskMonitor(config=_cfg(max_daily_loss_usd=10.0))
        monitor._positions._positions["0xyes"] = Position(
            token_id="0xyes", condition_id="cond1", side=Side.BUY,
            entry_price=Decimal("0.50"), size=Decimal("100"),
        )
        events: list[RiskEvent] = []
        monitor.on_event(lambda e: events.append(e))
        await monitor.record_fill(_result(side=Side.SELL, price=Decimal("0.30")))
        kill_events = [e for e in events if e.event_type == RiskEventType.KILL_SWITCH_TRIGGERED]
        assert len(kill_events) == 1

    @pytest.mark.asyncio
    async def test_reset(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        monitor._kill_switch.trigger("test", KillSwitchTrigger.MANUAL)
        await monitor.reset_kill_switch()
        assert monitor.killed is False

    @pytest.mark.asyncio
    async def test_reset_emits_event(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        monitor._kill_switch.trigger("test", KillSwitchTrigger.MANUAL)
        events: list[RiskEvent] = []
        monitor.on_event(lambda e: events.append(e))
        await monitor.reset_kill_switch()
        assert any(e.event_type == RiskEventType.KILL_SWITCH_RESET for e in events)


# ── Callbacks ──────────────────────────────────────────────────


class TestCallbacks:
    @pytest.mark.asyncio
    async def test_sync_callback(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        events: list[RiskEvent] = []
        monitor.on_event(lambda e: events.append(e))
        await monitor.record_fill(_result())
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_async_callback(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        events: list[RiskEvent] = []

        async def async_cb(e: RiskEvent) -> None:
            events.append(e)

        monitor.on_event(async_cb)
        await monitor.record_fill(_result())
        assert len(events) >= 1

    @pytest.mark.asyncio
    async def test_exception_safety(self) -> None:
        monitor = RiskMonitor(config=_cfg())

        def bad_cb(e: RiskEvent) -> None:
            raise RuntimeError("boom")

        monitor.on_event(bad_cb)
        # Should not raise
        await monitor.record_fill(_result())


# ── Snapshot ───────────────────────────────────────────────────


class TestSnapshot:
    def test_has_required_fields(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        snap = monitor.snapshot()
        assert "killed" in snap
        assert "open_positions" in snap
        assert "total_exposure_usd" in snap
        assert "realized_today" in snap
        assert "realized_total" in snap
        assert "trade_count_today" in snap

    @pytest.mark.asyncio
    async def test_reflects_state(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        await monitor.record_fill(_result())
        snap = monitor.snapshot()
        assert snap["open_positions"] == 1
        assert snap["trade_count_today"] == 1


# ── Config ─────────────────────────────────────────────────────


class TestConfig:
    def test_uses_provided_config(self) -> None:
        cfg = _cfg(max_daily_loss_usd=42.0)
        monitor = RiskMonitor(config=cfg)
        assert monitor._config.max_daily_loss_usd == 42.0


# ── Enhanced Kill Switch ──────────────────────────────────────


class TestEnhancedKillSwitch:
    @pytest.mark.asyncio
    async def test_consecutive_losses_trigger(self) -> None:
        cfg = _cfg()
        cfg.kill_switch = KillSwitchConfig(max_consecutive_losses=2)
        monitor = RiskMonitor(config=cfg)
        events: list[RiskEvent] = []
        monitor.on_event(lambda e: events.append(e))
        # Open positions so sells produce losses
        for i in range(2):
            token = f"0xtok{i}"
            monitor._positions._positions[token] = Position(
                token_id=token, condition_id=f"cond{i}", side=Side.BUY,
                entry_price=Decimal("0.50"), size=Decimal("100"),
            )
            r = ExecutionResult(
                action=TradeAction(
                    signal=_signal(condition_id=f"cond{i}"),
                    token_id=token, side=Side.SELL,
                    price=Decimal("0.40"), size=Decimal("100"),
                ),
                success=True,
                fill_price=Decimal("0.40"),
                fill_size=Decimal("100"),
                executed_at=time.time(),
            )
            await monitor.record_fill(r)
        assert monitor.killed is True
        kill_events = [
            e for e in events
            if e.event_type == RiskEventType.KILL_SWITCH_TRIGGERED
        ]
        assert len(kill_events) >= 1

    @pytest.mark.asyncio
    async def test_error_rate_trigger(self) -> None:
        cfg = _cfg()
        cfg.kill_switch = KillSwitchConfig(
            error_window_trades=4,
            max_error_rate_pct=50.0,
            max_consecutive_losses=999,
        )
        monitor = RiskMonitor(config=cfg)
        # Produce 3 losses out of 4 trades (75% > 50%)
        for i in range(3):
            token = f"0xtok{i}"
            monitor._positions._positions[token] = Position(
                token_id=token, condition_id=f"cond{i}", side=Side.BUY,
                entry_price=Decimal("0.50"), size=Decimal("100"),
            )
            r = ExecutionResult(
                action=TradeAction(
                    signal=_signal(condition_id=f"cond{i}"),
                    token_id=token, side=Side.SELL,
                    price=Decimal("0.40"), size=Decimal("100"),
                ),
                success=True,
                fill_price=Decimal("0.40"),
                fill_size=Decimal("100"),
                executed_at=time.time(),
            )
            await monitor.record_fill(r)
        assert monitor.killed is True

    @pytest.mark.asyncio
    async def test_connectivity_trigger_via_record_api_result(self) -> None:
        cfg = _cfg()
        cfg.kill_switch = KillSwitchConfig(connectivity_max_errors=2)
        monitor = RiskMonitor(config=cfg)
        events: list[RiskEvent] = []
        monitor.on_event(lambda e: events.append(e))
        await monitor.record_api_result(success=False)
        await monitor.record_api_result(success=False)
        assert monitor.killed is True
        assert any(
            e.event_type == RiskEventType.KILL_SWITCH_TRIGGERED for e in events
        )

    def test_oracle_gate_rejects_ambiguous(self) -> None:
        cfg = _cfg()
        cfg.kill_switch = KillSwitchConfig()
        monitor = RiskMonitor(config=cfg)
        action = _action()
        # Override with ambiguous question
        action.signal.match.opportunity.question = (
            "Resolved at discretion of the committee"
        )
        v = monitor.check_trade(action)
        assert v.approved is False
        assert v.reason == RiskRejectionReason.ORACLE_RISK

    def test_oracle_gate_passes_clear(self) -> None:
        cfg = _cfg()
        cfg.kill_switch = KillSwitchConfig()
        monitor = RiskMonitor(config=cfg)
        action = _action()
        action.signal.match.opportunity.question = "Will CPI exceed 3.0%?"
        v = monitor.check_trade(action)
        assert v.approved is True

    def test_kill_switch_state_property(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        state = monitor.kill_switch_state
        assert state.active is False
        assert state.trigger is None

    def test_snapshot_includes_trigger_info(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        monitor._kill_switch.trigger("test", KillSwitchTrigger.MANUAL)
        snap = monitor.snapshot()
        assert snap["killed"] is True
        assert snap["kill_switch_trigger"] == "MANUAL"
        assert snap["kill_switch_reason"] == "test"

    def test_killed_property_backward_compat(self) -> None:
        monitor = RiskMonitor(config=_cfg())
        assert monitor.killed is False
        monitor._kill_switch.trigger("test", KillSwitchTrigger.MANUAL)
        assert monitor.killed is True

    @pytest.mark.asyncio
    async def test_reset_clears_enhanced_state(self) -> None:
        cfg = _cfg()
        cfg.kill_switch = KillSwitchConfig(connectivity_max_errors=2)
        monitor = RiskMonitor(config=cfg)
        await monitor.record_api_result(success=False)
        await monitor.record_api_result(success=False)
        assert monitor.killed is True
        await monitor.reset_kill_switch()
        assert monitor.killed is False
        assert monitor.kill_switch_state.trigger is None

    @pytest.mark.asyncio
    async def test_daily_loss_trigger_with_manager(self) -> None:
        monitor = RiskMonitor(config=_cfg(max_daily_loss_usd=10.0))
        monitor._positions._positions["0xyes"] = Position(
            token_id="0xyes", condition_id="cond1", side=Side.BUY,
            entry_price=Decimal("0.50"), size=Decimal("100"),
        )
        await monitor.record_fill(_result(side=Side.SELL, price=Decimal("0.30")))
        assert monitor.killed is True
        assert monitor.kill_switch_state.trigger == KillSwitchTrigger.DAILY_LOSS
