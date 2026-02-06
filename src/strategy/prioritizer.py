"""OpportunityPrioritizer — scores, ranks, caps, and cooldown-filters matches."""

from __future__ import annotations

import time
from decimal import Decimal

import structlog

from src.core.config import PrioritizerConfig
from src.core.types import MatchResult, PrioritizedMatch

logger = structlog.stdlib.get_logger()


def _estimate_edge(match: MatchResult) -> float:
    """Estimate edge from market price vs assumed fair value (~0.99).

    For BUY signals: edge = (0.99 - best_ask) / 0.99, clamped [0, 1].
    Returns 0.0 when price data is unavailable.
    """
    ask = match.opportunity.best_ask
    if ask is None:
        return 0.0
    fair = Decimal("0.99")
    if ask >= fair:
        return 0.0
    raw = float((fair - ask) / fair)
    return min(max(raw, 0.0), 1.0)


def compute_priority_score(
    match: MatchResult,
    config: PrioritizerConfig,
) -> tuple[float, dict[str, float]]:
    """Compute a composite priority score for a match.

    Returns (total_score, component_dict) where components are:
    - opportunity: scanner's opp.score (liquidity/depth/spread)
    - confidence: match_confidence from matcher
    - edge: estimated edge from market price
    - category: lookup from category_weights config
    """
    opp_score = match.opportunity.score
    confidence = match.match_confidence
    edge = _estimate_edge(match)
    cat_key = match.opportunity.category.value
    cat_weight = config.category_weights.get(cat_key, 0.3)

    components = {
        "opportunity": opp_score,
        "confidence": confidence,
        "edge": edge,
        "category": cat_weight,
    }

    total = (
        config.score_weight_opportunity * opp_score
        + config.score_weight_confidence * confidence
        + config.score_weight_edge * edge
        + config.score_weight_category * cat_weight
    )

    return total, components


class OpportunityPrioritizer:
    """Ranks matches by composite priority, enforces trade caps and cooldowns."""

    def __init__(self, config: PrioritizerConfig | None = None) -> None:
        self._config = config or PrioritizerConfig()
        self._cooldowns: dict[str, float] = {}

    @property
    def cooldowns(self) -> dict[str, float]:
        """Read-only copy of active cooldowns {condition_id: expiry_ts}."""
        return dict(self._cooldowns)

    def prioritize(self, matches: list[MatchResult]) -> list[PrioritizedMatch]:
        """Score, filter cooldowns, sort by priority descending, and cap.

        Returns a list of PrioritizedMatch with 1-indexed ranks.
        """
        if not matches:
            return []

        now = time.time()
        filtered = self._filter_cooldowns(matches, now)

        if not filtered:
            return []

        scored: list[tuple[float, dict[str, float], MatchResult]] = []
        for m in filtered:
            total, components = compute_priority_score(m, self._config)
            scored.append((total, components, m))

        scored.sort(key=lambda x: x[0], reverse=True)

        cap = self._config.max_trades_per_event
        capped = scored[:cap]

        result: list[PrioritizedMatch] = []
        for rank_idx, (total, components, m) in enumerate(capped, start=1):
            result.append(PrioritizedMatch(
                match=m,
                priority_score=total,
                score_components=components,
                rank=rank_idx,
            ))

        logger.debug(
            "prioritized_matches",
            total=len(matches),
            after_cooldown=len(filtered),
            after_cap=len(result),
        )

        return result

    def record_trade(self, condition_id: str) -> None:
        """Start a cooldown timer for the given condition_id."""
        self._cooldowns[condition_id] = time.time() + self._config.cooldown_secs
        logger.debug(
            "cooldown_started",
            condition_id=condition_id,
            cooldown_secs=self._config.cooldown_secs,
        )

    def clear_cooldown(self, condition_id: str) -> None:
        """Manually clear cooldown for a specific condition_id."""
        self._cooldowns.pop(condition_id, None)

    def clear_all_cooldowns(self) -> None:
        """Clear all active cooldowns."""
        self._cooldowns.clear()

    def _filter_cooldowns(
        self,
        matches: list[MatchResult],
        now: float,
    ) -> list[MatchResult]:
        """Remove matches on cooldown and clean up expired entries."""
        # Clean expired cooldowns
        expired = [
            cid for cid, expiry in self._cooldowns.items() if expiry <= now
        ]
        for cid in expired:
            del self._cooldowns[cid]

        # Filter out matches still on cooldown
        result: list[MatchResult] = []
        for m in matches:
            cid = m.opportunity.condition_id
            if cid in self._cooldowns:
                logger.debug("match_on_cooldown", condition_id=cid)
            else:
                result.append(m)

        return result
