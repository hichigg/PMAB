"""Tests for src/core/config.py — YAML loading, defaults, SecretStr."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.core.config import (
    LoggingConfig,
    PolymarketConfig,
    RateLimitConfig,
    RiskConfig,
    Settings,
    load_settings,
    reset_settings,
)


@pytest.fixture(autouse=True)
def _clean_settings() -> None:
    """Reset the global settings cache before each test."""
    reset_settings()


class TestDefaults:
    """Settings should have sensible defaults when no YAML is provided."""

    def test_default_polymarket_config(self) -> None:
        cfg = PolymarketConfig()
        assert cfg.host == "https://clob.polymarket.com"
        assert cfg.chain_id == 137
        assert cfg.api_key == ""
        assert cfg.api_secret.get_secret_value() == ""

    def test_default_rate_limit_config(self) -> None:
        cfg = RateLimitConfig()
        assert cfg.burst_per_sec == 500
        assert cfg.sustained_per_sec == 60

    def test_default_risk_config(self) -> None:
        cfg = RiskConfig()
        assert cfg.max_daily_loss_usd == 500.0
        assert cfg.min_confidence == 0.99

    def test_default_logging_config(self) -> None:
        cfg = LoggingConfig()
        assert cfg.level == "INFO"
        assert cfg.format == "json"

    def test_default_settings(self) -> None:
        s = Settings()
        assert s.polymarket.chain_id == 137
        assert s.rate_limit.burst_per_sec == 500
        assert s.risk.max_daily_loss_usd == 500.0
        assert s.logging.level == "INFO"


class TestYamlLoading:
    """Settings should load correctly from YAML files."""

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        config_data = {
            "polymarket": {
                "api_key": "test-key",
                "api_secret": "test-secret",
                "api_passphrase": "test-pass",
                "private_key": "0xdeadbeef",
                "chain_id": 80001,
            },
            "risk": {
                "max_daily_loss_usd": 100,
                "min_confidence": 0.95,
            },
            "logging": {
                "level": "DEBUG",
                "format": "console",
            },
        }
        config_file = tmp_path / "settings.yaml"
        config_file.write_text(yaml.dump(config_data))

        settings = load_settings(config_file)

        assert settings.polymarket.api_key == "test-key"
        assert settings.polymarket.api_secret.get_secret_value() == "test-secret"
        assert settings.polymarket.api_passphrase.get_secret_value() == "test-pass"
        assert settings.polymarket.private_key.get_secret_value() == "0xdeadbeef"
        assert settings.polymarket.chain_id == 80001
        assert settings.risk.max_daily_loss_usd == 100
        assert settings.risk.min_confidence == 0.95
        assert settings.logging.level == "DEBUG"
        assert settings.logging.format == "console"

    def test_load_missing_file_uses_defaults(self, tmp_path: Path) -> None:
        settings = load_settings(tmp_path / "nonexistent.yaml")
        assert settings.polymarket.chain_id == 137
        assert settings.risk.max_daily_loss_usd == 500.0

    def test_load_empty_file_uses_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "empty.yaml"
        config_file.write_text("")
        settings = load_settings(config_file)
        assert settings.polymarket.chain_id == 137

    def test_partial_yaml_merges_with_defaults(self, tmp_path: Path) -> None:
        config_data = {"polymarket": {"chain_id": 80001}}
        config_file = tmp_path / "settings.yaml"
        config_file.write_text(yaml.dump(config_data))

        settings = load_settings(config_file)
        assert settings.polymarket.chain_id == 80001
        # Other defaults still intact
        assert settings.polymarket.host == "https://clob.polymarket.com"
        assert settings.risk.max_daily_loss_usd == 500.0


class TestSecretStr:
    """Sensitive fields should use SecretStr to prevent leaking."""

    def test_secret_str_repr_does_not_leak(self) -> None:
        cfg = PolymarketConfig(
            api_secret="super-secret",  # type: ignore[arg-type]
            private_key="0xdeadbeef",  # type: ignore[arg-type]
        )
        repr_str = repr(cfg)
        assert "super-secret" not in repr_str
        assert "0xdeadbeef" not in repr_str
        assert "**********" in repr_str

    def test_secret_str_get_value(self) -> None:
        cfg = PolymarketConfig(
            api_secret="my-secret",  # type: ignore[arg-type]
        )
        assert cfg.api_secret.get_secret_value() == "my-secret"
