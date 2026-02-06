"""Tests for SportsFeed — ESPN parsing, completion detection, poll cycle."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.core.config import SportsFeedConfig
from src.core.types import (
    FeedEventType,
    FeedType,
    GameStatus,
    OutcomeType,
    SportLeague,
)
from src.feeds.exceptions import FeedConnectionError, FeedParseError
from src.feeds.sports import (
    SportsFeed,
    _detect_completed_games,
    _league_to_espn_path,
    _parse_espn_scoreboard,
)

# ── Helpers ─────────────────────────────────────────────────────


def _make_competitor(
    home_away: str, team_name: str, score: str = "0"
) -> dict[str, Any]:
    return {
        "homeAway": home_away,
        "team": {"displayName": team_name},
        "score": score,
    }


def _make_event(
    game_id: str = "401",
    status_name: str = "STATUS_FINAL",
    home_team: str = "Team A",
    away_team: str = "Team B",
    home_score: str = "24",
    away_score: str = "17",
) -> dict[str, Any]:
    return {
        "id": game_id,
        "status": {"type": {"name": status_name, "completed": status_name == "STATUS_FINAL"}},
        "competitions": [{
            "competitors": [
                _make_competitor("home", home_team, home_score),
                _make_competitor("away", away_team, away_score),
            ]
        }],
    }


def _make_scoreboard(events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if events is None:
        events = [_make_event()]
    return {"events": events}


def _cfg(**overrides: object) -> SportsFeedConfig:
    return SportsFeedConfig(
        enabled=True,
        poll_interval_ms=50,
        leagues=["NFL"],
        base_url="https://test.espn.com/sports",
        **overrides,  # type: ignore[arg-type]
    )


def _mock_response(
    body: dict[str, Any],
    status_code: int = 200,
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=body,
        request=httpx.Request("GET", "https://test.espn.com/sports/football/nfl/scoreboard"),
    )


# ── _league_to_espn_path ──────────────────────────────────────


class TestLeagueToEspnPath:
    def test_nfl(self) -> None:
        assert _league_to_espn_path(SportLeague.NFL) == ("football", "nfl")

    def test_nba(self) -> None:
        assert _league_to_espn_path(SportLeague.NBA) == ("basketball", "nba")

    def test_mlb(self) -> None:
        assert _league_to_espn_path(SportLeague.MLB) == ("baseball", "mlb")

    def test_nhl(self) -> None:
        assert _league_to_espn_path(SportLeague.NHL) == ("hockey", "nhl")


# ── _parse_espn_scoreboard ────────────────────────────────────


class TestParseEspnScoreboard:
    def test_valid_final_game(self) -> None:
        data = _make_scoreboard([_make_event()])
        results = _parse_espn_scoreboard(SportLeague.NFL, data)
        assert len(results) == 1
        assert results[0].game_id == "401"
        assert results[0].status == GameStatus.FINAL
        assert results[0].home_team == "Team A"
        assert results[0].away_team == "Team B"
        assert results[0].home_score == 24
        assert results[0].away_score == 17
        assert results[0].winner == "Team A"

    def test_empty_events(self) -> None:
        data = _make_scoreboard([])
        results = _parse_espn_scoreboard(SportLeague.NFL, data)
        assert results == []

    def test_missing_events_key(self) -> None:
        results = _parse_espn_scoreboard(SportLeague.NFL, {"other": "data"})
        assert results == []

    def test_multi_game_scoreboard(self) -> None:
        data = _make_scoreboard([
            _make_event(game_id="401", home_score="24", away_score="17"),
            _make_event(game_id="402", status_name="STATUS_IN_PROGRESS",
                        home_score="10", away_score="14"),
        ])
        results = _parse_espn_scoreboard(SportLeague.NBA, data)
        assert len(results) == 2
        assert results[0].winner == "Team A"
        assert results[1].status == GameStatus.IN_PROGRESS
        assert results[1].winner == ""

    def test_malformed_event_skipped(self) -> None:
        data = _make_scoreboard([
            _make_event(),
            {"id": "bad", "status": "not_a_dict"},  # malformed
        ])
        results = _parse_espn_scoreboard(SportLeague.NFL, data)
        assert len(results) == 1

    def test_tie_game_no_winner(self) -> None:
        data = _make_scoreboard([
            _make_event(home_score="21", away_score="21"),
        ])
        results = _parse_espn_scoreboard(SportLeague.NFL, data)
        assert len(results) == 1
        assert results[0].winner == ""


# ── _detect_completed_games ───────────────────────────────────


class TestDetectCompletedGames:
    def test_new_game_final(self) -> None:
        from src.core.types import GameResult
        result = GameResult(
            game_id="401", league=SportLeague.NFL, status=GameStatus.FINAL, winner="X"
        )
        completed = _detect_completed_games([result], {})
        assert len(completed) == 1

    def test_no_change_returns_empty(self) -> None:
        from src.core.types import GameResult
        result = GameResult(
            game_id="401", league=SportLeague.NFL, status=GameStatus.FINAL, winner="X"
        )
        completed = _detect_completed_games(
            [result], {"401": GameStatus.FINAL}
        )
        assert completed == []

    def test_transition_to_final(self) -> None:
        from src.core.types import GameResult
        result = GameResult(
            game_id="401", league=SportLeague.NFL, status=GameStatus.FINAL, winner="X"
        )
        completed = _detect_completed_games(
            [result], {"401": GameStatus.IN_PROGRESS}
        )
        assert len(completed) == 1

    def test_in_progress_not_included(self) -> None:
        from src.core.types import GameResult
        result = GameResult(
            game_id="401", league=SportLeague.NFL, status=GameStatus.IN_PROGRESS
        )
        completed = _detect_completed_games([result], {})
        assert completed == []


# ── SportsFeed connect/close ──────────────────────────────────


class TestSportsFeedConnect:
    async def test_connect_creates_client(self) -> None:
        feed = SportsFeed(config=_cfg())
        assert not feed.connected
        await feed.connect()
        assert feed.connected
        await feed.close()

    async def test_close_clears_client(self) -> None:
        feed = SportsFeed(config=_cfg())
        await feed.connect()
        await feed.close()
        assert not feed.connected

    async def test_close_is_safe_when_not_connected(self) -> None:
        feed = SportsFeed(config=_cfg())
        await feed.close()  # should not raise


# ── SportsFeed poll ───────────────────────────────────────────


class TestSportsFeedPoll:
    async def test_poll_returns_events_for_completed_game(self) -> None:
        body = _make_scoreboard([_make_event()])
        feed = SportsFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "get", new_callable=AsyncMock) as mock_get:  # type: ignore[union-attr]
                mock_get.return_value = _mock_response(body)
                events = await feed.poll()
            assert len(events) == 1
            assert events[0].event_type == FeedEventType.DATA_RELEASED
            assert events[0].indicator == "NFL_GAME_RESULT"
        finally:
            await feed.close()

    async def test_poll_no_events_when_unchanged(self) -> None:
        body = _make_scoreboard([_make_event()])
        feed = SportsFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "get", new_callable=AsyncMock) as mock_get:  # type: ignore[union-attr]
                mock_get.return_value = _mock_response(body)
                await feed.poll()  # first poll — records state
                events = await feed.poll()  # second poll — same data
            assert events == []
        finally:
            await feed.close()

    async def test_poll_raises_on_http_error(self) -> None:
        feed = SportsFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "get", new_callable=AsyncMock) as mock_get:  # type: ignore[union-attr]
                mock_get.return_value = _mock_response({}, status_code=500)
                with pytest.raises(FeedConnectionError, match="500"):
                    await feed.poll()
        finally:
            await feed.close()

    async def test_poll_raises_on_parse_error(self) -> None:
        feed = SportsFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "get", new_callable=AsyncMock) as mock_get:  # type: ignore[union-attr]
                # Return a non-dict body after json() call
                resp = httpx.Response(
                    status_code=200,
                    content=b'"just a string"',
                    request=httpx.Request("GET", "https://test.espn.com/x"),
                )
                mock_get.return_value = resp
                with pytest.raises(FeedParseError, match="non-object"):
                    await feed.poll()
        finally:
            await feed.close()

    async def test_poll_not_connected_raises(self) -> None:
        feed = SportsFeed(config=_cfg())
        with pytest.raises(FeedConnectionError, match="not connected"):
            await feed.poll()


# ── SportsFeed event content ──────────────────────────────────


class TestSportsFeedEventContent:
    async def test_event_has_correct_fields(self) -> None:
        body = _make_scoreboard([_make_event()])
        feed = SportsFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "get", new_callable=AsyncMock) as mock_get:  # type: ignore[union-attr]
                mock_get.return_value = _mock_response(body)
                events = await feed.poll()
            evt = events[0]
            assert evt.feed_type == FeedType.SPORTS
            assert evt.outcome_type == OutcomeType.CATEGORICAL
            assert evt.value == "Team A"
            assert evt.received_at > 0
        finally:
            await feed.close()

    async def test_event_metadata(self) -> None:
        body = _make_scoreboard([_make_event()])
        feed = SportsFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "get", new_callable=AsyncMock) as mock_get:  # type: ignore[union-attr]
                mock_get.return_value = _mock_response(body)
                events = await feed.poll()
            meta = events[0].metadata
            assert meta["game_id"] == "401"
            assert meta["league"] == "NFL"
            assert meta["home_team"] == "Team A"
            assert meta["away_team"] == "Team B"
            assert meta["home_score"] == 24
            assert meta["away_score"] == 17
        finally:
            await feed.close()


# ── SportsFeed get_result / latest_results ────────────────────


class TestSportsFeedResults:
    async def test_get_result_after_poll(self) -> None:
        body = _make_scoreboard([_make_event()])
        feed = SportsFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "get", new_callable=AsyncMock) as mock_get:  # type: ignore[union-attr]
                mock_get.return_value = _mock_response(body)
                await feed.poll()
            result = feed.get_result("401")
            assert result is not None
            assert result.winner == "Team A"
        finally:
            await feed.close()

    async def test_latest_results_dict(self) -> None:
        body = _make_scoreboard([_make_event()])
        feed = SportsFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "get", new_callable=AsyncMock) as mock_get:  # type: ignore[union-attr]
                mock_get.return_value = _mock_response(body)
                await feed.poll()
            results = feed.latest_results
            assert "401" in results
        finally:
            await feed.close()

    def test_get_result_returns_none_before_poll(self) -> None:
        feed = SportsFeed(config=_cfg())
        assert feed.get_result("999") is None
