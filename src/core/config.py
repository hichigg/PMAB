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


class Settings(BaseModel):
    """Root settings container."""

    polymarket: PolymarketConfig = PolymarketConfig()
    rate_limit: RateLimitConfig = RateLimitConfig()
    risk: RiskConfig = RiskConfig()
    scanner: ScannerConfig = ScannerConfig()
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
