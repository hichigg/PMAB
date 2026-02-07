"""Notification channels — Telegram and Discord delivery."""

from __future__ import annotations

import abc
from html import escape as html_escape

import aiohttp
import structlog

from src.core.config import DiscordConfig, TelegramConfig
from src.monitor.types import AlertMessage, Severity

logger = structlog.get_logger(__name__)

# Discord embed colours keyed by severity.
_DISCORD_COLORS: dict[Severity, int] = {
    Severity.DEBUG: 0x95A5A6,    # grey
    Severity.INFO: 0x2ECC71,     # green
    Severity.WARNING: 0xF39C12,  # orange
    Severity.CRITICAL: 0xE74C3C, # red
}


class NotificationChannel(abc.ABC):
    """Base class for alert delivery channels."""

    @abc.abstractmethod
    async def send(self, msg: AlertMessage) -> bool:
        """Send an alert message. Returns True on success."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release resources (HTTP sessions, etc.)."""


class TelegramChannel(NotificationChannel):
    """Delivers alerts via the Telegram Bot API (HTML parse mode)."""

    def __init__(self, config: TelegramConfig) -> None:
        self._token = config.bot_token.get_secret_value()
        self._chat_id = config.chat_id
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, msg: AlertMessage) -> bool:
        severity_label = msg.severity.name
        text_parts = [f"<b>[{severity_label}] {html_escape(msg.title)}</b>"]
        if msg.body:
            text_parts.append(html_escape(msg.body))
        if msg.fields:
            lines = [
                f"  <code>{html_escape(k)}</code>: {html_escape(v)}"
                for k, v in msg.fields.items()
            ]
            text_parts.append("\n".join(lines))

        text = "\n".join(text_parts)
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }

        try:
            session = self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                body = await resp.text()
                logger.warning(
                    "telegram_send_failed",
                    status=resp.status,
                    body=body[:200],
                )
                return False
        except Exception:
            logger.exception("telegram_send_error")
            return False

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


class DiscordChannel(NotificationChannel):
    """Delivers alerts via a Discord webhook with colour-coded embeds."""

    def __init__(self, config: DiscordConfig) -> None:
        self._webhook_url = config.webhook_url.get_secret_value()
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, msg: AlertMessage) -> bool:
        color = _DISCORD_COLORS.get(msg.severity, 0x95A5A6)
        embed_fields = [
            {"name": k, "value": v, "inline": True}
            for k, v in msg.fields.items()
        ]
        embed: dict = {
            "title": f"[{msg.severity.name}] {msg.title}",
            "color": color,
        }
        if msg.body:
            embed["description"] = msg.body
        if embed_fields:
            embed["fields"] = embed_fields

        payload = {"embeds": [embed]}

        try:
            session = self._get_session()
            async with session.post(self._webhook_url, json=payload) as resp:
                if resp.status in (200, 204):
                    return True
                body = await resp.text()
                logger.warning(
                    "discord_send_failed",
                    status=resp.status,
                    body=body[:200],
                )
                return False
        except Exception:
            logger.exception("discord_send_error")
            return False

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
