"""KillSwitchManager — multi-trigger kill switch with rolling counters."""

from __future__ import annotations

import time
from collections import deque

from src.core.config import KillSwitchConfig
from src.core.types import KillSwitchState, KillSwitchTrigger


class KillSwitchManager:
    """Manages kill switch state with multiple auto-trigger conditions.

    Tracks consecutive losses, rolling error rate, and API connectivity
    health.  Any trigger condition activating sets the kill switch, which
    halts all trading until manually reset.
    """

    def __init__(self, config: KillSwitchConfig | None = None) -> None:
        self._config = config or KillSwitchConfig()
        self._state = KillSwitchState()
        self._consecutive_losses: int = 0
        self._recent_results: deque[bool] = deque(
            maxlen=self._config.error_window_trades,
        )
        self._api_error_count: int = 0

    # ── Properties ────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        """Whether the kill switch is currently engaged."""
        return self._state.active

    @property
    def state(self) -> KillSwitchState:
        """Read-only copy of the current kill switch state."""
        return self._state.model_copy()

    @property
    def consecutive_losses(self) -> int:
        """Current consecutive-loss streak."""
        return self._consecutive_losses

    @property
    def error_rate(self) -> float:
        """Current error rate percentage from the rolling window."""
        if not self._recent_results:
            return 0.0
        failures = sum(1 for r in self._recent_results if not r)
        return (failures / len(self._recent_results)) * 100.0

    # ── Trigger checks (pure) ────────────────────────────────────

    def check_consecutive_losses(self) -> bool:
        """Return True if consecutive losses have hit the threshold."""
        return self._consecutive_losses >= self._config.max_consecutive_losses

    def check_error_rate(self) -> bool:
        """Return True if rolling error rate has hit the threshold."""
        if not self._recent_results:
            return False
        return self.error_rate >= self._config.max_error_rate_pct

    def check_connectivity(self, error_count: int) -> bool:
        """Return True if API error count has hit the threshold."""
        return error_count >= self._config.connectivity_max_errors

    # ── State mutation ───────────────────────────────────────────

    def trigger(self, reason: str, trigger_type: KillSwitchTrigger) -> None:
        """Activate the kill switch."""
        self._state = KillSwitchState(
            active=True,
            trigger=trigger_type,
            triggered_at=time.time(),
            reason=reason,
        )

    def reset(self) -> None:
        """Deactivate the kill switch and clear all state."""
        self._state = KillSwitchState()
        self._consecutive_losses = 0
        self._recent_results.clear()
        self._api_error_count = 0

    def record_trade_result(self, success: bool) -> KillSwitchTrigger | None:
        """Update counters after a trade and auto-check triggers.

        Returns the trigger type if newly activated, else None.
        """
        if self._state.active:
            return None

        self._recent_results.append(success)

        if success:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1

        # Check consecutive losses
        if self.check_consecutive_losses():
            trigger = KillSwitchTrigger.CONSECUTIVE_LOSSES
            self.trigger(
                f"{self._consecutive_losses} consecutive losses",
                trigger,
            )
            return trigger

        # Check error rate
        if self.check_error_rate():
            trigger = KillSwitchTrigger.ERROR_RATE
            self.trigger(
                f"Error rate {self.error_rate:.1f}% exceeds"
                f" {self._config.max_error_rate_pct}% threshold",
                trigger,
            )
            return trigger

        return None

    def record_api_error(self) -> KillSwitchTrigger | None:
        """Record an API error and auto-check connectivity trigger.

        Returns the trigger type if newly activated, else None.
        """
        if self._state.active:
            return None

        self._api_error_count += 1

        if self.check_connectivity(self._api_error_count):
            trigger = KillSwitchTrigger.CONNECTIVITY
            self.trigger(
                f"{self._api_error_count} API errors exceeds"
                f" {self._config.connectivity_max_errors} threshold",
                trigger,
            )
            return trigger

        return None

    def record_api_success(self) -> None:
        """Reset the API error counter on successful API call."""
        self._api_error_count = 0
