"""Sports data feed — polls ESPN scoreboard API for game completions."""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from src.core.config import SportsFeedConfig, get_settings
from src.core.types import (
    FeedEvent,
    FeedEventType,
    FeedType,
    GameResult,
    GameStatus,
    OutcomeType,
    SportLeague,
)
from src.feeds.base import BaseFeed
from src.feeds.exceptions import FeedConnectionError, FeedParseError

logger = structlog.stdlib.get_logger()

# ESPN status.type.name → GameStatus mapping
_ESPN_STATUS_MAP: dict[str, GameStatus] = {
    "STATUS_FINAL": GameStatus.FINAL,
    "STATUS_IN_PROGRESS": GameStatus.IN_PROGRESS,
    "STATUS_SCHEDULED": GameStatus.SCHEDULED,
    "STATUS_DELAYED": GameStatus.DELAYED,
    "STATUS_CANCELED": GameStatus.CANCELLED,
    "STATUS_CANCELLED": GameStatus.CANCELLED,
    "STATUS_POSTPONED": GameStatus.DELAYED,
}


def _league_to_espn_path(league: SportLeague) -> tuple[str, str]:
    """Map a SportLeague to its ESPN API (sport, league) path segments.

    Returns:
        Tuple of (sport, league_slug) for the URL path.
    """
    mapping: dict[SportLeague, tuple[str, str]] = {
        SportLeague.NFL: ("football", "nfl"),
        SportLeague.NBA: ("basketball", "nba"),
        SportLeague.MLB: ("baseball", "mlb"),
        SportLeague.NHL: ("hockey", "nhl"),
    }
    return mapping[league]


def _parse_espn_scoreboard(
    league: SportLeague,
    data: dict[str, Any],
) -> list[GameResult]:
    """Parse an ESPN scoreboard response into GameResult objects.

    Expected structure::

        {
            "events": [
                {
                    "id": "401547417",
                    "status": {"type": {"name": "STATUS_FINAL", "completed": true}},
                    "competitions": [{
                        "competitors": [
                            {"homeAway": "home", "team": {"displayName": "..."},
                             "score": "24"},
                            {"homeAway": "away", "team": {"displayName": "..."},
                             "score": "17"},
                        ]
                    }]
                }
            ]
        }
    """
    events = data.get("events")
    if not isinstance(events, list):
        return []

    results: list[GameResult] = []
    for event in events:
        if not isinstance(event, dict):
            continue

        game_id = str(event.get("id", ""))
        if not game_id:
            continue

        # Parse status
        status_obj = event.get("status")
        if not isinstance(status_obj, dict):
            continue
        status_type = status_obj.get("type")
        if not isinstance(status_type, dict):
            continue
        status_name = str(status_type.get("name", ""))
        game_status = _ESPN_STATUS_MAP.get(status_name, GameStatus.SCHEDULED)

        # Parse competitors
        competitions = event.get("competitions")
        if not isinstance(competitions, list) or not competitions:
            continue
        comp = competitions[0]
        if not isinstance(comp, dict):
            continue
        competitors = comp.get("competitors")
        if not isinstance(competitors, list):
            continue

        home_team = ""
        away_team = ""
        home_score = 0
        away_score = 0

        for competitor in competitors:
            if not isinstance(competitor, dict):
                continue
            home_away = str(competitor.get("homeAway", ""))
            team_obj = competitor.get("team")
            team_name = ""
            if isinstance(team_obj, dict):
                team_name = str(team_obj.get("displayName", ""))

            try:
                score = int(competitor.get("score", 0))
            except (ValueError, TypeError):
                score = 0

            if home_away == "home":
                home_team = team_name
                home_score = score
            elif home_away == "away":
                away_team = team_name
                away_score = score

        # Determine winner
        winner = ""
        if game_status == GameStatus.FINAL:
            if home_score > away_score:
                winner = home_team
            elif away_score > home_score:
                winner = away_team
            # Tie: winner stays ""

        results.append(GameResult(
            game_id=game_id,
            league=league,
            home_team=home_team,
            away_team=away_team,
            home_score=home_score,
            away_score=away_score,
            winner=winner,
            status=game_status,
            completed_at=time.time() if game_status == GameStatus.FINAL else 0.0,
            raw=dict(event),
        ))

    return results


