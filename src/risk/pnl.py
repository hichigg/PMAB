"""PnLTracker — realized P&L tracking with UTC-day auto-reset."""

from __future__ import annotations

import time
from decimal import Decimal

from src.core.types import ExecutionResult, Position, Side


class PnLTracker:
    """Tracks realized and unrealized P&L with daily reset."""

    def __init__(self) -> None:
        self.realized_total: Decimal = Decimal(0)
        self.realized_today: Decimal = Decimal(0)
        self.trade_count_today: int = 0
        self._day_start: float = self._current_day_start()

    @staticmethod
    def _current_day_start() -> float:
        """Return the UTC epoch of the start of the current day."""
        now = time.time()
        return now - (now % 86400)

    def _maybe_reset_day(self) -> None:
        """Reset daily counters if the UTC day has rolled over."""
        now = time.time()
        if now >= self._day_start + 86400:
            self.realized_today = Decimal(0)
            self.trade_count_today = 0
            self._day_start = self._current_day_start()

    def record_fill(
        self,
        result: ExecutionResult,
        existing_position: Position | None,
    ) -> Decimal:
        """Compute and record realized P&L for a fill.

        Opening fills (no existing position or same direction) produce 0 P&L.
        Closing fills compute P&L based on entry vs fill price.

        Returns the realized P&L amount for this fill.
        """
        self._maybe_reset_day()
        self.trade_count_today += 1

        if existing_position is None:
            # Opening fill — no realized P&L
            return Decimal(0)

        action = result.action
        fill_price = result.fill_price or action.price
        fill_size = result.fill_size or action.size

        if existing_position.side == action.side:
            # Same direction (averaging in) — no realized P&L
            return Decimal(0)

        # Closing fill: compute realized P&L
        close_size = min(fill_size, existing_position.size)

        if existing_position.side == Side.BUY:
            # Bought at entry, selling at fill → profit = (fill - entry) * size
            realized = (fill_price - existing_position.entry_price) * close_size
        else:
            # Sold at entry, buying to close → profit = (entry - fill) * size
            realized = (existing_position.entry_price - fill_price) * close_size

        self.realized_today += realized
        self.realized_total += realized
        return realized

    def unrealized_pnl(
        self,
        positions: dict[str, Position],
        current_prices: dict[str, Decimal],
    ) -> Decimal:
        """Compute mark-to-market unrealized P&L across all positions.

        Args:
            positions: Open positions keyed by token_id.
            current_prices: Current market prices keyed by token_id.

        Returns:
            Total unrealized P&L.
        """
        total = Decimal(0)
        for token_id, pos in positions.items():
            current = current_prices.get(token_id)
            if current is None:
                continue
            if pos.side == Side.BUY:
                total += (current - pos.entry_price) * pos.size
            else:
                total += (pos.entry_price - current) * pos.size
        return total

    def reset(self) -> None:
        """Reset all state (for testing)."""
        self.realized_total = Decimal(0)
        self.realized_today = Decimal(0)
        self.trade_count_today = 0
        self._day_start = self._current_day_start()
