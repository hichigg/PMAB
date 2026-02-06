"""Position sizing — converts signals into sized trade actions."""

from __future__ import annotations

from decimal import Decimal

import structlog

from src.core.config import RiskConfig, StrategyConfig
from src.core.types import (
    OrderType,
    Side,
    Signal,
    SignalDirection,
    TradeAction,
)

logger = structlog.stdlib.get_logger()


class PositionSizer:
    """Sizes positions based on signal edge, confidence, and risk limits."""

    def __init__(
        self,
        config: StrategyConfig | None = None,
        risk_config: RiskConfig | None = None,
    ) -> None:
        from src.core.config import get_settings

        settings = get_settings()
        self._config = config or settings.strategy
        self._risk = risk_config or settings.risk

    def size(self, signal: Signal) -> TradeAction | None:
        """Size a trade from a signal.

        Returns None if:
        - Estimated profit is below min_profit_usd
        - Orderbook depth is insufficient

        Sizing logic:
        1. Start with base_size_usd
        2. Optionally apply Kelly criterion
        3. Cap at max_size_usd and orderbook depth fraction
        """
        # Start with base size
        size_usd = Decimal(str(self._config.base_size_usd))

        # Optionally apply Kelly sizing
        if self._config.use_kelly_sizing:
            kelly = self._kelly_size(signal)
            if kelly > Decimal("0"):
                size_usd = kelly

        # Cap at max_size_usd
        max_size = Decimal(str(self._config.max_size_usd))
        size_usd = min(size_usd, max_size)

        # Cap to orderbook depth
        size_usd = self._cap_to_depth(size_usd, signal)

        if size_usd <= Decimal("0"):
            logger.debug("sizing_zero_after_caps", signal_edge=float(signal.edge))
            return None

        # Convert USD size to token quantity
        # For binary markets: size_tokens = size_usd / price
        price = signal.current_price
        if price <= Decimal("0"):
            return None

        size_tokens = size_usd / price

        # Estimated profit
        estimated_profit = size_tokens * signal.edge

        # Check minimum profit
        min_profit = Decimal(str(self._risk.min_profit_usd))
        if estimated_profit < min_profit:
            logger.debug(
                "sizing_below_min_profit",
                estimated_profit=float(estimated_profit),
                min_profit=float(min_profit),
            )
            return None

        # Determine side
        side = Side.BUY if signal.direction == SignalDirection.BUY else Side.SELL

        # Order type
        order_type_str = self._config.default_order_type.upper()
        order_type = OrderType.FOK if order_type_str == "FOK" else OrderType.GTC

        return TradeAction(
            signal=signal,
            token_id=signal.match.target_token_id,
            side=side,
            price=price,
            size=size_tokens,
            order_type=order_type,
            max_slippage=Decimal(str(self._config.max_slippage)),
            estimated_profit_usd=estimated_profit,
            reason=(
                f"edge={signal.edge:.4f} conf={signal.confidence:.2f} "
                f"size=${size_usd:.2f}"
            ),
        )

    def _kelly_size(self, signal: Signal) -> Decimal:
        """Calculate Kelly-optimal size.

        Kelly fraction: f* = (p * b - q) / b
        where p = probability of win, b = odds, q = 1 - p

        We apply a fractional Kelly (kelly_fraction) for safety.
        """
        p = Decimal(str(signal.confidence))
        q = Decimal("1") - p

        # Edge-implied odds: if we buy at current_price, payout is 1.00
        # So b = (1 - price) / price for a buy
        price = signal.current_price
        if price <= Decimal("0") or price >= Decimal("1"):
            return Decimal("0")

        if signal.direction == SignalDirection.BUY:
            b = (Decimal("1") - price) / price
        else:
            # For sell: profit is price, loss is 1 - price
            b = price / (Decimal("1") - price)

        if b <= Decimal("0"):
            return Decimal("0")

        kelly_f = (p * b - q) / b
        if kelly_f <= Decimal("0"):
            return Decimal("0")

        # Apply fractional Kelly
        fraction = Decimal(str(self._config.kelly_fraction))
        bankroll = Decimal(str(self._config.max_size_usd))

        return kelly_f * fraction * bankroll

    def _cap_to_depth(self, size_usd: Decimal, signal: Signal) -> Decimal:
        """Cap size to a fraction of available orderbook depth."""
        opp = signal.match.opportunity
        depth = opp.depth_usd
        if depth <= Decimal("0"):
            return Decimal("0")

        # Don't take more than 20% of visible depth
        max_from_depth = depth * Decimal("0.20")
        return min(size_usd, max_from_depth)
