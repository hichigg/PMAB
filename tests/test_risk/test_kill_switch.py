"""Tests for KillSwitchManager — multi-trigger kill switch logic."""

from __future__ import annotations

from src.core.config import KillSwitchConfig
from src.core.types import KillSwitchTrigger
from src.risk.kill_switch import KillSwitchManager


def _ks(**overrides: object) -> KillSwitchConfig:
    defaults: dict[str, object] = {
        "max_consecutive_losses": 5,
        "error_window_trades": 10,
        "max_error_rate_pct": 50.0,
        "connectivity_max_errors": 5,
        "connectivity_max_latency_ms": 5000.0,
        "oracle_blacklist_patterns": [],
    }
    defaults.update(overrides)
    return KillSwitchConfig(**defaults)  # type: ignore[arg-type]


# ── State ─────────────────────────────────────────────────────


class TestKillSwitchState:
    def test_initial_state_inactive(self) -> None:
        mgr = KillSwitchManager(_ks())
        assert mgr.active is False
        assert mgr.state.trigger is None

    def test_trigger_activates(self) -> None:
        mgr = KillSwitchManager(_ks())
        mgr.trigger("test reason", KillSwitchTrigger.MANUAL)
        assert mgr.active is True
        assert mgr.state.trigger == KillSwitchTrigger.MANUAL
        assert mgr.state.reason == "test reason"

    def test_reset_deactivates(self) -> None:
        mgr = KillSwitchManager(_ks())
        mgr.trigger("test", KillSwitchTrigger.MANUAL)
        mgr.reset()
        assert mgr.active is False
        assert mgr.state.trigger is None

    def test_state_is_copy(self) -> None:
        mgr = KillSwitchManager(_ks())
        s1 = mgr.state
        mgr.trigger("test", KillSwitchTrigger.MANUAL)
        s2 = mgr.state
        # s1 should not have been mutated
        assert s1.active is False
        assert s2.active is True


# ── Consecutive Losses ────────────────────────────────────────


class TestConsecutiveLosses:
    def test_no_trigger_below_threshold(self) -> None:
        mgr = KillSwitchManager(
            _ks(max_consecutive_losses=3, max_error_rate_pct=100.1),
        )
        for _ in range(2):
            mgr.record_trade_result(False)
        assert mgr.active is False

    def test_triggers_at_threshold(self) -> None:
        mgr = KillSwitchManager(
            _ks(max_consecutive_losses=3, max_error_rate_pct=100.1),
        )
        for _ in range(3):
            mgr.record_trade_result(False)
        assert mgr.active is True
        assert mgr.state.trigger == KillSwitchTrigger.CONSECUTIVE_LOSSES

    def test_resets_on_win(self) -> None:
        mgr = KillSwitchManager(
            _ks(max_consecutive_losses=5, max_error_rate_pct=100.1),
        )
        for _ in range(4):
            mgr.record_trade_result(False)
        assert mgr.consecutive_losses == 4
        mgr.record_trade_result(True)
        assert mgr.consecutive_losses == 0

    def test_starts_at_zero(self) -> None:
        mgr = KillSwitchManager(_ks())
        assert mgr.consecutive_losses == 0

    def test_custom_threshold(self) -> None:
        mgr = KillSwitchManager(
            _ks(max_consecutive_losses=2, max_error_rate_pct=100.1),
        )
        mgr.record_trade_result(False)
        mgr.record_trade_result(False)
        assert mgr.active is True


# ── Error Rate ────────────────────────────────────────────────


