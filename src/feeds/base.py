"""Abstract base feed — poll loop, event system, lifecycle management."""

from __future__ import annotations

import abc
import asyncio
import time
from collections.abc import Awaitable, Callable
from types import TracebackType

import structlog

from src.core.types import FeedEvent, FeedEventType, FeedType

logger = structlog.stdlib.get_logger()

# Type alias for feed event callbacks
FeedEventCallback = Callable[[FeedEvent], Awaitable[None] | None]


class BaseFeed(abc.ABC):
    """Abstract base class for data feeds.

    Subclasses implement ``connect()``, ``close()``, and ``poll()`` —
    the base class handles the background loop, event dispatch, and lifecycle.

    Usage::

        feed = MyFeed(poll_interval_ms=500)
        feed.on_event(my_callback)
        async with feed:
            await asyncio.sleep(60)  # runs for 60 seconds
    """

    def __init__(self, feed_type: FeedType, poll_interval_ms: int = 100) -> None:
        self._feed_type = feed_type
        self._poll_interval_ms = poll_interval_ms
        self._callbacks: list[FeedEventCallback] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._error_count = 0
        self._last_poll_time: float = 0.0

    @property
    def feed_type(self) -> FeedType:
        return self._feed_type

    @property
    def running(self) -> bool:
        return self._running

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def last_poll_time(self) -> float:
        return self._last_poll_time

    def on_event(self, callback: FeedEventCallback) -> None:
        """Register a callback for feed events."""
        self._callbacks.append(callback)

    async def _emit(self, event: FeedEvent) -> None:
        """Dispatch a feed event to all registered callbacks."""
        for cb in self._callbacks:
            try:
                result = cb(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "feed_event_callback_error",
                    feed_type=self._feed_type,
                    event_type=event.event_type,
                )

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish connection to the data source."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Close connection to the data source."""

    @abc.abstractmethod
    async def poll(self) -> list[FeedEvent]:
        """Poll the data source for new events.

        Returns a list of new events since the last poll.
        """

    async def start(self) -> None:
        """Start the background poll loop."""
        if self._running:
            return
        self._running = True
        await self.connect()
        self._task = asyncio.create_task(self._poll_loop())
        await self._emit(FeedEvent(
            feed_type=self._feed_type,
            event_type=FeedEventType.FEED_CONNECTED,
            received_at=time.time(),
        ))
        logger.info(
            "feed_started",
            feed_type=self._feed_type,
            poll_interval_ms=self._poll_interval_ms,
        )

    async def stop(self) -> None:
        """Stop the poll loop and close the connection."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.close()
        await self._emit(FeedEvent(
            feed_type=self._feed_type,
            event_type=FeedEventType.FEED_DISCONNECTED,
            received_at=time.time(),
        ))
        logger.info("feed_stopped", feed_type=self._feed_type)

    async def _poll_loop(self) -> None:
        """Background loop that calls poll() at the configured interval."""
        interval_secs = self._poll_interval_ms / 1000.0
        while self._running:
            try:
                events = await self.poll()
                self._last_poll_time = time.time()
                for event in events:
                    await self._emit(event)
            except asyncio.CancelledError:
                break
            except Exception:
                self._error_count += 1
                logger.exception(
                    "feed_poll_error",
                    feed_type=self._feed_type,
                    error_count=self._error_count,
                )
                await self._emit(FeedEvent(
                    feed_type=self._feed_type,
                    event_type=FeedEventType.FEED_ERROR,
                    received_at=time.time(),
                ))

            try:
                await asyncio.sleep(interval_secs)
            except asyncio.CancelledError:
                break

    async def __aenter__(self) -> BaseFeed:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.stop()
