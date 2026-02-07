"""Tests for notification channels — HTTP mocking, error handling, session management."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import SecretStr

from src.core.config import DiscordConfig, TelegramConfig
from src.monitor.channels import DiscordChannel, TelegramChannel
from src.monitor.types import AlertMessage, Severity


# ── Helpers ─────────────────────────────────────────────────────


def _msg(**kw: object) -> AlertMessage:
    defaults: dict[str, object] = {
        "severity": Severity.INFO,
        "title": "TEST_TITLE",
        "body": "test body",
        "fields": {"key": "value"},
        "source_event_type": "TEST",
        "timestamp": 1000.0,
    }
    defaults.update(kw)
    return AlertMessage(**defaults)  # type: ignore[arg-type]


def _tg_config(**kw: object) -> TelegramConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "bot_token": SecretStr("fake-token"),
        "chat_id": "12345",
    }
    defaults.update(kw)
    return TelegramConfig(**defaults)  # type: ignore[arg-type]


def _dc_config(**kw: object) -> DiscordConfig:
    defaults: dict[str, object] = {
        "enabled": True,
        "webhook_url": SecretStr("https://discord.com/api/webhooks/fake"),
    }
    defaults.update(kw)
    return DiscordConfig(**defaults)  # type: ignore[arg-type]


def _mock_response(status: int = 200, text: str = "ok") -> AsyncMock:
    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


# ── TelegramChannel ────────────────────────────────────────────


class TestTelegramChannel:
    async def test_send_success(self) -> None:
        ch = TelegramChannel(_tg_config())
        mock_resp = _mock_response(200)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        ch._session = mock_session

        result = await ch.send(_msg())
        assert result is True
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert "fake-token" in call_args[0][0]
        payload = call_args[1]["json"]
        assert payload["chat_id"] == "12345"
        assert payload["parse_mode"] == "HTML"

    async def test_send_failure_status(self) -> None:
        ch = TelegramChannel(_tg_config())
        mock_resp = _mock_response(400, "bad request")
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        ch._session = mock_session

        result = await ch.send(_msg())
        assert result is False

    async def test_send_exception(self) -> None:
        ch = TelegramChannel(_tg_config())
        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=ConnectionError("timeout"))
        mock_session.closed = False
        ch._session = mock_session

        result = await ch.send(_msg())
        assert result is False

    async def test_html_escaping(self) -> None:
        ch = TelegramChannel(_tg_config())
        mock_resp = _mock_response(200)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        ch._session = mock_session

        msg = _msg(title="<script>alert('xss')</script>", body="a & b")
        await ch.send(msg)
        payload = mock_session.post.call_args[1]["json"]
        assert "<script>" not in payload["text"]
        assert "&lt;script&gt;" in payload["text"]
        assert "&amp;" in payload["text"]

    async def test_severity_label_in_text(self) -> None:
        ch = TelegramChannel(_tg_config())
        mock_resp = _mock_response(200)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        ch._session = mock_session

        msg = _msg(severity=Severity.CRITICAL)
        await ch.send(msg)
        payload = mock_session.post.call_args[1]["json"]
        assert "CRITICAL" in payload["text"]

    async def test_close_session(self) -> None:
        ch = TelegramChannel(_tg_config())
        mock_session = AsyncMock()
        mock_session.closed = False
        ch._session = mock_session

        await ch.close()
        mock_session.close.assert_awaited_once()

    async def test_close_when_no_session(self) -> None:
        ch = TelegramChannel(_tg_config())
        await ch.close()  # should not raise

    async def test_lazy_session_creation(self) -> None:
        ch = TelegramChannel(_tg_config())
        assert ch._session is None
        session = ch._get_session()
        assert session is not None
        await ch.close()


# ── DiscordChannel ──────────────────────────────────────────────


class TestDiscordChannel:
    async def test_send_success(self) -> None:
        ch = DiscordChannel(_dc_config())
        mock_resp = _mock_response(204)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        ch._session = mock_session

        result = await ch.send(_msg())
        assert result is True
        call_args = mock_session.post.call_args
        payload = call_args[1]["json"]
        assert len(payload["embeds"]) == 1
        embed = payload["embeds"][0]
        assert "TEST_TITLE" in embed["title"]

    async def test_send_200_also_success(self) -> None:
        ch = DiscordChannel(_dc_config())
        mock_resp = _mock_response(200)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        ch._session = mock_session

        result = await ch.send(_msg())
        assert result is True

    async def test_send_failure(self) -> None:
        ch = DiscordChannel(_dc_config())
        mock_resp = _mock_response(429, "rate limited")
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        ch._session = mock_session

        result = await ch.send(_msg())
        assert result is False

    async def test_send_exception(self) -> None:
        ch = DiscordChannel(_dc_config())
        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=ConnectionError("timeout"))
        mock_session.closed = False
        ch._session = mock_session

        result = await ch.send(_msg())
        assert result is False

    async def test_color_by_severity(self) -> None:
        ch = DiscordChannel(_dc_config())
        for severity, expected_color in [
            (Severity.INFO, 0x2ECC71),
            (Severity.WARNING, 0xF39C12),
            (Severity.CRITICAL, 0xE74C3C),
        ]:
            mock_resp = _mock_response(204)
            mock_session = MagicMock()
            mock_session.post = MagicMock(return_value=mock_resp)
            mock_session.closed = False
            ch._session = mock_session

            await ch.send(_msg(severity=severity))
            payload = mock_session.post.call_args[1]["json"]
            assert payload["embeds"][0]["color"] == expected_color

    async def test_embed_fields(self) -> None:
        ch = DiscordChannel(_dc_config())
        mock_resp = _mock_response(204)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        ch._session = mock_session

        msg = _msg(fields={"token": "tok_1", "price": "0.90"})
        await ch.send(msg)
        payload = mock_session.post.call_args[1]["json"]
        embed = payload["embeds"][0]
        assert len(embed["fields"]) == 2
        names = {f["name"] for f in embed["fields"]}
        assert "token" in names
        assert "price" in names

    async def test_embed_description_from_body(self) -> None:
        ch = DiscordChannel(_dc_config())
        mock_resp = _mock_response(204)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        ch._session = mock_session

        msg = _msg(body="detailed reason")
        await ch.send(msg)
        payload = mock_session.post.call_args[1]["json"]
        assert payload["embeds"][0]["description"] == "detailed reason"

    async def test_no_body_no_description(self) -> None:
        ch = DiscordChannel(_dc_config())
        mock_resp = _mock_response(204)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        ch._session = mock_session

        msg = _msg(body="")
        await ch.send(msg)
        payload = mock_session.post.call_args[1]["json"]
        assert "description" not in payload["embeds"][0]

    async def test_close_session(self) -> None:
        ch = DiscordChannel(_dc_config())
        mock_session = AsyncMock()
        mock_session.closed = False
        ch._session = mock_session

        await ch.close()
        mock_session.close.assert_awaited_once()

    async def test_webhook_url_used(self) -> None:
        ch = DiscordChannel(_dc_config())
        mock_resp = _mock_response(204)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        ch._session = mock_session

        await ch.send(_msg())
        url = mock_session.post.call_args[0][0]
        assert url == "https://discord.com/api/webhooks/fake"
