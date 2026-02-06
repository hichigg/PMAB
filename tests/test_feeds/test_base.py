"""Tests for BaseFeed — lifecycle, event system, error handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from src.core.types import FeedEvent, FeedEventType, FeedType
from src.feeds.base import BaseFeed


class StubFeed(BaseFeed):
    """Concrete BaseFeed for testing."""

    def __init__(
        self,
        poll_results: list[list[FeedEvent]] | None = None,
        poll_error: Exception | None = None,
        poll_interval_ms: int = 50,
    ) -> None:
        super().__init__(feed_type=FeedType.ECONOMIC, poll_interval_ms=poll_interval_ms)
        self._poll_results = poll_results or []
        self._poll_error = poll_error
        self._poll_index = 0
        self.connect_called = False
        self.close_called = False

    async def connect(self) -> None:
        self.connect_called = True

    async def close(self) -> None:
        self.close_called = True

    async def poll(self) -> list[FeedEvent]:
        if self._poll_error is not None:
            raise self._poll_error
        if self._poll_index < len(self._poll_results):
            result = self._poll_results[self._poll_index]
            self._poll_index += 1
            return result
        return []


class TestBaseFeedLifecycle:
    async def test_start_calls_connect(self) -> None:
        feed = StubFeed()
        await feed.start()
        assert feed.connect_called
        assert feed.running
        await feed.stop()

    async def test_stop_calls_close(self) -> None:
        feed = StubFeed()
        await feed.start()
        await feed.stop()
        assert feed.close_called
        assert not feed.running

    async def test_start_is_idempotent(self) -> None:
        feed = StubFeed()
        await feed.start()
        task1 = feed._task
        await feed.start()  # should be no-op
        assert feed._task is task1
        await feed.stop()

    async def test_async_context_manager(self) -> None:
        feed = StubFeed()
        async with feed:
            assert feed.running
            assert feed.connect_called
        assert not feed.running
        assert feed.close_called


class TestBaseFeedEvents:
    async def test_event_callback_receives_events(self) -> None:
        event = FeedEvent(
            feed_type=FeedType.ECONOMIC,
            event_type=FeedEventType.DATA_RELEASED,
            indicator="CPI",
            value="3.2",
        )
        feed = StubFeed(poll_results=[[event]])
        received: list[FeedEvent] = []
        feed.on_event(received.append)  # type: ignore[arg-type]
        await feed.start()
        await asyncio.sleep(0.15)
        await feed.stop()
        # Should have: FEED_CONNECTED, DATA_RELEASED, ..., FEED_DISCONNECTED
        data_events = [e for e in received if e.event_type == FeedEventType.DATA_RELEASED]
        assert len(data_events) >= 1
        assert data_events[0].indicator == "CPI"

    async def test_start_emits_connected(self) -> None:
        feed = StubFeed()
        received: list[FeedEvent] = []
        feed.on_event(received.append)  # type: ignore[arg-type]
        await feed.start()
        await asyncio.sleep(0.02)
        await feed.stop()
        types = [e.event_type for e in received]
        assert FeedEventType.FEED_CONNECTED in types

    async def test_stop_emits_disconnected(self) -> None:
        feed = StubFeed()
        received: list[FeedEvent] = []
        feed.on_event(received.append)  # type: ignore[arg-type]
        await feed.start()
        await asyncio.sleep(0.02)
        await feed.stop()
        types = [e.event_type for e in received]
        assert FeedEventType.FEED_DISCONNECTED in types

    async def test_async_callback(self) -> None:
        event = FeedEvent(
            feed_type=FeedType.ECONOMIC,
            event_type=FeedEventType.DATA_RELEASED,
            value="100",
        )
        feed = StubFeed(poll_results=[[event]])
        cb = AsyncMock()
        feed.on_event(cb)
        await feed.start()
        await asyncio.sleep(0.15)
        await feed.stop()
        assert cb.call_count >= 1


class TestBaseFeedErrorHandling:
    async def test_poll_error_increments_error_count(self) -> None:
        feed = StubFeed(poll_error=RuntimeError("boom"), poll_interval_ms=30)
        await feed.start()
        await asyncio.sleep(0.15)
        await feed.stop()
        assert feed.error_count >= 1

    async def test_poll_error_emits_feed_error_event(self) -> None:
        feed = StubFeed(poll_error=RuntimeError("boom"), poll_interval_ms=30)
        received: list[FeedEvent] = []
        feed.on_event(received.append)  # type: ignore[arg-type]
        await feed.start()
        await asyncio.sleep(0.15)
        await feed.stop()
        error_events = [e for e in received if e.event_type == FeedEventType.FEED_ERROR]
        assert len(error_events) >= 1

    async def test_callback_exception_does_not_crash_loop(self) -> None:
        event = FeedEvent(
            feed_type=FeedType.ECONOMIC,
            event_type=FeedEventType.DATA_RELEASED,
            value="x",
        )
        feed = StubFeed(poll_results=[[event], [event]], poll_interval_ms=30)

        def bad_callback(evt: FeedEvent) -> None:
            raise ValueError("callback exploded")

        feed.on_event(bad_callback)
        await feed.start()
        await asyncio.sleep(0.15)
        await feed.stop()
        # Feed should still be running (not crashed)
        assert feed.last_poll_time > 0


class TestBaseFeedProperties:
    async def test_last_poll_time_updates(self) -> None:
        feed = StubFeed(poll_interval_ms=30)
        assert feed.last_poll_time == 0.0
        await feed.start()
        await asyncio.sleep(0.1)
        await feed.stop()
        assert feed.last_poll_time > 0

    def test_feed_type_property(self) -> None:
        feed = StubFeed()
        assert feed.feed_type == FeedType.ECONOMIC
