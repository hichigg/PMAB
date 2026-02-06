"""Tests for EconomicFeed — BLS parsing, new-release detection, poll cycle."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.core.config import EconomicFeedConfig
from src.core.types import (
    EconomicIndicator,
    EconomicRelease,
    FeedEventType,
    FeedType,
)
from src.feeds.economic import (
    EconomicFeed,
    _detect_new_releases,
    _indicator_from_series_id,
    _parse_bls_series,
)
from src.feeds.exceptions import FeedConnectionError, FeedParseError

# ── Helpers ─────────────────────────────────────────────────────


def _make_bls_response(
    series: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a minimal BLS API response body."""
    return {"Results": {"series": series or []}}


def _make_series(
    series_id: str = "CUSR0000SA0",
    data: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Build a single BLS series object."""
    if data is None:
        data = [{"year": "2024", "period": "M12", "value": "3.2", "latest": "true"}]
    return {
        "seriesID": series_id,
        "data": data,
    }


def _cfg(**overrides: object) -> EconomicFeedConfig:
    return EconomicFeedConfig(
        enabled=True,
        poll_interval_ms=50,
        base_url="https://test.bls.gov/api/",
        **overrides,  # type: ignore[arg-type]
    )


# ── _indicator_from_series_id ──────────────────────────────────


class TestIndicatorFromSeriesId:
    def test_known_cpi(self) -> None:
        assert _indicator_from_series_id("CUSR0000SA0") == EconomicIndicator.CPI

    def test_known_nfp(self) -> None:
        assert _indicator_from_series_id("CES0000000001") == EconomicIndicator.NFP

    def test_unknown_returns_none(self) -> None:
        assert _indicator_from_series_id("UNKNOWN123") is None


# ── _parse_bls_series ──────────────────────────────────────────


class TestParseBLSSeries:
    def test_valid_cpi_series(self) -> None:
        series = _make_series(
            series_id="CUSR0000SA0",
            data=[{"year": "2024", "period": "M12", "value": "3.2"}],
        )
        releases = _parse_bls_series(series)
        assert len(releases) == 1
        assert releases[0].indicator == EconomicIndicator.CPI
        assert releases[0].value == "3.2"
        assert releases[0].numeric_value == Decimal("3.2")

    def test_empty_data(self) -> None:
        series = _make_series(data=[])
        releases = _parse_bls_series(series)
        assert releases == []

    def test_unknown_series_id_returns_empty(self) -> None:
        series = _make_series(series_id="UNKNOWN")
        releases = _parse_bls_series(series)
        assert releases == []

    def test_non_numeric_value(self) -> None:
        series = _make_series(
            data=[{"year": "2024", "period": "M12", "value": "N/A"}],
        )
        releases = _parse_bls_series(series)
        assert len(releases) == 1
        assert releases[0].value == "N/A"
        assert releases[0].numeric_value is None

    def test_multiple_data_points(self) -> None:
        series = _make_series(
            data=[
                {"year": "2024", "period": "M12", "value": "3.2"},
                {"year": "2024", "period": "M11", "value": "3.1"},
            ],
        )
        releases = _parse_bls_series(series)
        assert len(releases) == 2


# ── _detect_new_releases ───────────────────────────────────────


class TestDetectNewReleases:
    def test_all_new_when_no_previous(self) -> None:
        releases = [
            EconomicRelease(indicator=EconomicIndicator.CPI, value="3.2"),
        ]
        new = _detect_new_releases(releases, {})
        assert len(new) == 1

    def test_no_change_returns_empty(self) -> None:
        release = EconomicRelease(indicator=EconomicIndicator.CPI, value="3.2")
        previous = {EconomicIndicator.CPI: release}
        new = _detect_new_releases([release], previous)
        assert new == []

    def test_value_change_detected(self) -> None:
        old = EconomicRelease(indicator=EconomicIndicator.CPI, value="3.1")
        current = EconomicRelease(indicator=EconomicIndicator.CPI, value="3.2")
        new = _detect_new_releases([current], {EconomicIndicator.CPI: old})
        assert len(new) == 1
        assert new[0].value == "3.2"

    def test_multiple_indicators(self) -> None:
        cpi = EconomicRelease(indicator=EconomicIndicator.CPI, value="3.2")
        nfp = EconomicRelease(indicator=EconomicIndicator.NFP, value="200")
        previous = {EconomicIndicator.CPI: cpi}
        # CPI unchanged, NFP is new
        new = _detect_new_releases([cpi, nfp], previous)
        assert len(new) == 1
        assert new[0].indicator == EconomicIndicator.NFP


# ── EconomicFeed ───────────────────────────────────────────────


def _mock_response(
    body: dict[str, object],
    status_code: int = 200,
) -> httpx.Response:
    """Build a mock httpx.Response."""
    return httpx.Response(
        status_code=status_code,
        json=body,
        request=httpx.Request("POST", "https://test.bls.gov/api/"),
    )


class TestEconomicFeedConnect:
    async def test_connect_creates_client(self) -> None:
        feed = EconomicFeed(config=_cfg())
        assert not feed.connected
        await feed.connect()
        assert feed.connected
        await feed.close()

    async def test_close_clears_client(self) -> None:
        feed = EconomicFeed(config=_cfg())
        await feed.connect()
        await feed.close()
        assert not feed.connected

    async def test_close_is_safe_when_not_connected(self) -> None:
        feed = EconomicFeed(config=_cfg())
        await feed.close()  # should not raise


class TestEconomicFeedPoll:
    async def test_poll_returns_events_for_new_data(self) -> None:
        body = _make_bls_response([_make_series()])
        feed = EconomicFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "post", new_callable=AsyncMock) as mock_post:  # type: ignore[union-attr]
                mock_post.return_value = _mock_response(body)
                events = await feed.poll()
            assert len(events) == 1
            assert events[0].event_type == FeedEventType.DATA_RELEASED
            assert events[0].indicator == "CPI"
        finally:
            await feed.close()

    async def test_poll_no_events_on_unchanged_data(self) -> None:
        body = _make_bls_response([_make_series()])
        feed = EconomicFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "post", new_callable=AsyncMock) as mock_post:  # type: ignore[union-attr]
                mock_post.return_value = _mock_response(body)
                await feed.poll()  # first poll — populates cache
                events = await feed.poll()  # second poll — same data
            assert events == []
        finally:
            await feed.close()

    async def test_poll_updates_latest_cache(self) -> None:
        body = _make_bls_response([_make_series()])
        feed = EconomicFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "post", new_callable=AsyncMock) as mock_post:  # type: ignore[union-attr]
                mock_post.return_value = _mock_response(body)
                await feed.poll()
            latest = feed.get_latest(EconomicIndicator.CPI)
            assert latest is not None
            assert latest.value == "3.2"
        finally:
            await feed.close()

    async def test_poll_raises_on_connection_error(self) -> None:
        feed = EconomicFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "post", new_callable=AsyncMock) as mock_post:  # type: ignore[union-attr]
                mock_post.side_effect = httpx.ConnectError("connection refused")
                with pytest.raises(FeedConnectionError):
                    await feed.poll()
        finally:
            await feed.close()

    async def test_poll_raises_on_http_error(self) -> None:
        feed = EconomicFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "post", new_callable=AsyncMock) as mock_post:  # type: ignore[union-attr]
                mock_post.return_value = _mock_response({}, status_code=429)
                with pytest.raises(FeedConnectionError, match="429"):
                    await feed.poll()
        finally:
            await feed.close()

    async def test_poll_raises_on_parse_error(self) -> None:
        feed = EconomicFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "post", new_callable=AsyncMock) as mock_post:  # type: ignore[union-attr]
                mock_post.return_value = _mock_response({"bad": "data"})
                with pytest.raises(FeedParseError):
                    await feed.poll()
        finally:
            await feed.close()

    async def test_poll_not_connected_raises(self) -> None:
        feed = EconomicFeed(config=_cfg())
        with pytest.raises(FeedConnectionError, match="not connected"):
            await feed.poll()


class TestEconomicFeedGetLatest:
    async def test_get_latest_returns_none_before_poll(self) -> None:
        feed = EconomicFeed(config=_cfg())
        assert feed.get_latest(EconomicIndicator.CPI) is None

    async def test_latest_releases_dict(self) -> None:
        body = _make_bls_response([_make_series()])
        feed = EconomicFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "post", new_callable=AsyncMock) as mock_post:  # type: ignore[union-attr]
                mock_post.return_value = _mock_response(body)
                await feed.poll()
            releases = feed.latest_releases
            assert EconomicIndicator.CPI in releases
        finally:
            await feed.close()


class TestEconomicFeedEventContent:
    async def test_feed_event_has_correct_fields(self) -> None:
        body = _make_bls_response([_make_series()])
        feed = EconomicFeed(config=_cfg())
        await feed.connect()
        try:
            with patch.object(feed._http, "post", new_callable=AsyncMock) as mock_post:  # type: ignore[union-attr]
                mock_post.return_value = _mock_response(body)
                events = await feed.poll()
            assert len(events) == 1
            evt = events[0]
            assert evt.feed_type == FeedType.ECONOMIC
            assert evt.event_type == FeedEventType.DATA_RELEASED
            assert evt.value == "3.2"
            assert evt.numeric_value == Decimal("3.2")
            assert evt.received_at > 0
        finally:
            await feed.close()
