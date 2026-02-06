"""Tests for PreSignedOrderPool."""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.core.config import reset_settings
from src.core.types import OrderRequest, Side
from src.polymarket.market_params import MarketParams, MarketParamsCache
from src.polymarket.order_pool import PreSignedOrderPool, _make_key
from src.polymarket.presigner import (
    STALENESS_THRESHOLD_SECS,
    OrderPreSigner,
    PreSignedOrder,
)


@pytest.fixture(autouse=True)
def _clean_settings() -> None:
    reset_settings()


def _make_presigned(
    token_id: str = "tok1",
    side: Side = Side.BUY,
    price: Decimal = Decimal("0.60"),
    expiration_ts: int = 0,
) -> PreSignedOrder:
    """Helper to create a PreSignedOrder for testing."""
    return PreSignedOrder(
        signed_order=MagicMock(),
        request=OrderRequest(
            token_id=token_id,
            side=side,
            price=price,
            size=Decimal("100"),
        ),
        market_params=MarketParams(
            token_id=token_id,
            tick_size="0.01",
            neg_risk=False,
            fee_rate_bps=20,
            fetched_at=time.monotonic(),
        ),
        expiration_ts=expiration_ts,
    )


@pytest.fixture()
def pool() -> PreSignedOrderPool:
    """Create a pool with mocked dependencies."""
    presigner = MagicMock(spec=OrderPreSigner)
    params_cache = MagicMock(spec=MarketParamsCache)
    sdk = MagicMock()
    return PreSignedOrderPool(
        presigner=presigner,
        params_cache=params_cache,
        sdk=sdk,
    )


class TestMakeKey:
    """Test the pool key generation."""

    def test_key_tuple(self) -> None:
        key = _make_key("tok1", Side.BUY, Decimal("0.60"))
        assert key == ("tok1", "BUY", "0.60")

    def test_different_prices_different_keys(self) -> None:
        k1 = _make_key("tok1", Side.BUY, Decimal("0.60"))
        k2 = _make_key("tok1", Side.BUY, Decimal("0.65"))
        assert k1 != k2

    def test_different_sides_different_keys(self) -> None:
        k1 = _make_key("tok1", Side.BUY, Decimal("0.60"))
        k2 = _make_key("tok1", Side.SELL, Decimal("0.60"))
        assert k1 != k2


