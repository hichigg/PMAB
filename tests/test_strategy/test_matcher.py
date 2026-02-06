"""Tests for MarketMatcher — event-to-market matching logic."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.core.types import (
    FeedEvent,
    FeedEventType,
    FeedType,
    MarketCategory,
    MarketOpportunity,
    OutcomeType,
)
from src.strategy.matcher import (
    MarketMatcher,
    _extract_threshold_from_question,
    _find_token_for_outcome,
    _normalize_team_name,
    _team_in_question,
)

# ── Helpers ─────────────────────────────────────────────────────


def _tokens(
    yes_id: str = "0xyes", no_id: str = "0xno",
) -> list[dict[str, Any]]:
    """Build standard Yes/No token list."""
    return [
        {"token_id": yes_id, "outcome": "Yes"},
        {"token_id": no_id, "outcome": "No"},
    ]


def _team_tokens(
    team_a: str = "Lakers",
    team_b: str = "Celtics",
    id_a: str = "0xlakers",
    id_b: str = "0xceltics",
) -> list[dict[str, Any]]:
    """Build team-name token list."""
    return [
        {"token_id": id_a, "outcome": team_a},
        {"token_id": id_b, "outcome": team_b},
    ]


def _opp(
    condition_id: str = "cond1",
    question: str = "Will CPI be above 3.0%?",
    category: MarketCategory = MarketCategory.ECONOMIC,
    tokens: list[dict[str, Any]] | None = None,
    best_bid: Decimal | None = Decimal("0.40"),
    best_ask: Decimal | None = Decimal("0.45"),
    depth_usd: Decimal = Decimal("5000"),
) -> MarketOpportunity:
    if tokens is None:
        tokens = _tokens()
    return MarketOpportunity(
        condition_id=condition_id,
        question=question,
        category=category,
        tokens=tokens,
        best_bid=best_bid,
        best_ask=best_ask,
        depth_usd=depth_usd,
    )


def _economic_event(
    indicator: str = "CPI",
    value: str = "3.5",
    numeric_value: Decimal | None = Decimal("3.5"),
) -> FeedEvent:
    return FeedEvent(
        feed_type=FeedType.ECONOMIC,
        event_type=FeedEventType.DATA_RELEASED,
        indicator=indicator,
        value=value,
        numeric_value=numeric_value,
        outcome_type=OutcomeType.NUMERIC,
        received_at=1000.0,
    )


def _sports_event(
    winner: str = "Lakers",
    home_team: str = "Lakers",
    away_team: str = "Celtics",
) -> FeedEvent:
    return FeedEvent(
        feed_type=FeedType.SPORTS,
        event_type=FeedEventType.DATA_RELEASED,
        indicator="NBA",
        value=winner,
        outcome_type=OutcomeType.CATEGORICAL,
        received_at=1000.0,
        metadata={
            "winner": winner,
            "home_team": home_team,
            "away_team": away_team,
        },
    )


def _crypto_event(
    pair: str = "BTC_USDT",
    price: Decimal = Decimal("55000"),
) -> FeedEvent:
    return FeedEvent(
        feed_type=FeedType.CRYPTO,
        event_type=FeedEventType.DATA_RELEASED,
        indicator=pair,
        value=str(price),
        numeric_value=price,
        outcome_type=OutcomeType.NUMERIC,
        received_at=1000.0,
    )


# ── _extract_threshold_from_question ────────────────────────────


class TestExtractThreshold:
    def test_above_percentage(self) -> None:
        result = _extract_threshold_from_question("Will CPI be above 3.0%?")
        assert result is not None
        assert result[0] == Decimal("3.0")
        assert result[1] == "above"

    def test_below_percentage(self) -> None:
        result = _extract_threshold_from_question(
            "Will unemployment fall below 4.5%?"
        )
        assert result is not None
        assert result[0] == Decimal("4.5")
        assert result[1] == "below"

    def test_over_dollar(self) -> None:
        result = _extract_threshold_from_question("Will BTC go over $50,000?")
        assert result is not None
        assert result[0] == Decimal("50000")
        assert result[1] == "above"

    def test_under_dollar(self) -> None:
        result = _extract_threshold_from_question("Will ETH drop under $3,000?")
        assert result is not None
        assert result[0] == Decimal("3000")
        assert result[1] == "below"

    def test_exceed(self) -> None:
        result = _extract_threshold_from_question("Will GDP exceed 2.5%?")
        assert result is not None
        assert result[0] == Decimal("2.5")
        assert result[1] == "above"

    def test_dollar_with_commas(self) -> None:
        result = _extract_threshold_from_question(
            "Will BTC exceed $100,000?"
        )
        assert result is not None
        assert result[0] == Decimal("100000")

    def test_no_threshold(self) -> None:
        result = _extract_threshold_from_question("Who will win the election?")
        assert result is None

    def test_decimal_value(self) -> None:
        result = _extract_threshold_from_question(
            "Will inflation be above 3.25%?"
        )
        assert result is not None
        assert result[0] == Decimal("3.25")


# ── _normalize_team_name ────────────────────────────────────────


class TestNormalizeTeamName:
    def test_basic(self) -> None:
        assert _normalize_team_name("Lakers") == "lakers"

    def test_strips_articles(self) -> None:
        assert _normalize_team_name("The Los Angeles Lakers") == "los angeles lakers"

    def test_case_insensitive(self) -> None:
        assert _normalize_team_name("NEW YORK KNICKS") == "new york knicks"


# ── _team_in_question ──────────────────────────────────────────


class TestTeamInQuestion:
    def test_exact_match(self) -> None:
        assert _team_in_question("Lakers", "Will the Lakers win?")

    def test_partial_match(self) -> None:
        assert _team_in_question(
            "Los Angeles Lakers",
            "Will the los angeles lakers win the championship?"
        )

    def test_no_match(self) -> None:
        assert not _team_in_question("Warriors", "Will the Lakers win?")


# ── _find_token_for_outcome ────────────────────────────────────


class TestFindTokenForOutcome:
    def test_find_yes(self) -> None:
        tokens = _tokens()
        assert _find_token_for_outcome(tokens, "Yes") == "0xyes"

    def test_find_no(self) -> None:
        tokens = _tokens()
        assert _find_token_for_outcome(tokens, "No") == "0xno"

    def test_find_team_name(self) -> None:
        tokens = _team_tokens()
        assert _find_token_for_outcome(tokens, "Lakers") == "0xlakers"

    def test_not_found(self) -> None:
        tokens = _tokens()
        assert _find_token_for_outcome(tokens, "Maybe") is None


# ── MarketMatcher: Economic ────────────────────────────────────


class TestMatcherEconomic:
    def test_matches_cpi_above(self) -> None:
        """CPI=3.5 vs "above 3.0%" → Yes."""
        matcher = MarketMatcher()
        event = _economic_event(indicator="CPI", numeric_value=Decimal("3.5"))
        opps = {"c1": _opp(question="Will CPI be above 3.0%?")}

        results = matcher.match(event, opps)
        assert len(results) == 1
        assert results[0].target_outcome == "Yes"
        assert results[0].target_token_id == "0xyes"

    def test_matches_cpi_below_threshold(self) -> None:
        """CPI=2.5 vs "above 3.0%" → No."""
        matcher = MarketMatcher()
        event = _economic_event(indicator="CPI", numeric_value=Decimal("2.5"))
        opps = {"c1": _opp(question="Will CPI be above 3.0%?")}

        results = matcher.match(event, opps)
        assert len(results) == 1
        assert results[0].target_outcome == "No"
        assert results[0].target_token_id == "0xno"

    def test_wrong_indicator_no_match(self) -> None:
        """GDP event should not match CPI market."""
        matcher = MarketMatcher()
        event = _economic_event(indicator="GDP", numeric_value=Decimal("3.5"))
        opps = {"c1": _opp(question="Will CPI be above 3.0%?")}

        results = matcher.match(event, opps)
        assert len(results) == 0

    def test_multiple_markets(self) -> None:
        """Event matches multiple markets with the indicator."""
        matcher = MarketMatcher()
        event = _economic_event(indicator="CPI", numeric_value=Decimal("3.5"))
        opps = {
            "c1": _opp(condition_id="c1", question="Will CPI be above 3.0%?"),
            "c2": _opp(condition_id="c2", question="Will CPI exceed 4.0%?"),
        }

        results = matcher.match(event, opps)
        assert len(results) == 2
        # First market: 3.5 > 3.0 → Yes
        r1 = next(r for r in results if r.opportunity.condition_id == "c1")
        assert r1.target_outcome == "Yes"
        # Second market: 3.5 < 4.0 → No (does not exceed)
        r2 = next(r for r in results if r.opportunity.condition_id == "c2")
        assert r2.target_outcome == "No"

    def test_no_numeric_value_skipped(self) -> None:
        """Events without numeric_value are skipped."""
        matcher = MarketMatcher()
        event = _economic_event(indicator="CPI", numeric_value=None)
        opps = {"c1": _opp(question="Will CPI be above 3.0%?")}

        results = matcher.match(event, opps)
        assert len(results) == 0


# ── MarketMatcher: Sports ──────────────────────────────────────


class TestMatcherSports:
    def test_team_match_winner_token(self) -> None:
        """Winner's team name matches a token outcome directly."""
        matcher = MarketMatcher()
        event = _sports_event(winner="Lakers", home_team="Lakers", away_team="Celtics")
        opps = {
            "c1": _opp(
                question="Will the Lakers beat the Celtics?",
                category=MarketCategory.SPORTS,
                tokens=_team_tokens(),
            ),
        }

        results = matcher.match(event, opps)
        assert len(results) == 1
        assert results[0].target_token_id == "0xlakers"

    def test_no_match_wrong_teams(self) -> None:
        """Event teams don't appear in question → no match."""
        matcher = MarketMatcher()
        event = _sports_event(winner="Warriors", home_team="Warriors", away_team="Nets")
        opps = {
            "c1": _opp(
                question="Will the Lakers beat the Celtics?",
                category=MarketCategory.SPORTS,
                tokens=_team_tokens(),
            ),
        }

        results = matcher.match(event, opps)
        assert len(results) == 0

    def test_yes_no_fallback(self) -> None:
        """When winner matches question but no team token, falls back to Yes/No."""
        matcher = MarketMatcher()
        event = _sports_event(winner="Lakers", home_team="Lakers", away_team="Celtics")
        opps = {
            "c1": _opp(
                question="Will the Lakers win tonight?",
                category=MarketCategory.SPORTS,
                tokens=_tokens(),  # Yes/No tokens, not team tokens
            ),
        }

        results = matcher.match(event, opps)
        assert len(results) == 1
        assert results[0].target_outcome == "Yes"
        assert results[0].target_token_id == "0xyes"

    def test_no_winner_no_match(self) -> None:
        """Events without a winner produce no matches."""
        matcher = MarketMatcher()
        event = _sports_event(winner="")
        opps = {
            "c1": _opp(
                question="Will the Lakers win?",
                category=MarketCategory.SPORTS,
                tokens=_tokens(),
            ),
        }

        results = matcher.match(event, opps)
        assert len(results) == 0


