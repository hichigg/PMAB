"""Event-to-market matching — maps feed events to Polymarket opportunities."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from src.core.types import (
    FeedEvent,
    FeedType,
    MarketCategory,
    MarketOpportunity,
    MatchResult,
)

logger = structlog.stdlib.get_logger()

# Regex for threshold extraction: "CPI above 3.0%", "BTC over $50,000", etc.
_THRESHOLD_PATTERN = re.compile(
    r"(above|below|over|under|exceed|exceeds?)\s+"
    r"\$?([\d,]+(?:\.\d+)?)\s*%?",
    re.IGNORECASE,
)

# Direction words → canonical direction
_DIRECTION_MAP: dict[str, str] = {
    "above": "above",
    "over": "above",
    "exceed": "above",
    "exceeds": "above",
    "below": "below",
    "under": "below",
}

# Articles to strip for team name normalization
_ARTICLES = {"the", "a", "an"}


def _extract_threshold_from_question(
    question: str,
) -> tuple[Decimal, str] | None:
    """Parse a threshold and direction from a market question.

    Examples:
        "Will CPI be above 3.0%?" → (Decimal("3.0"), "above")
        "Will BTC exceed $50,000?" → (Decimal("50000"), "above")
        "Will unemployment fall below 4.5%?" → (Decimal("4.5"), "below")

    Returns None if no threshold pattern is found.
    """
    match = _THRESHOLD_PATTERN.search(question)
    if match is None:
        return None

    direction_word = match.group(1).lower()
    raw_value = match.group(2).replace(",", "")

    try:
        threshold = Decimal(raw_value)
    except InvalidOperation:
        return None

    direction = _DIRECTION_MAP.get(direction_word, "above")
    return threshold, direction


def _normalize_team_name(name: str) -> str:
    """Normalize a team name for fuzzy matching.

    Lowercases, strips leading articles, and collapses whitespace.
    """
    words = name.lower().split()
    words = [w for w in words if w not in _ARTICLES]
    return " ".join(words).strip()


def _team_in_question(team_name: str, question: str) -> bool:
    """Check if a team name appears in a market question (fuzzy)."""
    normalized = _normalize_team_name(team_name)
    question_lower = question.lower()
    if not normalized:
        return False
    return normalized in question_lower


def _find_token_for_outcome(
    tokens: list[dict[str, Any]], outcome: str,
) -> str | None:
    """Find the token_id whose outcome field matches (case-insensitive).

    Polymarket tokens look like:
        [{"token_id": "0xabc", "outcome": "Yes"}, {"token_id": "0xdef", "outcome": "No"}]
    """
    outcome_lower = outcome.lower()
    for token in tokens:
        token_outcome = str(token.get("outcome", "")).lower()
        if token_outcome == outcome_lower:
            return str(token.get("token_id", ""))
    return None


class MarketMatcher:
    """Matches feed events to tracked market opportunities."""

    def __init__(self, match_confidence_threshold: float = 0.8) -> None:
        self._threshold = match_confidence_threshold

    def match(
        self,
        event: FeedEvent,
        opportunities: dict[str, MarketOpportunity],
    ) -> list[MatchResult]:
        """Match a feed event against known opportunities.

        Dispatches to category-specific matchers based on event.feed_type.
        """
        if event.feed_type == FeedType.ECONOMIC:
            return self._match_economic(event, opportunities)
        if event.feed_type == FeedType.SPORTS:
            return self._match_sports(event, opportunities)
        if event.feed_type == FeedType.CRYPTO:
            return self._match_crypto(event, opportunities)
        return []

    def _match_economic(
        self,
        event: FeedEvent,
        opportunities: dict[str, MarketOpportunity],
    ) -> list[MatchResult]:
        """Match economic data releases to ECONOMIC-category markets."""
        results: list[MatchResult] = []
        indicator_lower = event.indicator.lower()

        for opp in opportunities.values():
            if opp.category != MarketCategory.ECONOMIC:
                continue

            question_lower = opp.question.lower()
            if indicator_lower not in question_lower:
                continue

            # Parse threshold from question
            threshold_info = _extract_threshold_from_question(opp.question)
            if threshold_info is None or event.numeric_value is None:
                continue

            threshold, direction = threshold_info

            # Determine which outcome is correct
            if direction == "above":
                outcome = "Yes" if event.numeric_value > threshold else "No"
            else:
                outcome = "Yes" if event.numeric_value < threshold else "No"

            token_id = _find_token_for_outcome(opp.tokens, outcome)
            if token_id is None:
                continue

            match_result = MatchResult(
                feed_event=event,
                opportunity=opp,
                target_token_id=token_id,
                target_outcome=outcome,
                match_confidence=0.95,
                match_reason=(
                    f"{event.indicator}={event.numeric_value} "
                    f"vs threshold {threshold} ({direction}) → {outcome}"
                ),
            )
            if match_result.match_confidence >= self._threshold:
                results.append(match_result)

        return results

    def _match_sports(
        self,
        event: FeedEvent,
        opportunities: dict[str, MarketOpportunity],
    ) -> list[MatchResult]:
        """Match sports results to SPORTS-category markets."""
        results: list[MatchResult] = []
        winner = event.metadata.get("winner", "")
        home_team = event.metadata.get("home_team", "")
        away_team = event.metadata.get("away_team", "")

        if not winner:
            return results

        for opp in opportunities.values():
            if opp.category != MarketCategory.SPORTS:
                continue

            # Check if either team appears in the question
            home_in = _team_in_question(home_team, opp.question) if home_team else False
            away_in = _team_in_question(away_team, opp.question) if away_team else False

            if not home_in and not away_in:
                continue

            # Try to find a token matching the winner's name
            token_id = _find_token_for_outcome(opp.tokens, winner)

            # Fallback: if winner matches one team, try "Yes" outcome
            if token_id is None:
                if _team_in_question(winner, opp.question):
                    token_id = _find_token_for_outcome(opp.tokens, "Yes")
                    outcome = "Yes"
                else:
                    token_id = _find_token_for_outcome(opp.tokens, "No")
                    outcome = "No"
            else:
                outcome = winner

            if token_id is None:
                continue

            match_result = MatchResult(
                feed_event=event,
                opportunity=opp,
                target_token_id=token_id,
                target_outcome=outcome,
                match_confidence=0.95,
                match_reason=f"Winner={winner}, outcome={outcome}",
            )
            if match_result.match_confidence >= self._threshold:
                results.append(match_result)

        return results

    def _match_crypto(
        self,
        event: FeedEvent,
        opportunities: dict[str, MarketOpportunity],
    ) -> list[MatchResult]:
        """Match crypto price moves to CRYPTO-category markets."""
        results: list[MatchResult] = []
        pair = event.indicator.upper()

        for opp in opportunities.values():
            if opp.category != MarketCategory.CRYPTO:
                continue

            # Check if the crypto pair is referenced in the question
            question_upper = opp.question.upper()
            # Match common representations: BTC, BITCOIN, BTC_USDT, etc.
            pair_parts = pair.replace("_", " ").split()
            base_symbol = pair_parts[0] if pair_parts else pair

            if base_symbol not in question_upper:
                continue

            # Parse threshold from question
            threshold_info = _extract_threshold_from_question(opp.question)
            if threshold_info is None or event.numeric_value is None:
                continue

            threshold, direction = threshold_info

            if direction == "above":
                outcome = "Yes" if event.numeric_value > threshold else "No"
            else:
                outcome = "Yes" if event.numeric_value < threshold else "No"

            token_id = _find_token_for_outcome(opp.tokens, outcome)
            if token_id is None:
                continue

            match_result = MatchResult(
                feed_event=event,
                opportunity=opp,
                target_token_id=token_id,
                target_outcome=outcome,
                match_confidence=0.90,
                match_reason=(
                    f"{pair}={event.numeric_value} "
                    f"vs threshold {threshold} ({direction}) → {outcome}"
                ),
            )
            if match_result.match_confidence >= self._threshold:
                results.append(match_result)

        return results
