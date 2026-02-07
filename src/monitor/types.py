"""Domain types for the monitoring / alerting subsystem."""

from __future__ import annotations

import time
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field


class Severity(IntEnum):
    """Alert severity — ordered so comparisons work naturally."""

    DEBUG = 0
    INFO = 1
    WARNING = 2
    CRITICAL = 3


class AlertMessage(BaseModel):
    """Normalised alert ready for dispatch to channels."""

    severity: Severity
    title: str
    body: str = ""
    fields: dict[str, str] = Field(default_factory=dict)
    source_event_type: str = ""
    timestamp: float = Field(default_factory=time.time)
    raw: dict[str, Any] = Field(default_factory=dict)