class TestPreSignedOrderPool:
    """Test pool CRUD operations."""

    def test_add_and_get(self, pool: PreSignedOrderPool) -> None:
        order = _make_presigned()
        pool.add(order)
        result = pool.get("tok1", Side.BUY, Decimal("0.60"))
        assert result is not None
        assert result.request.token_id == "tok1"

    def test_get_returns_none_for_missing(
        self, pool: PreSignedOrderPool
    ) -> None:
        result = pool.get("tok1", Side.BUY, Decimal("0.60"))
        assert result is None

    def test_get_returns_none_for_expired(
        self, pool: PreSignedOrderPool
    ) -> None:
        order = _make_presigned(expiration_ts=int(time.time()) - 10)
        pool.add(order)
        result = pool.get("tok1", Side.BUY, Decimal("0.60"))
        assert result is None
        # Should also remove from pool
        assert pool.size == 0

    def test_get_returns_none_for_stale(
        self, pool: PreSignedOrderPool
    ) -> None:
        order = _make_presigned(
            expiration_ts=int(time.time()) + STALENESS_THRESHOLD_SECS - 5
        )
        pool.add(order)
        result = pool.get("tok1", Side.BUY, Decimal("0.60"))
        assert result is None

    def test_pop_removes_from_pool(self, pool: PreSignedOrderPool) -> None:
        order = _make_presigned()
        pool.add(order)
        result = pool.pop("tok1", Side.BUY, Decimal("0.60"))
        assert result is not None
        # Should be removed
        assert pool.size == 0
        assert pool.pop("tok1", Side.BUY, Decimal("0.60")) is None

    def test_pop_returns_none_for_expired(
        self, pool: PreSignedOrderPool
    ) -> None:
        order = _make_presigned(expiration_ts=int(time.time()) - 10)
        pool.add(order)
        result = pool.pop("tok1", Side.BUY, Decimal("0.60"))
        assert result is None

    def test_get_best_buy_returns_highest_price(
        self, pool: PreSignedOrderPool
    ) -> None:
        pool.add(_make_presigned(price=Decimal("0.55")))
        pool.add(_make_presigned(price=Decimal("0.60")))
        pool.add(_make_presigned(price=Decimal("0.50")))
        best = pool.get_best("tok1", Side.BUY)
        assert best is not None
        assert best.request.price == Decimal("0.60")

    def test_get_best_sell_returns_lowest_price(
        self, pool: PreSignedOrderPool
    ) -> None:
        pool.add(
            _make_presigned(side=Side.SELL, price=Decimal("0.70"))
        )
        pool.add(
            _make_presigned(side=Side.SELL, price=Decimal("0.65"))
        )
        pool.add(
            _make_presigned(side=Side.SELL, price=Decimal("0.75"))
        )
        best = pool.get_best("tok1", Side.SELL)
        assert best is not None
        assert best.request.price == Decimal("0.65")

    def test_get_best_returns_none_when_empty(
        self, pool: PreSignedOrderPool
    ) -> None:
        assert pool.get_best("tok1", Side.BUY) is None

    def test_get_best_skips_expired(
        self, pool: PreSignedOrderPool
    ) -> None:
        pool.add(
            _make_presigned(
                price=Decimal("0.60"),
                expiration_ts=int(time.time()) - 10,
            )
        )
        pool.add(_make_presigned(price=Decimal("0.55")))
        best = pool.get_best("tok1", Side.BUY)
        assert best is not None
        assert best.request.price == Decimal("0.55")

    def test_remove(self, pool: PreSignedOrderPool) -> None:
        pool.add(_make_presigned())
        assert pool.remove("tok1", Side.BUY, Decimal("0.60")) is True
        assert pool.size == 0

    def test_remove_missing_returns_false(
        self, pool: PreSignedOrderPool
    ) -> None:
        assert pool.remove("tok1", Side.BUY, Decimal("0.60")) is False

    def test_clear_token(self, pool: PreSignedOrderPool) -> None:
        pool.add(_make_presigned(price=Decimal("0.55")))
        pool.add(_make_presigned(price=Decimal("0.60")))
        pool.add(
            _make_presigned(token_id="tok2", price=Decimal("0.70"))
        )
        removed = pool.clear_token("tok1")
        assert removed == 2
        assert pool.size == 1

    def test_clear(self, pool: PreSignedOrderPool) -> None:
        pool.add(_make_presigned(price=Decimal("0.55")))
        pool.add(_make_presigned(price=Decimal("0.60")))
        pool.clear()
        assert pool.size == 0

    def test_clear_expired(self, pool: PreSignedOrderPool) -> None:
        pool.add(_make_presigned(price=Decimal("0.55")))  # no expiry
        pool.add(
            _make_presigned(
                price=Decimal("0.60"),
                expiration_ts=int(time.time()) - 10,
            )
        )
        removed = pool.clear_expired()
        assert removed == 1
        assert pool.size == 1

    def test_keys(self, pool: PreSignedOrderPool) -> None:
        pool.add(_make_presigned(price=Decimal("0.55")))
        pool.add(_make_presigned(price=Decimal("0.60")))
        keys = pool.keys()
        assert len(keys) == 2


class TestPoolRefreshLoop:
    """Test the background refresh loop."""

    async def test_start_and_stop(self, pool: PreSignedOrderPool) -> None:
        await pool.start_refresh_loop()
        assert pool._running is True
        assert pool._refresh_task is not None
        await pool.stop_refresh_loop()
        assert pool._running is False
        assert pool._refresh_task is None

    async def test_double_start_is_idempotent(
        self, pool: PreSignedOrderPool
    ) -> None:
        await pool.start_refresh_loop()
        task1 = pool._refresh_task
        await pool.start_refresh_loop()
        task2 = pool._refresh_task
        assert task1 is task2
        await pool.stop_refresh_loop()