class TestErrorRate:
    def test_no_trigger_below_rate(self) -> None:
        mgr = KillSwitchManager(
            _ks(
                error_window_trades=10,
                max_error_rate_pct=50.0,
                max_consecutive_losses=999,
            ),
        )
        # Fill with successes first, then add failures — keeps rate below 50%
        # [T, T, T, T, T, T, F, F, F, F] → 4/10 = 40% < 50%
        for _ in range(6):
            mgr.record_trade_result(True)
        for _ in range(4):
            mgr.record_trade_result(False)
        assert mgr.active is False

    def test_triggers_at_rate(self) -> None:
        mgr = KillSwitchManager(
            _ks(
                error_window_trades=10,
                max_error_rate_pct=50.0,
                max_consecutive_losses=999,  # disable consecutive trigger
            ),
        )
        # 5 failures out of 10 = 50%
        for _ in range(5):
            mgr.record_trade_result(True)
        for _ in range(5):
            mgr.record_trade_result(False)
        assert mgr.active is True
        assert mgr.state.trigger == KillSwitchTrigger.ERROR_RATE

    def test_rolling_window(self) -> None:
        mgr = KillSwitchManager(
            _ks(
                error_window_trades=4,
                max_error_rate_pct=75.0,
                max_consecutive_losses=999,
            ),
        )
        # Fill window: [F, F, F, T] → 75% → triggers
        mgr.record_trade_result(False)
        mgr.record_trade_result(False)
        mgr.record_trade_result(False)
        # Still only 3 entries, 100% error rate → triggers at 3/3 = 100%
        # But let's check it's not triggered yet by checking the threshold
        # Actually 3/3 = 100% >= 75%, so it triggers
        assert mgr.active is True

    def test_empty_window_safe(self) -> None:
        mgr = KillSwitchManager(_ks())
        assert mgr.error_rate == 0.0
        assert mgr.check_error_rate() is False

    def test_custom_window_size(self) -> None:
        mgr = KillSwitchManager(
            _ks(
                error_window_trades=3,
                max_error_rate_pct=60.0,
                max_consecutive_losses=999,
            ),
        )
        # 2 out of 3 = 66.7% >= 60%
        mgr.record_trade_result(True)
        mgr.record_trade_result(False)
        mgr.record_trade_result(False)
        assert mgr.active is True


# ── Connectivity ──────────────────────────────────────────────


class TestConnectivity:
    def test_no_trigger_below_count(self) -> None:
        mgr = KillSwitchManager(_ks(connectivity_max_errors=3))
        mgr.record_api_error()
        mgr.record_api_error()
        assert mgr.active is False

    def test_triggers_at_count(self) -> None:
        mgr = KillSwitchManager(_ks(connectivity_max_errors=3))
        for _ in range(3):
            mgr.record_api_error()
        assert mgr.active is True
        assert mgr.state.trigger == KillSwitchTrigger.CONNECTIVITY

    def test_success_resets_counter(self) -> None:
        mgr = KillSwitchManager(_ks(connectivity_max_errors=3))
        mgr.record_api_error()
        mgr.record_api_error()
        mgr.record_api_success()
        mgr.record_api_error()
        mgr.record_api_error()
        # Only 2 consecutive errors, not 3
        assert mgr.active is False

    def test_custom_threshold(self) -> None:
        mgr = KillSwitchManager(_ks(connectivity_max_errors=1))
        mgr.record_api_error()
        assert mgr.active is True


# ── record_trade_result ───────────────────────────────────────


class TestRecordTradeResult:
    def test_success_resets_losses(self) -> None:
        mgr = KillSwitchManager(
            _ks(max_consecutive_losses=5, max_error_rate_pct=100.1),
        )
        for _ in range(3):
            mgr.record_trade_result(False)
        mgr.record_trade_result(True)
        assert mgr.consecutive_losses == 0

    def test_failure_increments(self) -> None:
        mgr = KillSwitchManager(
            _ks(max_consecutive_losses=10, max_error_rate_pct=100.1),
        )
        mgr.record_trade_result(False)
        assert mgr.consecutive_losses == 1
        mgr.record_trade_result(False)
        assert mgr.consecutive_losses == 2

    def test_returns_trigger_type_on_activation(self) -> None:
        mgr = KillSwitchManager(
            _ks(max_consecutive_losses=2, max_error_rate_pct=100.1),
        )
        mgr.record_trade_result(False)
        trigger = mgr.record_trade_result(False)
        assert trigger == KillSwitchTrigger.CONSECUTIVE_LOSSES

    def test_returns_none_when_already_active(self) -> None:
        mgr = KillSwitchManager(_ks())
        mgr.trigger("already active", KillSwitchTrigger.MANUAL)
        result = mgr.record_trade_result(False)
        assert result is None


# ── record_api_error ──────────────────────────────────────────


class TestRecordApiError:
    def test_increments_counter(self) -> None:
        mgr = KillSwitchManager(_ks(connectivity_max_errors=10))
        mgr.record_api_error()
        assert mgr._api_error_count == 1

    def test_triggers_at_threshold(self) -> None:
        mgr = KillSwitchManager(_ks(connectivity_max_errors=2))
        mgr.record_api_error()
        trigger = mgr.record_api_error()
        assert trigger == KillSwitchTrigger.CONNECTIVITY

    def test_success_resets(self) -> None:
        mgr = KillSwitchManager(_ks(connectivity_max_errors=3))
        mgr.record_api_error()
        mgr.record_api_error()
        mgr.record_api_success()
        assert mgr._api_error_count == 0
