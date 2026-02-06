"""Tests for OrderPreSigner and PreSignedOrder."""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.core.config import reset_settings
from src.core.types import OrderRequest, Side
from src.polymarket.market_params import MarketParams
from src.polymarket.presigner import (
    STALENESS_THRESHOLD_SECS,
    OrderPreSigner,
    PreSignedOrder,
)


@pytest.fixture(autouse=True)
def _clean_settings() -> None:
    reset_settings()


@pytest.fixture()
def sample_params() -> MarketParams:
    return MarketParams(
        token_id="tok1",
        tick_size="0.01",
        neg_risk=False,
        fee_rate_bps=20,
        fetched_at=time.monotonic(),
    )


@pytest.fixture()
def sample_request() -> OrderRequest:
    return OrderRequest(
        token_id="tok1",
        side=Side.BUY,
        price=Decimal("0.60"),
        size=Decimal("100"),
    )


@pytest.fixture()
def mock_sdk() -> MagicMock:
    sdk = MagicMock()
    sdk.builder = MagicMock()
    mock_signed = MagicMock()
    mock_signed.order = MagicMock()
    mock_signed.signature = "0xdeadbeef"
    sdk.builder.create_order.return_value = mock_signed
    return sdk


class TestPreSignedOrder:
    """Test PreSignedOrder lifecycle properties."""

    def _make_presigned(
        self, expiration_ts: int = 0
    ) -> PreSignedOrder:
        return PreSignedOrder(
            signed_order=MagicMock(),
            request=OrderRequest(
                token_id="tok1",
                side=Side.BUY,
                price=Decimal("0.60"),
                size=Decimal("100"),
            ),
            market_params=MarketParams(
                token_id="tok1",
                tick_size="0.01",
                neg_risk=False,
                fee_rate_bps=20,
                fetched_at=time.monotonic(),
            ),
            expiration_ts=expiration_ts,
        )

    def test_not_expired_when_no_expiration(self) -> None:
        order = self._make_presigned(expiration_ts=0)
        assert order.is_expired is False

    def test_not_expired_when_future(self) -> None:
        order = self._make_presigned(
            expiration_ts=int(time.time()) + 3600
        )
        assert order.is_expired is False

    def test_expired_when_past(self) -> None:
        order = self._make_presigned(
            expiration_ts=int(time.time()) - 10
        )
        assert order.is_expired is True

    def test_not_stale_when_no_expiration(self) -> None:
        order = self._make_presigned(expiration_ts=0)
        assert order.is_stale is False

    def test_stale_when_close_to_expiry(self) -> None:
        order = self._make_presigned(
            expiration_ts=int(time.time()) + STALENESS_THRESHOLD_SECS - 5
        )
        assert order.is_stale is True

    def test_not_stale_when_far_from_expiry(self) -> None:
        order = self._make_presigned(
            expiration_ts=int(time.time()) + 3600
        )
        assert order.is_stale is False

    def test_time_until_expiry_none_when_no_expiration(self) -> None:
        order = self._make_presigned(expiration_ts=0)
        assert order.time_until_expiry is None

    def test_time_until_expiry_positive(self) -> None:
        order = self._make_presigned(
            expiration_ts=int(time.time()) + 100
        )
        remaining = order.time_until_expiry
        assert remaining is not None
        assert 99 <= remaining <= 101

    def test_age_secs(self) -> None:
        order = self._make_presigned()
        assert order.age_secs >= 0
        assert order.age_secs < 1.0  # Just created


class TestOrderPreSigner:
    """Test OrderPreSigner signing behavior."""

    async def test_presign_produces_signed_order(
        self,
        mock_sdk: MagicMock,
        sample_request: OrderRequest,
        sample_params: MarketParams,
    ) -> None:
        presigner = OrderPreSigner(mock_sdk)
        result = await presigner.presign(sample_request, sample_params)

        assert isinstance(result, PreSignedOrder)
        assert result.request.token_id == "tok1"
        assert result.request.side == Side.BUY
        assert result.signed_order is not None
        mock_sdk.builder.create_order.assert_called_once()

    async def test_presign_uses_cached_params(
        self,
        mock_sdk: MagicMock,
        sample_request: OrderRequest,
        sample_params: MarketParams,
    ) -> None:
        presigner = OrderPreSigner(mock_sdk)
        await presigner.presign(sample_request, sample_params)

        call_args = mock_sdk.builder.create_order.call_args
        options = call_args[0][1]
        assert options.tick_size == "0.01"
        assert options.neg_risk is False

    async def test_presign_sets_expiration(
        self,
        mock_sdk: MagicMock,
        sample_request: OrderRequest,
        sample_params: MarketParams,
    ) -> None:
        presigner = OrderPreSigner(mock_sdk, default_expiration_secs=600)
        result = await presigner.presign(sample_request, sample_params)

        assert result.expiration_ts > 0
        expected_min = int(time.time()) + 599
        expected_max = int(time.time()) + 601
        assert expected_min <= result.expiration_ts <= expected_max

    async def test_presign_zero_expiration(
        self,
        mock_sdk: MagicMock,
        sample_request: OrderRequest,
        sample_params: MarketParams,
    ) -> None:
        presigner = OrderPreSigner(mock_sdk)
        result = await presigner.presign(
            sample_request, sample_params, expiration_secs=0
        )
        assert result.expiration_ts == 0

    async def test_presign_batch(
        self,
        mock_sdk: MagicMock,
        sample_params: MarketParams,
    ) -> None:
        presigner = OrderPreSigner(mock_sdk)
        requests = [
            OrderRequest(
                token_id="tok1",
                side=Side.BUY,
                price=Decimal("0.60"),
                size=Decimal("100"),
            ),
            OrderRequest(
                token_id="tok1",
                side=Side.SELL,
                price=Decimal("0.65"),
                size=Decimal("50"),
            ),
        ]
        results = await presigner.presign_batch(
            requests, {"tok1": sample_params}
        )
        assert len(results) == 2
        assert results[0].request.side == Side.BUY
        assert results[1].request.side == Side.SELL

    async def test_presign_price_ladder(
        self,
        mock_sdk: MagicMock,
        sample_params: MarketParams,
    ) -> None:
        presigner = OrderPreSigner(mock_sdk)
        prices = [Decimal("0.55"), Decimal("0.60"), Decimal("0.65")]
        results = await presigner.presign_price_ladder(
            token_id="tok1",
            side=Side.BUY,
            prices=prices,
            size=Decimal("100"),
            params=sample_params,
        )
        assert len(results) == 3
        result_prices = [r.request.price for r in results]
        assert Decimal("0.55") in result_prices
        assert Decimal("0.60") in result_prices
        assert Decimal("0.65") in result_prices

    async def test_presign_respects_request_expiration(
        self,
        mock_sdk: MagicMock,
        sample_params: MarketParams,
    ) -> None:
        """If OrderRequest has explicit expiration, use it instead of default."""
        presigner = OrderPreSigner(mock_sdk)
        req = OrderRequest(
            token_id="tok1",
            side=Side.BUY,
            price=Decimal("0.60"),
            size=Decimal("100"),
            expiration=9999999999,
        )
        await presigner.presign(req, sample_params)

        call_args = mock_sdk.builder.create_order.call_args
        order_args = call_args[0][0]
        assert order_args.expiration == 9999999999
