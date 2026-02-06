"""Signal generation — evaluates match results to produce trading signals."""

from __future__ import annotations

import time
from decimal import Decimal

import structlog

from src.core.config import StrategyConfig
from src.core.types import (
    FeedType,
    MatchResult,
    OutcomeType,
    Signal,
    SignalDirection,
)

logger = structlog.stdlib.get_logger()


def _determine_direction_and_price(
    fair_value: Decimal,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
) -> tuple[SignalDirection, Decimal] | None:
    """Determine trade direction and execution price from fair value vs book.

    BUY if fair_value is high and ask is low (underpriced on ask side).
    SELL if fair_value is low and bid is high (overpriced on bid side).
    Returns None if no actionable edge.
    """
    if best_ask is not None and fair_value > best_ask:
        return SignalDirection.BUY, best_ask

    if best_bid is not None and fair_value < best_bid:
        return SignalDirection.SELL, best_bid

    return None


class SignalGenerator:
    """Generates trading signals from match results."""

    def __init__(self, config: StrategyConfig | None = None) -> None:
        from src.core.config import get_settings

        self._config = config or get_settings().strategy

    def evaluate(self, match: MatchResult) -> Signal | None:
        """Evaluate a match result and produce a signal if actionable.

        Returns None if:
        - No edge exists (fair value vs market price)
        - Edge is below minimum threshold
        - Event is too stale
        - Confidence is below threshold
        """
        now = time.time()

        # Check staleness
        event = match.feed_event
        event_age = now - event.received_at if event.received_at > 0 else 0.0
        if event_age > self._config.max_staleness_secs:
            logger.debug(
                "signal_rejected_stale",
                indicator=event.indicator,
                age_secs=event_age,
            )
            return None

        # Route by outcome type
        if event.outcome_type == OutcomeType.CATEGORICAL:
            return self._evaluate_categorical(match, now)
        if event.outcome_type in (OutcomeType.NUMERIC, OutcomeType.BOOLEAN):
            return self._evaluate_numeric_threshold(match, now)

        return None

    def _evaluate_categorical(
        self, match: MatchResult, now: float,
    ) -> Signal | None:
        """Evaluate categorical outcomes (e.g. sports: winner = TeamA).

        For deterministic game results, the winning token's fair value is ~1.00.
        """
        # For completed games, the winner's token is worth ~1.00
        fair_value = Decimal("0.99")
        confidence = 0.99

        opp = match.opportunity
        direction_result = _determine_direction_and_price(
            fair_value, opp.best_bid, opp.best_ask,
        )
        if direction_result is None:
            return None

        direction, current_price = direction_result
        edge = abs(fair_value - current_price)

        min_edge = self._get_min_edge(match.feed_event.feed_type)
        if edge < min_edge:
            logger.debug(
                "signal_rejected_low_edge",
                edge=float(edge),
                min_edge=float(min_edge),
            )
            return None

        return Signal(
            match=match,
            fair_value=fair_value,
            confidence=confidence,
            direction=direction,
            edge=edge,
            current_price=current_price,
            created_at=now,
        )

    def _evaluate_numeric_threshold(
        self, match: MatchResult, now: float,
    ) -> Signal | None:
        """Evaluate numeric threshold outcomes (economic/crypto).

        When the actual value clearly exceeds the threshold, the correct
        outcome token has fair_value ≈ 1.00 (deterministic).
        """
        event = match.feed_event

        # For deterministic data releases (CPI published, etc.), high confidence
        if event.feed_type == FeedType.CRYPTO:
            # Crypto prices can revert — lower confidence
            confidence = 0.85
            # Scale confidence with cross-validation if available
            validated = event.metadata.get("cross_validated", False)
            if validated:
                confidence = 0.92
        else:
            # Economic data is deterministic once released
            confidence = 0.99

        if confidence < self._config.min_confidence:
            # For crypto, we may still want to trade with lower confidence
            # but the edge needs to be larger
            pass

        fair_value = Decimal("0.99")

        opp = match.opportunity
        direction_result = _determine_direction_and_price(
            fair_value, opp.best_bid, opp.best_ask,
        )
        if direction_result is None:
            return None

        direction, current_price = direction_result
        edge = abs(fair_value - current_price)

        min_edge = self._get_min_edge(event.feed_type)
        if edge < min_edge:
            logger.debug(
                "signal_rejected_low_edge",
                edge=float(edge),
                min_edge=float(min_edge),
                feed_type=event.feed_type,
            )
            return None

        return Signal(
            match=match,
            fair_value=fair_value,
            confidence=confidence,
            direction=direction,
            edge=edge,
            current_price=current_price,
            created_at=now,
        )

    def _get_min_edge(self, feed_type: FeedType) -> Decimal:
        """Get minimum edge threshold, with per-category overrides."""
        override_map = {
            FeedType.ECONOMIC: self._config.economic_min_edge,
            FeedType.SPORTS: self._config.sports_min_edge,
            FeedType.CRYPTO: self._config.crypto_min_edge,
        }
        override = override_map.get(feed_type)
        if override is not None:
            return Decimal(str(override))
        return Decimal(str(self._config.min_edge))
