"""Structured logging setup using structlog."""

from __future__ import annotations

import logging
import sys

import structlog

from src.core.config import get_settings


def setup_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Configure structlog with JSON or console renderer.

    Args:
        level: Log level override (e.g. "DEBUG"). Uses config if None.
        fmt: Renderer format override ("json" or "console"). Uses config if None.
    """
    settings = get_settings()
    log_level = getattr(logging, (level or settings.logging.level).upper(), logging.INFO)
    log_format = fmt or settings.logging.format

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if log_format == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)
