"""RiskMonitor — orchestrates risk gates, position tracking, and P&L."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal

import structlog

from src.core.config import RiskConfig
from src.core.types import (
    ExecutionResult,
    KillSwitchState,
    KillSwitchTrigger,
    OracleAlert,
    OracleEventType,
    RiskEvent,
    RiskEventType,
    RiskVerdict,
    TradeAction,
)
from src.risk.gates import (
    check_daily_loss,
    check_fee_rate,
    check_kill_switch,
    check_market_status,
    check_max_concurrent_positions,
    check_oracle_risk,
    check_orderbook_depth,
    check_position_concentration,
    check_spread,
    check_uma_exposure,
)
from src.risk.kill_switch import KillSwitchManager
from src.risk.oracle_monitor import OracleMonitor
from src.risk.pnl import PnLTracker
from src.risk.positions import PositionTracker

logger = structlog.stdlib.get_logger()

RiskEventCallback = Callable[[RiskEvent], Awaitable[None] | None]


class RiskMonitor:
    """Orchestrates risk gate checks, position tracking, and P&L.

    Usage::

        monitor = RiskMonitor(risk_config)
        monitor.on_event(my_callback)

        verdict = monitor.check_trade(action)
        if not verdict.approved:
            return  # rejected

        # ... execute trade ...
        await monitor.record_fill(result)
    """

    def __init__(
        self,
        config: RiskConfig | None = None,
        oracle_monitor: OracleMonitor | None = None,
    ) -> None:
        from src.core.config import get_settings

        self._config = config or get_settings().risk
        self._positions = PositionTracker()
        self._pnl = PnLTracker()
        self._kill_switch = KillSwitchManager(self._config.kill_switch)
        self._oracle_monitor = oracle_monitor
        self._callbacks: list[RiskEventCallback] = []

        # Wire oracle alerts → risk events
        if self._oracle_monitor is not None:
            self._oracle_monitor.on_alert(self._forward_oracle_alert)

    @property
    def positions(self) -> PositionTracker:
        """The position tracker."""
        return self._positions

    @property
    def pnl(self) -> PnLTracker:
        """The P&L tracker."""
        return self._pnl

    @property
    def killed(self) -> bool:
        """Whether the kill switch is active."""
        return self._kill_switch.active

    @property
    def kill_switch_state(self) -> KillSwitchState:
        """Full kill switch state snapshot."""
        return self._kill_switch.state

    @property
    def oracle(self) -> OracleMonitor | None:
        """The oracle monitor, if configured."""
        return self._oracle_monitor

    def on_event(self, callback: RiskEventCallback) -> None:
        """Register a callback for risk events."""
        self._callbacks.append(callback)

    async def _emit(self, event: RiskEvent) -> None:
        """Dispatch a risk event to all registered callbacks."""
        for cb in self._callbacks:
            try:
                result = cb(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "risk_event_callback_error",
                    event_type=event.event_type,
                )

    def check_trade(self, action: TradeAction) -> RiskVerdict:
        """Run all risk gates in priority order.

        Returns the first rejection, or an approved verdict if all pass.
        """
        # 1. Kill switch (highest priority)
        verdict = check_kill_switch(self._kill_switch.active)
        if not verdict.approved:
            return verdict

        # 2. Oracle risk filter
        question = action.signal.match.opportunity.question
        verdict = check_oracle_risk(question, self._config.kill_switch)
        if not verdict.approved:
            return verdict

        # 2.5. UMA exposure / dispute check
        if self._oracle_monitor is not None:
            verdict = check_uma_exposure(
                action,
                self._positions.positions,
                self._oracle_monitor.proposals,
                self._config,
            )
            if not verdict.approved:
                return verdict

        # 3. Daily loss limit
        verdict = check_daily_loss(self._pnl, self._config)
        if not verdict.approved:
            return verdict

        # 4. Position concentration
        verdict = check_position_concentration(
            action, self._positions.positions, self._config,
        )
        if not verdict.approved:
            return verdict

        # 5. Max concurrent positions
        verdict = check_max_concurrent_positions(
            self._positions.positions, self._config,
        )
        if not verdict.approved:
            return verdict

        # 6. Market status
        verdict = check_market_status(action, self._config)
        if not verdict.approved:
            return verdict

        # 7. Fee rate
        verdict = check_fee_rate(action, self._config)
        if not verdict.approved:
            return verdict

        # 8. Orderbook depth (directional when available)
        verdict = check_orderbook_depth(action, self._config)
        if not verdict.approved:
            return verdict

        # 9. Spread
        verdict = check_spread(action, self._config)
        if not verdict.approved:
            return verdict

        return RiskVerdict(approved=True)

    async def record_fill(self, result: ExecutionResult) -> None:
        """Record a successful fill — update positions and P&L.

        May auto-trigger the kill switch if daily loss or trade counters
        breach thresholds.
        """
        existing = self._positions.get(result.action.token_id)
        pnl_amount = self._pnl.record_fill(result, existing)
        updated_pos = self._positions.record_fill(result)

        if updated_pos is not None:
            await self._emit(RiskEvent(
                event_type=RiskEventType.POSITION_OPENED,
                position=updated_pos,
                timestamp=time.time(),
            ))
        else:
            await self._emit(RiskEvent(
                event_type=RiskEventType.POSITION_CLOSED,
                daily_pnl=self._pnl.realized_today,
                timestamp=time.time(),
            ))

        # Auto-trigger kill switch if daily loss limit breached
        if pnl_amount < 0:
            limit = Decimal(str(self._config.max_daily_loss_usd))
            if self._pnl.realized_today < -limit and not self._kill_switch.active:
                self._kill_switch.trigger(
                    f"Daily loss ${self._pnl.realized_today}"
                    f" breached -${limit} limit",
                    KillSwitchTrigger.DAILY_LOSS,
                )
                logger.warning(
                    "kill_switch_triggered",
                    trigger="DAILY_LOSS",
                    daily_pnl=str(self._pnl.realized_today),
                    limit=str(limit),
                )
                await self._emit(RiskEvent(
                    event_type=RiskEventType.KILL_SWITCH_TRIGGERED,
                    daily_pnl=self._pnl.realized_today,
                    reason=(
                        f"Daily loss ${self._pnl.realized_today}"
                        f" breached -${limit} limit"
                    ),
                    timestamp=time.time(),
                ))

        # Record trade result for consecutive-loss / error-rate triggers
        trade_success = pnl_amount >= 0
        trigger = self._kill_switch.record_trade_result(trade_success)
        if trigger is not None:
            logger.warning(
                "kill_switch_triggered",
                trigger=trigger.value,
            )
            await self._emit(RiskEvent(
                event_type=RiskEventType.KILL_SWITCH_TRIGGERED,
                reason=self._kill_switch.state.reason,
                timestamp=time.time(),
            ))

    async def record_api_result(
        self, success: bool, latency_ms: float = 0,
    ) -> None:
        """Record an API call result for connectivity health tracking."""
        if success:
            self._kill_switch.record_api_success()
        else:
            trigger = self._kill_switch.record_api_error()
            if trigger is not None:
                logger.warning(
                    "kill_switch_triggered",
                    trigger=trigger.value,
                )
                await self._emit(RiskEvent(
                    event_type=RiskEventType.KILL_SWITCH_TRIGGERED,
                    reason=self._kill_switch.state.reason,
                    timestamp=time.time(),
                ))

    async def reset_kill_switch(self) -> None:
        """Manually reset the kill switch."""
        self._kill_switch.reset()
        logger.info("kill_switch_reset")
        await self._emit(RiskEvent(
            event_type=RiskEventType.KILL_SWITCH_RESET,
            reason="Kill switch manually reset",
            timestamp=time.time(),
        ))

    def snapshot(self) -> dict[str, object]:
        """Return a snapshot of current risk state."""
        self._pnl._maybe_reset_day()
        ks = self._kill_switch.state
        snap: dict[str, object] = {
            "killed": self._kill_switch.active,
            "kill_switch_trigger": ks.trigger.value if ks.trigger else None,
            "kill_switch_reason": ks.reason,
            "open_positions": self._positions.count,
            "total_exposure_usd": float(self._positions.total_exposure_usd()),
            "realized_today": float(self._pnl.realized_today),
            "realized_total": float(self._pnl.realized_total),
            "trade_count_today": self._pnl.trade_count_today,
        }
        if self._oracle_monitor is not None:
            snap["disputed_markets"] = len(
                self._oracle_monitor.disputed_conditions,
            )
            snap["exposure_at_risk_usd"] = float(
                self._oracle_monitor.exposure_at_risk(),
            )
        return snap

    async def _forward_oracle_alert(self, alert: OracleAlert) -> None:
        """Forward oracle alerts as RiskEvents."""
        event_type_map = {
            OracleEventType.DISPUTE_DETECTED: RiskEventType.DISPUTE_DETECTED,
            OracleEventType.WHALE_ACTIVITY_DETECTED: (
                RiskEventType.WHALE_ACTIVITY_DETECTED
            ),
        }
        risk_event_type = event_type_map.get(alert.event_type)
        if risk_event_type is not None:
            await self._emit(RiskEvent(
                event_type=risk_event_type,
                reason=alert.reason,
                timestamp=alert.timestamp,
            ))
