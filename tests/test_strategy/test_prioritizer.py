"""Tests for OpportunityPrioritizer — scoring, ranking, cooldowns, capping."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from src.core.config import PrioritizerConfig
from src.core.types import (
    FeedEvent,
    FeedEventType,
    FeedType,
    MarketCategory,
    MarketOpportunity,
    MatchResult,
    OutcomeType,
)
from src.strategy.prioritizer import (
    OpportunityPrioritizer,
    _estimate_edge,
    compute_priority_score,
)

# ── Helpers ─────────────────────────────────────────────────────


def _tokens() -> list[dict[str, Any]]:
    return [
        {"token_id": "0xyes", "outcome": "Yes"},
        {"token_id": "0xno", "outcome": "No"},
    ]


def _opp(
    condition_id: str = "cond1",
    question: str = "Will CPI be above 3.0%?",
    best_bid: Decimal | None = Decimal("0.40"),
    best_ask: Decimal | None = Decimal("0.45"),
    depth_usd: Decimal = Decimal("5000"),
    category: MarketCategory = MarketCategory.ECONOMIC,
    score: float = 0.7,
) -> MarketOpportunity:
    return MarketOpportunity(
        condition_id=condition_id,
        question=question,
        category=category,
        tokens=_tokens(),
        best_bid=best_bid,
        best_ask=best_ask,
        depth_usd=depth_usd,
        score=score,
    )


def _event(
    feed_type: FeedType = FeedType.ECONOMIC,
    indicator: str = "CPI",
) -> FeedEvent:
    return FeedEvent(
        feed_type=feed_type,
        event_type=FeedEventType.DATA_RELEASED,
        indicator=indicator,
        value="3.5",
        numeric_value=Decimal("3.5"),
        outcome_type=OutcomeType.NUMERIC,
        received_at=time.time(),
    )


def _match(
    condition_id: str = "cond1",
    confidence: float = 0.9,
    best_ask: Decimal | None = Decimal("0.45"),
    category: MarketCategory = MarketCategory.ECONOMIC,
    score: float = 0.7,
) -> MatchResult:
    return MatchResult(
        feed_event=_event(),
        opportunity=_opp(
            condition_id=condition_id,
            best_ask=best_ask,
            category=category,
            score=score,
        ),
        target_token_id="0xyes",
        target_outcome="Yes",
        match_confidence=confidence,
        match_reason="CPI above threshold",
    )


def _cfg(**overrides: object) -> PrioritizerConfig:
    defaults: dict[str, object] = {
        "max_trades_per_event": 3,
        "cooldown_secs": 300.0,
    }
    defaults.update(overrides)
    return PrioritizerConfig(**defaults)  # type: ignore[arg-type]


# ── TestComputePriorityScore ────────────────────────────────────


class TestComputePriorityScore:
    def test_basic_score(self) -> None:
        """Score is a float between 0 and 1."""
        m = _match()
        config = _cfg()
        total, components = compute_priority_score(m, config)
        assert isinstance(total, float)
        assert total > 0.0

    def test_all_components_present(self) -> None:
        """All four score components are returned."""
        m = _match()
        config = _cfg()
        _, components = compute_priority_score(m, config)
        assert "opportunity" in components
        assert "confidence" in components
        assert "edge" in components
        assert "category" in components

    def test_high_beats_low(self) -> None:
        """Higher confidence + cheaper ask → higher score."""
        high = _match(confidence=0.95, best_ask=Decimal("0.30"), score=0.9)
        low = _match(confidence=0.50, best_ask=Decimal("0.85"), score=0.2)
        config = _cfg()
        h_score, _ = compute_priority_score(high, config)
        l_score, _ = compute_priority_score(low, config)
        assert h_score > l_score

    def test_economic_weight(self) -> None:
        """ECONOMIC category gets weight 1.0."""
        m = _match(category=MarketCategory.ECONOMIC)
        config = _cfg()
        _, components = compute_priority_score(m, config)
        assert components["category"] == 1.0

    def test_crypto_weight(self) -> None:
        """CRYPTO category gets weight 0.7."""
        m = _match(category=MarketCategory.CRYPTO)
        config = _cfg()
        _, components = compute_priority_score(m, config)
        assert components["category"] == 0.7

    def test_other_weight(self) -> None:
        """OTHER category gets weight 0.3."""
        m = _match(category=MarketCategory.OTHER)
        config = _cfg()
        _, components = compute_priority_score(m, config)
        assert components["category"] == 0.3

    def test_custom_weights(self) -> None:
        """Custom score weights change the total."""
        m = _match()
        config_a = _cfg(
            score_weight_opportunity=1.0,
            score_weight_confidence=0.0,
            score_weight_edge=0.0,
            score_weight_category=0.0,
        )
        config_b = _cfg(
            score_weight_opportunity=0.0,
            score_weight_confidence=1.0,
            score_weight_edge=0.0,
            score_weight_category=0.0,
        )
        score_a, _ = compute_priority_score(m, config_a)
        score_b, _ = compute_priority_score(m, config_b)
        # opportunity score (0.7) vs confidence (0.9) — different totals
        assert score_a != score_b

    def test_zero_opp_score(self) -> None:
        """Zero opportunity score contributes nothing to that component."""
        m = _match(score=0.0)
        config = _cfg()
        _, components = compute_priority_score(m, config)
        assert components["opportunity"] == 0.0


# ── TestEstimateEdge ────────────────────────────────────────────


class TestEstimateEdge:
    def test_buy_edge_from_ask(self) -> None:
        """Edge calculated from ask price vs 0.99 fair value."""
        m = _match(best_ask=Decimal("0.50"))
        edge = _estimate_edge(m)
        expected = (0.99 - 0.50) / 0.99
        assert abs(edge - expected) < 0.001

    def test_no_ask(self) -> None:
        """Returns 0.0 when ask is None."""
        m = _match(best_ask=None)
        edge = _estimate_edge(m)
        assert edge == 0.0

    def test_already_priced(self) -> None:
        """Edge is 0.0 when ask >= 0.99 (no edge)."""
        m = _match(best_ask=Decimal("0.99"))
        edge = _estimate_edge(m)
        assert edge == 0.0

    def test_clamped_to_zero(self) -> None:
        """Edge clamped to 0 when ask > fair value."""
        m = _match(best_ask=Decimal("1.00"))
        edge = _estimate_edge(m)
        assert edge == 0.0

    def test_very_cheap_ask(self) -> None:
        """Very cheap ask → high edge, clamped to 1.0 max."""
        m = _match(best_ask=Decimal("0.01"))
        edge = _estimate_edge(m)
        assert 0.0 < edge <= 1.0


# ── TestPrioritize ──────────────────────────────────────────────


class TestPrioritize:
    def test_empty_input(self) -> None:
        """Empty match list → empty output."""
        p = OpportunityPrioritizer(_cfg())
        assert p.prioritize([]) == []

    def test_single_match(self) -> None:
        """Single match gets rank 1."""
        p = OpportunityPrioritizer(_cfg())
        result = p.prioritize([_match()])
        assert len(result) == 1
        assert result[0].rank == 1

    def test_sorted_by_score(self) -> None:
        """Higher-scored match comes first."""
        p = OpportunityPrioritizer(_cfg())
        low = _match(condition_id="low", confidence=0.3, best_ask=Decimal("0.90"), score=0.1)
        high = _match(condition_id="high", confidence=0.95, best_ask=Decimal("0.30"), score=0.9)
        result = p.prioritize([low, high])
        assert result[0].match.opportunity.condition_id == "high"
        assert result[1].match.opportunity.condition_id == "low"

    def test_cap_enforced(self) -> None:
        """Max trades per event caps the output."""
        config = _cfg(max_trades_per_event=2)
        p = OpportunityPrioritizer(config)
        matches = [_match(condition_id=f"c{i}") for i in range(5)]
        result = p.prioritize(matches)
        assert len(result) == 2

    def test_ranks_assigned(self) -> None:
        """Ranks are 1-indexed and sequential."""
        p = OpportunityPrioritizer(_cfg(max_trades_per_event=5))
        matches = [_match(condition_id=f"c{i}") for i in range(4)]
        result = p.prioritize(matches)
        ranks = [pm.rank for pm in result]
        assert ranks == [1, 2, 3, 4]

    def test_all_on_cooldown(self) -> None:
        """All matches on cooldown → empty output."""
        p = OpportunityPrioritizer(_cfg())
        p.record_trade("cond1")
        result = p.prioritize([_match(condition_id="cond1")])
        assert result == []

    def test_partial_cooldown(self) -> None:
        """Only cooled-down matches are filtered."""
        p = OpportunityPrioritizer(_cfg())
        p.record_trade("c1")
        matches = [
            _match(condition_id="c1"),
            _match(condition_id="c2"),
        ]
        result = p.prioritize(matches)
        assert len(result) == 1
        assert result[0].match.opportunity.condition_id == "c2"

    def test_default_cap_is_3(self) -> None:
        """Default max_trades_per_event is 3."""
        p = OpportunityPrioritizer()
        matches = [_match(condition_id=f"c{i}") for i in range(10)]
        result = p.prioritize(matches)
        assert len(result) == 3

    def test_components_populated(self) -> None:
        """Score components dict is populated in output."""
        p = OpportunityPrioritizer(_cfg())
        result = p.prioritize([_match()])
        assert result[0].score_components
        assert "opportunity" in result[0].score_components
        assert "edge" in result[0].score_components

    def test_deterministic_ordering(self) -> None:
        """Same inputs produce same ordering."""
        p = OpportunityPrioritizer(_cfg())
        matches = [
            _match(condition_id="a", confidence=0.8, score=0.5),
            _match(condition_id="b", confidence=0.9, score=0.6),
            _match(condition_id="c", confidence=0.7, score=0.8),
        ]
        r1 = p.prioritize(list(matches))
        r2 = p.prioritize(list(matches))
        ids_1 = [pm.match.opportunity.condition_id for pm in r1]
        ids_2 = [pm.match.opportunity.condition_id for pm in r2]
        assert ids_1 == ids_2


# ── TestCooldown ────────────────────────────────────────────────


class TestCooldown:
    def test_no_cooldown_initially(self) -> None:
        """Fresh prioritizer has no cooldowns."""
        p = OpportunityPrioritizer(_cfg())
        assert p.cooldowns == {}

    def test_record_starts_cooldown(self) -> None:
        """record_trade adds a cooldown entry."""
        p = OpportunityPrioritizer(_cfg())
        p.record_trade("cond1")
        assert "cond1" in p.cooldowns

    def test_cooldown_expires(self) -> None:
        """Expired cooldowns are cleaned during prioritize."""
        config = _cfg(cooldown_secs=0.0)  # Expire immediately
        p = OpportunityPrioritizer(config)
        p.record_trade("cond1")
        # Cooldown already expired (0 seconds)
        result = p.prioritize([_match(condition_id="cond1")])
        assert len(result) == 1
        assert "cond1" not in p.cooldowns

    def test_clear_cooldown(self) -> None:
        """clear_cooldown removes a specific entry."""
        p = OpportunityPrioritizer(_cfg())
        p.record_trade("cond1")
        p.record_trade("cond2")
        p.clear_cooldown("cond1")
        assert "cond1" not in p.cooldowns
        assert "cond2" in p.cooldowns

    def test_clear_all_cooldowns(self) -> None:
        """clear_all_cooldowns removes everything."""
        p = OpportunityPrioritizer(_cfg())
        p.record_trade("cond1")
        p.record_trade("cond2")
        p.clear_all_cooldowns()
        assert p.cooldowns == {}

    def test_cooldowns_property_is_copy(self) -> None:
        """cooldowns property returns a copy, not the internal dict."""
        p = OpportunityPrioritizer(_cfg())
        p.record_trade("cond1")
        copy = p.cooldowns
        copy["injected"] = 999.0
        assert "injected" not in p.cooldowns

    def test_expired_cleaned_during_filter(self) -> None:
        """Expired cooldown entries are removed during prioritize."""
        config = _cfg(cooldown_secs=0.0)
        p = OpportunityPrioritizer(config)
        p.record_trade("cond1")
        p.record_trade("cond2")
        # Both should have expired by now (0 sec cooldown)
        p.prioritize([_match(condition_id="cond3")])
        assert "cond1" not in p.cooldowns
        assert "cond2" not in p.cooldowns
