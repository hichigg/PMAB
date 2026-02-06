"""Order pre-signing: sign orders during analysis, POST during execution."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import structlog
from py_clob_client.clob_types import CreateOrderOptions, OrderArgs
from pydantic import BaseModel, Field

from src.core.types import OrderRequest, OrderType, Side
from src.polymarket.market_params import MarketParams

logger = structlog.stdlib.get_logger()

# Default expiration: 5 minutes from now (unix seconds)
DEFAULT_EXPIRATION_SECS = 300
# Considered stale if less than this many seconds until expiration
STALENESS_THRESHOLD_SECS = 30


class PreSignedOrder(BaseModel):
    """A signed order bundled with metadata for lifecycle management."""

    signed_order: Any  # py_order_utils SignedOrder — not a pydantic model
    request: OrderRequest
    market_params: MarketParams
    created_at: float = Field(default_factory=time.monotonic)
    expiration_ts: int = 0  # unix timestamp; 0 = no expiration
    order_type: OrderType = OrderType.GTC

    model_config = {"arbitrary_types_allowed": True}

    @property
    def is_expired(self) -> bool:
        """Check if the order's on-chain expiration has passed."""
        if self.expiration_ts == 0:
            return False
        return time.time() >= self.expiration_ts

    @property
    def is_stale(self) -> bool:
        """Check if expiration is too close to safely post."""
        if self.expiration_ts == 0:
            return False
        return (self.expiration_ts - time.time()) < STALENESS_THRESHOLD_SECS

    @property
    def time_until_expiry(self) -> float | None:
        """Seconds remaining until expiration, or None if no expiration."""
        if self.expiration_ts == 0:
            return None
        return max(0.0, self.expiration_ts - time.time())

    @property
    def age_secs(self) -> float:
        """How long ago this order was signed, in seconds."""
        return time.monotonic() - self.created_at


class OrderPreSigner:
    """Signs orders without posting them, using cached market params.

    Calls ``sdk.builder.create_order()`` directly, bypassing the SDK's
    own parameter resolution (tick_size, neg_risk, fee_rate) since we
    already have those cached in MarketParams.
    """

    def __init__(
        self,
        sdk: Any,
        default_expiration_secs: int = DEFAULT_EXPIRATION_SECS,
    ) -> None:
        self._sdk = sdk
        self._default_expiration_secs = default_expiration_secs

    async def presign(
        self,
        request: OrderRequest,
        params: MarketParams,
        expiration_secs: int | None = None,
    ) -> PreSignedOrder:
        """Sign a single order without posting it.

        Args:
            request: The order specification.
            params: Pre-fetched market parameters for this token.
            expiration_secs: Override for order expiration window.
                If None, uses default_expiration_secs.
                If 0, the order has no on-chain expiration.
        """
        exp_secs = (
            expiration_secs
            if expiration_secs is not None
            else self._default_expiration_secs
        )
        expiration_ts = int(time.time()) + exp_secs if exp_secs > 0 else 0

        order_args = OrderArgs(
            token_id=request.token_id,
            price=float(request.price),
            size=float(request.size),
            side=request.side.value,
            fee_rate_bps=params.fee_rate_bps,
            expiration=(
                expiration_ts if request.expiration is None else request.expiration
            ),
        )

        options = CreateOrderOptions(
            tick_size=params.tick_size,
            neg_risk=params.neg_risk,
        )

        # Signing is CPU-bound (ECDSA), run in thread pool
        signed_order = await asyncio.to_thread(
            self._sdk.builder.create_order, order_args, options
        )

        logger.debug(
            "order_presigned",
            token_id=request.token_id,
            side=request.side.value,
            price=str(request.price),
            size=str(request.size),
            expiration_ts=expiration_ts,
        )

        return PreSignedOrder(
            signed_order=signed_order,
            request=request,
            market_params=params,
            expiration_ts=expiration_ts,
            order_type=request.order_type,
        )

    async def presign_batch(
        self,
        requests: list[OrderRequest],
        params_map: dict[str, MarketParams],
        expiration_secs: int | None = None,
    ) -> list[PreSignedOrder]:
        """Sign multiple orders concurrently.

        Args:
            requests: List of order specifications.
            params_map: Dict of token_id -> MarketParams.
            expiration_secs: Shared expiration override for all orders.

        Raises:
            KeyError: If params_map is missing a required token_id.
        """
        tasks = [
            self.presign(req, params_map[req.token_id], expiration_secs)
            for req in requests
        ]
        return list(await asyncio.gather(*tasks))

    async def presign_price_ladder(
        self,
        token_id: str,
        side: Side,
        prices: list[Decimal],
        size: Decimal,
        params: MarketParams,
        expiration_secs: int | None = None,
    ) -> list[PreSignedOrder]:
        """Pre-sign orders at multiple price levels for the same token."""
        requests = [
            OrderRequest(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                order_type=OrderType.GTC,
            )
            for price in prices
        ]
        params_map = {token_id: params}
        return await self.presign_batch(requests, params_map, expiration_secs)