# ── MarketMatcher: Crypto ──────────────────────────────────────


class TestMatcherCrypto:
    def test_btc_above_threshold(self) -> None:
        """BTC at 55000 vs 'above $50,000' → Yes."""
        matcher = MarketMatcher()
        event = _crypto_event(pair="BTC_USDT", price=Decimal("55000"))
        opps = {
            "c1": _opp(
                question="Will BTC go above $50,000?",
                category=MarketCategory.CRYPTO,
                tokens=_tokens(),
            ),
        }

        results = matcher.match(event, opps)
        assert len(results) == 1
        assert results[0].target_outcome == "Yes"

    def test_wrong_pair_no_match(self) -> None:
        """ETH event should not match BTC market."""
        matcher = MarketMatcher()
        event = _crypto_event(pair="ETH_USDT", price=Decimal("4000"))
        opps = {
            "c1": _opp(
                question="Will BTC go above $50,000?",
                category=MarketCategory.CRYPTO,
                tokens=_tokens(),
            ),
        }

        results = matcher.match(event, opps)
        assert len(results) == 0

    def test_below_threshold(self) -> None:
        """BTC at 45000 vs 'above $50,000' → No."""
        matcher = MarketMatcher()
        event = _crypto_event(pair="BTC_USDT", price=Decimal("45000"))
        opps = {
            "c1": _opp(
                question="Will BTC go above $50,000?",
                category=MarketCategory.CRYPTO,
                tokens=_tokens(),
            ),
        }

        results = matcher.match(event, opps)
        assert len(results) == 1
        assert results[0].target_outcome == "No"