def _detect_completed_games(
    current: list[GameResult],
    previous_states: dict[str, GameStatus],
) -> list[GameResult]:
    """Detect games that have transitioned to FINAL status.

    Returns only games whose status changed to FINAL compared to previous_states.
    Games already in FINAL in previous_states are excluded.
    Games newly seen as FINAL (no prior entry) are included.
    """
    completed: list[GameResult] = []
    for result in current:
        if result.status != GameStatus.FINAL:
            continue
        prev = previous_states.get(result.game_id)
        if prev is None or prev != GameStatus.FINAL:
            completed.append(result)
    return completed


class SportsFeed(BaseFeed):
    """Sports data feed that polls ESPN scoreboards for game completions.

    Usage::

        feed = SportsFeed()
        feed.on_event(my_callback)
        async with feed:
            await asyncio.sleep(60)
    """

    def __init__(self, config: SportsFeedConfig | None = None) -> None:
        cfg = config or get_settings().feeds.sports
        super().__init__(
            feed_type=FeedType.SPORTS,
            poll_interval_ms=cfg.poll_interval_ms,
        )
        self._config = cfg
        self._http: httpx.AsyncClient | None = None
        self._game_states: dict[str, GameStatus] = {}
        self._latest_results: dict[str, GameResult] = {}

    @property
    def latest_results(self) -> dict[str, GameResult]:
        """Most recent result per game ID."""
        return dict(self._latest_results)

    @property
    def connected(self) -> bool:
        """Whether the HTTP client is active."""
        return self._http is not None and not self._http.is_closed

    def get_result(self, game_id: str) -> GameResult | None:
        """Return the latest result for a given game ID."""
        return self._latest_results.get(game_id)

    async def connect(self) -> None:
        """Create the httpx async client."""
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def close(self) -> None:
        """Close the httpx async client."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def poll(self) -> list[FeedEvent]:
        """Fetch ESPN scoreboards, detect completed games, return FeedEvents."""
        if self._http is None:
            raise FeedConnectionError("HTTP client not connected")

        all_events: list[FeedEvent] = []

        for league_str in self._config.leagues:
            try:
                league = SportLeague(league_str)
            except ValueError:
                continue

            sport, league_slug = _league_to_espn_path(league)
            url = f"{self._config.base_url}/{sport}/{league_slug}/scoreboard"

            try:
                response = await self._http.get(url)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise FeedConnectionError(
                    f"ESPN API returned {exc.response.status_code} for {league_str}"
                ) from exc
            except httpx.HTTPError as exc:
                raise FeedConnectionError(
                    f"ESPN API request failed for {league_str}: {exc}"
                ) from exc

            try:
                body = response.json()
            except ValueError as exc:
                raise FeedParseError(
                    f"ESPN API returned invalid JSON for {league_str}"
                ) from exc

            if not isinstance(body, dict):
                raise FeedParseError(
                    f"ESPN API returned non-object for {league_str}"
                )

            results = _parse_espn_scoreboard(league, body)
            completed = _detect_completed_games(results, self._game_states)

            # Update state
            for result in results:
                self._game_states[result.game_id] = result.status
                self._latest_results[result.game_id] = result

            # Convert completed games to FeedEvents
            now = time.time()
            for result in completed:
                all_events.append(self._to_feed_event(result, now))

        if all_events:
            logger.info(
                "sports_games_completed",
                count=len(all_events),
                games=[e.metadata.get("game_id") for e in all_events],
            )

        return all_events

    @staticmethod
    def _to_feed_event(result: GameResult, received_at: float) -> FeedEvent:
        """Convert a GameResult to a generic FeedEvent."""
        return FeedEvent(
            feed_type=FeedType.SPORTS,
            event_type=FeedEventType.DATA_RELEASED,
            indicator=f"{result.league}_GAME_RESULT",
            value=result.winner,
            outcome_type=OutcomeType.CATEGORICAL,
            released_at=result.completed_at,
            received_at=received_at,
            metadata={
                "game_id": result.game_id,
                "league": result.league.value,
                "home_team": result.home_team,
                "away_team": result.away_team,
                "home_score": result.home_score,
                "away_score": result.away_score,
            },
            raw=result.raw,
        )
