"""Pydantic settings loaded from YAML configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, SecretStr

_settings: Settings | None = None

_DEFAULT_CONFIG_PATH = Path("config/settings.yaml")


class PolymarketConfig(BaseModel):
    """Polymarket CLOB API configuration."""

    host: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    api_key: str = ""
    api_secret: SecretStr = SecretStr("")
    api_passphrase: SecretStr = SecretStr("")
    private_key: SecretStr = SecretStr("")
    chain_id: int = 137


class RateLimitConfig(BaseModel):
    """Rate limiting configuration for CLOB API."""

    burst_per_sec: int = 500
    sustained_per_sec: int = 60


class RiskConfig(BaseModel):
    """Risk management configuration."""

    max_daily_loss_usd: float = 500.0
    max_position_usd: float = 5000.0
    max_bankroll_pct_per_event: float = 0.20
    min_profit_usd: float = 10.0
    min_confidence: float = 0.99
    min_orderbook_depth_usd: float = 500.0
    max_spread: float = 0.10
    max_concurrent_positions: int = 10
    bankroll_usd: float = 10000.0


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = "INFO"
    format: str = "json"


class ScannerConfig(BaseModel):
    """Market scanner configuration."""

    scan_interval_secs: float = 60.0
    max_tracked_markets: int = 50
    orderbook_batch_size: int = 10
    score_weights: dict[str, float] = {
        "depth": 0.4,
        "spread": 0.4,
        "recency": 0.2,
    }


class EconomicFeedConfig(BaseModel):
    """Economic data feed configuration."""

    enabled: bool = False
    source: str = "bls_scraper"
    poll_interval_ms: int = 100
    indicators: list[str] = [
        "CPI",
        "CORE_CPI",
        "NFP",
        "UNEMPLOYMENT",
        "PPI",
    ]
    base_url: str = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    api_key: str = ""


class SportsFeedConfig(BaseModel):
    """Sports data feed configuration — ESPN scoreboard polling."""

    enabled: bool = False
    source: str = "espn"
    poll_interval_ms: int = 10000
    leagues: list[str] = ["NFL", "NBA", "MLB", "NHL"]
    base_url: str = "https://site.api.espn.com/apis/site/v2/sports"


class CryptoFeedConfig(BaseModel):
    """Crypto data feed configuration — Binance WS primary + cross-validation."""

    enabled: bool = False
    exchanges: list[str] = ["binance", "coinbase", "kraken"]
    pairs: list[str] = ["BTC_USDT", "ETH_USDT"]
    poll_interval_ms: int = 5000
    price_move_threshold_pct: float = 2.0
    cross_validation_threshold_pct: float = 1.0
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"
    coinbase_ws_url: str = "wss://advanced-trade-ws.coinbase.com"
    kraken_ws_url: str = "wss://ws.kraken.com/v2"
    reconnect_base_secs: float = 1.0
    reconnect_cap_secs: float = 30.0
    ping_interval_secs: float = 10.0


class FeedsConfig(BaseModel):
    """Container for all feed configurations."""

    economic: EconomicFeedConfig = EconomicFeedConfig()
    sports: SportsFeedConfig = SportsFeedConfig()
    crypto: CryptoFeedConfig = CryptoFeedConfig()


class StrategyConfig(BaseModel):
    """Core arbitrage strategy configuration."""

    match_confidence_threshold: float = 0.8
    min_edge: float = 0.05
    min_confidence: float = 0.99
    max_staleness_secs: float = 60.0
    base_size_usd: float = 100.0
    max_size_usd: float = 1000.0
    kelly_fraction: float = 0.25
    use_kelly_sizing: bool = False
    default_order_type: str = "FOK"
    max_slippage: float = 0.02
    use_presigned_orders: bool = True
    economic_min_edge: float | None = None
    sports_min_edge: float | None = None
    crypto_min_edge: float | None = None


class Settings(BaseModel):
    """Root settings container."""

    polymarket: PolymarketConfig = PolymarketConfig()
    rate_limit: RateLimitConfig = RateLimitConfig()
    risk: RiskConfig = RiskConfig()
    scanner: ScannerConfig = ScannerConfig()
    feeds: FeedsConfig = FeedsConfig()
    strategy: StrategyConfig = StrategyConfig()
    logging: LoggingConfig = LoggingConfig()


def load_settings(path: str | Path | None = None) -> Settings:
    """Load settings from a YAML file and cache globally.

    Args:
        path: Path to YAML config. Defaults to config/settings.yaml.

    Returns:
        Parsed Settings instance.
    """
    global _settings  # noqa: PLW0603

    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH

    data: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f)
            if isinstance(raw, dict):
                data = raw

    _settings = Settings(**data)
    return _settings


def get_settings() -> Settings:
    """Return the cached settings, loading defaults if not yet loaded."""
    global _settings  # noqa: PLW0603
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings() -> None:
    """Reset the cached settings (useful for testing)."""
    global _settings  # noqa: PLW0603
    _settings = None
