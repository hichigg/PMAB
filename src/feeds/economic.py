"""Economic data feed — polls BLS API for CPI, NFP, unemployment, etc."""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation

import httpx
import structlog

from src.core.config import EconomicFeedConfig, get_settings
from src.core.types import (
    EconomicIndicator,
    EconomicRelease,
    FeedEvent,
    FeedEventType,
    FeedType,
    OutcomeType,
)
from src.feeds.base import BaseFeed
from src.feeds.exceptions import FeedConnectionError, FeedParseError

logger = structlog.stdlib.get_logger()

# BLS series ID → EconomicIndicator mapping
_BLS_SERIES_MAP: dict[str, EconomicIndicator] = {
    "CUSR0000SA0": EconomicIndicator.CPI,
    "CUSR0000SA0L1E": EconomicIndicator.CORE_CPI,
    "CES0000000001": EconomicIndicator.NFP,
    "LNS14000000": EconomicIndicator.UNEMPLOYMENT,
    "WPSFD4": EconomicIndicator.PPI,
}


def _indicator_from_series_id(series_id: str) -> EconomicIndicator | None:
    """Map a BLS series ID to an EconomicIndicator, or None if unknown."""
    return _BLS_SERIES_MAP.get(series_id)


def _parse_bls_series(series_data: dict[str, object]) -> list[EconomicRelease]:
    """Parse a single BLS series object into a list of EconomicRelease.

    Expected structure::

        {
            "seriesID": "CUSR0000SA0",
            "data": [
                {"year": "2024", "period": "M12", "value": "3.2", "latest": "true"},
                ...
            ]
        }
    """
    series_id = str(series_data.get("seriesID", ""))
    indicator = _indicator_from_series_id(series_id)
    if indicator is None:
        return []

    raw_data = series_data.get("data")
    if not isinstance(raw_data, list):
        return []

    releases: list[EconomicRelease] = []
    for entry in raw_data:
        if not isinstance(entry, dict):
            continue

        value_str = str(entry.get("value", ""))
        numeric_val: Decimal | None = None
        try:
            numeric_val = Decimal(value_str)
        except (InvalidOperation, ValueError):
            pass

        releases.append(EconomicRelease(
            indicator=indicator,
            value=value_str,
            numeric_value=numeric_val,
            released_at=0.0,
            source="bls",
            raw=dict(entry),
        ))

    return releases


def _detect_new_releases(
    current: list[EconomicRelease],
    previous: dict[EconomicIndicator, EconomicRelease],
) -> list[EconomicRelease]:
    """Detect releases in *current* that differ from *previous* (by value).

    Returns only releases whose value changed compared to the last-seen
    release for that indicator. If an indicator has no prior entry, the
    release is considered new.
    """
    new: list[EconomicRelease] = []
    for release in current:
        prior = previous.get(release.indicator)
        if prior is None or prior.value != release.value:
            new.append(release)
    return new


class EconomicFeed(BaseFeed):
    """Economic data feed that polls the BLS (or configured) API.

    Usage::

        feed = EconomicFeed()
        feed.on_event(my_callback)
        async with feed:
            await asyncio.sleep(60)
    """

    def __init__(self, config: EconomicFeedConfig | None = None) -> None:
        cfg = config or get_settings().feeds.economic
        super().__init__(
            feed_type=FeedType.ECONOMIC,
            poll_interval_ms=cfg.poll_interval_ms,
        )
        self._config = cfg
        self._http: httpx.AsyncClient | None = None
        self._latest: dict[EconomicIndicator, EconomicRelease] = {}

    @property
    def latest_releases(self) -> dict[EconomicIndicator, EconomicRelease]:
        """Most recent release per indicator."""
        return dict(self._latest)

    @property
    def connected(self) -> bool:
        """Whether the HTTP client is active."""
        return self._http is not None and not self._http.is_closed

    def get_latest(self, indicator: EconomicIndicator) -> EconomicRelease | None:
        """Return the most recent release for a given indicator."""
        return self._latest.get(indicator)

    async def connect(self) -> None:
        """Create the httpx async client."""
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def close(self) -> None:
        """Close the httpx async client."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def poll(self) -> list[FeedEvent]:
        """Fetch BLS data, detect new releases, return FeedEvents."""
        if self._http is None:
            raise FeedConnectionError("HTTP client not connected")

        # Build the series IDs to request
        series_ids = list(_BLS_SERIES_MAP.keys())
        payload: dict[str, object] = {"seriesid": series_ids, "latest": True}
        if self._config.api_key:
            payload["registrationkey"] = self._config.api_key

        try:
            response = await self._http.post(self._config.base_url, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise FeedConnectionError(
                f"BLS API returned {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise FeedConnectionError(f"BLS API request failed: {exc}") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise FeedParseError("BLS API returned invalid JSON") from exc

        # Parse all series
        all_releases = self._parse_response(body)

        # Detect new releases
        new_releases = _detect_new_releases(all_releases, self._latest)

        # Update latest cache
        for release in all_releases:
            self._latest[release.indicator] = release

        # Convert new releases to FeedEvents
        now = time.time()
        events: list[FeedEvent] = []
        for release in new_releases:
            events.append(self._to_feed_event(release, now))

        if events:
            logger.info(
                "economic_new_releases",
                count=len(events),
                indicators=[e.indicator for e in new_releases],
            )

        return events

    def _parse_response(self, body: dict[str, object]) -> list[EconomicRelease]:
        """Parse the full BLS API response body.

        Returns an empty list (instead of raising) when the response has an
        unexpected shape — e.g. rate-limit or auth-error responses from BLS.
        """
        status = str(body.get("status", ""))
        if status not in ("REQUEST_SUCCEEDED", ""):
            logger.warning(
                "bls_api_non_success",
                status=status,
                message=body.get("message", []),
            )
            return []

        results = body.get("Results")
        if not isinstance(results, dict):
            logger.warning("bls_response_missing_results", keys=list(body.keys()))
            return []

        series_list = results.get("series")
        if not isinstance(series_list, list):
            logger.warning("bls_response_missing_series", keys=list(results.keys()))
            return []

        releases: list[EconomicRelease] = []
        for series_data in series_list:
            if isinstance(series_data, dict):
                releases.extend(_parse_bls_series(series_data))

        return releases

    @staticmethod
    def _to_feed_event(release: EconomicRelease, received_at: float) -> FeedEvent:
        """Convert an EconomicRelease to a generic FeedEvent."""
        return FeedEvent(
            feed_type=FeedType.ECONOMIC,
            event_type=FeedEventType.DATA_RELEASED,
            indicator=release.indicator.value,
            value=release.value,
            numeric_value=release.numeric_value,
            outcome_type=OutcomeType.NUMERIC,
            released_at=release.released_at,
            received_at=received_at,
            metadata={"source": release.source, "prior": release.prior_value},
            raw=release.raw,
        )
