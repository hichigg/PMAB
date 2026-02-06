"""Tests for MarketParamsCache."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from src.core.config import reset_settings
from src.polymarket.market_params import MarketParams, MarketParamsCache


@pytest.fixture(autouse=True)
def _clean_settings() -> None:
    reset_settings()


@pytest.fixture()
def mock_sdk() -> MagicMock:
    sdk = MagicMock()
    sdk.get_tick_size.return_value = "0.01"
    sdk.get_neg_risk.return_value = False
    sdk.get_fee_rate_bps.return_value = 20
    return sdk


class TestMarketParams:
    """Test MarketParams model."""

    def test_is_stale_within_ttl(self) -> None:
        params = MarketParams(
            token_id="tok1",
            tick_size="0.01",
            neg_risk=False,
            fee_rate_bps=20,
            fetched_at=time.monotonic(),
        )
        assert params.is_stale(300.0) is False

    def test_is_stale_past_ttl(self) -> None:
        params = MarketParams(
            token_id="tok1",
            tick_size="0.01",
            neg_risk=False,
            fee_rate_bps=20,
            fetched_at=time.monotonic() - 400,
        )
        assert params.is_stale(300.0) is True


class TestMarketParamsCache:
    """Test MarketParamsCache async behavior."""

    async def test_cache_miss_fetches_from_sdk(self, mock_sdk: MagicMock) -> None:
        cache = MarketParamsCache(ttl_secs=300.0)
        params = await cache.get("tok1", mock_sdk)

        assert params.token_id == "tok1"
        assert params.tick_size == "0.01"
        assert params.neg_risk is False
        assert params.fee_rate_bps == 20
        mock_sdk.get_tick_size.assert_called_once_with("tok1")
        mock_sdk.get_neg_risk.assert_called_once_with("tok1")
        mock_sdk.get_fee_rate_bps.assert_called_once_with("tok1")

    async def test_cache_hit_returns_cached(self, mock_sdk: MagicMock) -> None:
        cache = MarketParamsCache(ttl_secs=300.0)
        params1 = await cache.get("tok1", mock_sdk)
        params2 = await cache.get("tok1", mock_sdk)

        assert params1.token_id == params2.token_id
        # SDK methods should only be called once
        assert mock_sdk.get_tick_size.call_count == 1

    async def test_stale_entry_refetches(self, mock_sdk: MagicMock) -> None:
        cache = MarketParamsCache(ttl_secs=0.0)  # Immediate staleness
        await cache.get("tok1", mock_sdk)

        # Second call should re-fetch because TTL is 0
        mock_sdk.get_tick_size.return_value = "0.001"
        params = await cache.get("tok1", mock_sdk)

        assert params.tick_size == "0.001"
        assert mock_sdk.get_tick_size.call_count == 2

    async def test_force_refresh_bypasses_cache(
        self, mock_sdk: MagicMock
    ) -> None:
        cache = MarketParamsCache(ttl_secs=300.0)
        await cache.get("tok1", mock_sdk)

        mock_sdk.get_tick_size.return_value = "0.001"
        params = await cache.get("tok1", mock_sdk, force_refresh=True)

        assert params.tick_size == "0.001"
        assert mock_sdk.get_tick_size.call_count == 2

    async def test_warm_fetches_multiple_tokens(
        self, mock_sdk: MagicMock
    ) -> None:
        cache = MarketParamsCache(ttl_secs=300.0)
        result = await cache.warm(["tok1", "tok2", "tok3"], mock_sdk)

        assert len(result) == 3
        assert "tok1" in result
        assert "tok2" in result
        assert "tok3" in result
        assert mock_sdk.get_tick_size.call_count == 3

    async def test_concurrent_requests_only_fetch_once(
        self, mock_sdk: MagicMock
    ) -> None:
        """Two concurrent .get() calls for same token should only fetch once."""
        cache = MarketParamsCache(ttl_secs=300.0)

        results = await asyncio.gather(
            cache.get("tok1", mock_sdk),
            cache.get("tok1", mock_sdk),
        )

        assert results[0].token_id == results[1].token_id
        # Per-token lock should prevent duplicate fetches
        assert mock_sdk.get_tick_size.call_count == 1

    def test_invalidate_removes_entry(self) -> None:
        cache = MarketParamsCache()
        cache._cache["tok1"] = MarketParams(
            token_id="tok1",
            tick_size="0.01",
            neg_risk=False,
            fee_rate_bps=20,
            fetched_at=time.monotonic(),
        )
        assert "tok1" in cache
        cache.invalidate("tok1")
        assert "tok1" not in cache

    def test_clear_empties_cache(self) -> None:
        cache = MarketParamsCache()
        cache._cache["tok1"] = MarketParams(
            token_id="tok1",
            tick_size="0.01",
            neg_risk=False,
            fee_rate_bps=20,
            fetched_at=time.monotonic(),
        )
        assert len(cache) == 1
        cache.clear()
        assert len(cache) == 0
