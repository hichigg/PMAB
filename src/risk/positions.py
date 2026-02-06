"""PositionTracker — in-memory position tracking by token_id."""

from __future__ import annotations

import time
from decimal import Decimal

from src.core.types import ExecutionResult, Position


class PositionTracker:
    """Tracks open positions in memory, keyed by token_id."""

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}

    @property
    def positions(self) -> dict[str, Position]:
        """Read-only copy of open positions."""
        return dict(self._positions)

    @property
    def count(self) -> int:
        """Number of open positions."""
        return len(self._positions)

    def get(self, token_id: str) -> Position | None:
        """Look up a position by token_id."""
        return self._positions.get(token_id)

    def total_exposure_usd(self) -> Decimal:
        """Total exposure across all open positions (sum of price * size)."""
        return sum(
            (p.entry_price * p.size for p in self._positions.values()),
            Decimal(0),
        )

    def exposure_for_condition(self, condition_id: str) -> Decimal:
        """Total exposure for a specific condition (event)."""
        return sum(
            (
                p.entry_price * p.size
                for p in self._positions.values()
                if p.condition_id == condition_id
            ),
            Decimal(0),
        )

    def record_fill(self, result: ExecutionResult) -> Position | None:
        """Update positions based on an execution result.

        Returns the updated/opened Position, or None if the position was closed.
        """
        action = result.action
        token_id = action.token_id
        fill_price = result.fill_price or action.price
        fill_size = result.fill_size or action.size
        fill_side = action.side
        now = time.time()

        existing = self._positions.get(token_id)

        if existing is None:
            # Open new position
            condition_id = ""
            if action.signal and action.signal.match:
                condition_id = action.signal.match.opportunity.condition_id
            pos = Position(
                token_id=token_id,
                condition_id=condition_id,
                side=fill_side,
                entry_price=fill_price,
                size=fill_size,
                opened_at=now,
                last_updated=now,
            )
            self._positions[token_id] = pos
            return pos

        if existing.side == fill_side:
            # Same direction → average in
            total_size = existing.size + fill_size
            weighted_price = (
                (existing.entry_price * existing.size + fill_price * fill_size)
                / total_size
            )
            existing.entry_price = weighted_price
            existing.size = total_size
            existing.last_updated = now
            return existing

        # Opposite direction → reduce or close
        if fill_size >= existing.size:
            # Close position
            del self._positions[token_id]
            return None

        # Partial reduce
        existing.size = existing.size - fill_size
        existing.last_updated = now
        return existing

    def clear(self) -> None:
        """Reset all positions."""
        self._positions.clear()
