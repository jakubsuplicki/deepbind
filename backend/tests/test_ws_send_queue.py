"""Tests for `services.ws_send_queue.WSSendQueue`.

Covers the contracts the chat router depends on:

  - FIFO ordering across enqueues.
  - `enqueue()` returns fast (does not couple to `ws.send_json` latency).
  - A stuck consumer doesn't deadlock the producer — the bounded queue
    plus enqueue timeout means a dead client backs up but doesn't
    indefinitely block the dispatcher.
  - Once a send raises, the queue fail-latches: subsequent enqueues
    return False, the consumer exits, and `close()` is idempotent.
  - `close()` waits for the consumer to drain and times out cleanly
    when the consumer is stuck.
"""

from __future__ import annotations

import asyncio
from typing import Any, List
from unittest.mock import AsyncMock

import pytest

from services.ws_send_queue import WSSendQueue, attach, detach, queue_for

pytestmark = pytest.mark.anyio(backends=["asyncio"])


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


def _ws_with_recorder() -> tuple[Any, List[dict]]:
    """Return a fake WS object whose `send_json` records calls."""
    received: List[dict] = []

    async def _send_json(payload: dict) -> None:
        received.append(payload)

    ws = type("FakeWS", (), {})()
    ws.send_json = _send_json  # type: ignore[attr-defined]
    return ws, received


class TestFIFOOrdering:

    @pytest.mark.anyio
    async def test_enqueues_drain_in_order(self):
        ws, received = _ws_with_recorder()
        q = WSSendQueue(ws)
        q.start()
        try:
            for i in range(20):
                ok = await q.enqueue({"type": "evt", "i": i})
                assert ok is True
        finally:
            await q.close()

        assert [e["i"] for e in received] == list(range(20))

    @pytest.mark.anyio
    async def test_concurrent_enqueues_preserve_per_producer_order(self):
        """Even with concurrent producers, the consumer dequeues in
        the order asyncio.Queue accepted them. Per-producer order is
        preserved (which is what the chat router needs)."""
        ws, received = _ws_with_recorder()
        q = WSSendQueue(ws)
        q.start()

        async def _producer(prefix: str, n: int) -> None:
            for i in range(n):
                await q.enqueue({"type": "evt", "tag": f"{prefix}{i}"})

        try:
            await asyncio.gather(
                _producer("A", 10),
                _producer("B", 10),
                _producer("C", 10),
            )
        finally:
            await q.close()

        # Per-producer relative order is preserved.
        for prefix in "ABC":
            tags = [e["tag"] for e in received if e["tag"].startswith(prefix)]
            assert tags == [f"{prefix}{i}" for i in range(10)]


class TestProducerDecouplesFromConsumer:

    @pytest.mark.anyio
    async def test_enqueue_does_not_wait_on_send_json(self):
        """The chat-router contract: producer-side `await enqueue(...)`
        must not block on the consumer's `await ws.send_json(...)`.
        This is the property that decouples lock-hold time from WS
        write latency."""
        send_started = asyncio.Event()
        send_release = asyncio.Event()

        async def _slow_send(payload: dict) -> None:
            send_started.set()
            await send_release.wait()

        ws = type("FakeWS", (), {})()
        ws.send_json = _slow_send  # type: ignore[attr-defined]
        q = WSSendQueue(ws, maxsize=8)
        q.start()
        try:
            await q.enqueue({"type": "first"})
            await send_started.wait()  # consumer is now blocked inside send_json

            # Producer can keep enqueueing while consumer is stuck.
            for i in range(7):
                ok = await asyncio.wait_for(
                    q.enqueue({"type": "evt", "i": i}),
                    timeout=0.5,
                )
                assert ok is True
        finally:
            send_release.set()
            await q.close()

    @pytest.mark.anyio
    async def test_full_queue_with_stuck_consumer_times_out(self):
        """When the consumer is wedged AND the queue is full,
        producers see enqueue() return False after the timeout
        rather than waiting forever."""
        send_release = asyncio.Event()

        async def _wedged_send(payload: dict) -> None:
            await send_release.wait()

        ws = type("FakeWS", (), {})()
        ws.send_json = _wedged_send  # type: ignore[attr-defined]
        q = WSSendQueue(ws, maxsize=2, enqueue_timeout=0.2)
        q.start()
        try:
            # First enqueue: consumer pulls it, blocks on send_json.
            assert await q.enqueue({"i": 0}) is True
            # Next two: queue capacity (2 slots) absorbs them.
            assert await q.enqueue({"i": 1}) is True
            assert await q.enqueue({"i": 2}) is True
            # Queue is now full and consumer is wedged — next enqueue
            # must time out cleanly (not raise, just return False).
            assert await q.enqueue({"i": 3}) is False
        finally:
            send_release.set()
            await q.close()


class TestFailLatching:

    @pytest.mark.anyio
    async def test_send_failure_latches_queue(self):
        """Once `ws.send_json` raises, the queue refuses further work."""
        attempts = 0

        async def _failing_send(payload: dict) -> None:
            nonlocal attempts
            attempts += 1
            raise ConnectionError("boom")

        ws = type("FakeWS", (), {})()
        ws.send_json = _failing_send  # type: ignore[attr-defined]
        q = WSSendQueue(ws)
        q.start()
        try:
            await q.enqueue({"i": 0})
            # Wait for the consumer to process the failing send.
            for _ in range(50):
                if q.failed:
                    break
                await asyncio.sleep(0.01)
            assert q.failed is True
            # Subsequent enqueues should fast-return False.
            assert await q.enqueue({"i": 1}) is False
            assert await q.enqueue({"i": 2}) is False
            # Consumer was called exactly once before latching.
            assert attempts == 1
        finally:
            await q.close()


class TestClose:

    @pytest.mark.anyio
    async def test_close_drains_pending_events(self):
        """If we close while events are queued and the consumer is
        progressing normally, the consumer drains everything before
        the close completes."""
        ws, received = _ws_with_recorder()
        q = WSSendQueue(ws)
        q.start()
        for i in range(10):
            await q.enqueue({"i": i})
        await q.close()
        assert [e["i"] for e in received] == list(range(10))

    @pytest.mark.anyio
    async def test_close_with_stuck_consumer_cancels_after_timeout(self):
        """A consumer wedged inside `ws.send_json` must not block
        close() forever — drain_timeout cancels the consumer."""
        send_release = asyncio.Event()

        async def _wedged_send(payload: dict) -> None:
            await send_release.wait()

        ws = type("FakeWS", (), {})()
        ws.send_json = _wedged_send  # type: ignore[attr-defined]
        q = WSSendQueue(ws)
        q.start()
        await q.enqueue({"i": 0})
        # Don't release send_release — consumer is permanently wedged.
        # close() should cancel after drain_timeout.
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await q.close(drain_timeout=0.1)
        elapsed = loop.time() - t0
        assert elapsed < 1.0, f"close() should not wait > 1s; took {elapsed:.2f}s"
        # Cleanup the still-pending send (so the test doesn't leak the task).
        send_release.set()

    @pytest.mark.anyio
    async def test_close_is_idempotent(self):
        ws, _ = _ws_with_recorder()
        q = WSSendQueue(ws)
        q.start()
        await q.close()
        await q.close()  # must not raise
        assert q.closed is True

    @pytest.mark.anyio
    async def test_enqueue_after_close_returns_false(self):
        ws, received = _ws_with_recorder()
        q = WSSendQueue(ws)
        q.start()
        await q.close()
        assert await q.enqueue({"type": "after"}) is False
        # The post-close event must not arrive at the WS.
        assert all(e.get("type") != "after" for e in received)


class TestAttachHelpers:

    @pytest.mark.anyio
    async def test_attach_starts_queue_and_stores_on_ws(self):
        ws, received = _ws_with_recorder()
        q = attach(ws, name="test")
        try:
            assert queue_for(ws) is q
            await q.enqueue({"type": "ping"})
        finally:
            await q.close()
            detach(ws)

        assert any(e.get("type") == "ping" for e in received)
        assert queue_for(ws) is None

    @pytest.mark.anyio
    async def test_queue_for_returns_none_when_not_attached(self):
        ws, _ = _ws_with_recorder()
        assert queue_for(ws) is None

    @pytest.mark.anyio
    async def test_detach_is_safe_when_not_attached(self):
        ws, _ = _ws_with_recorder()
        # Must not raise even though nothing was attached.
        detach(ws)
        assert queue_for(ws) is None
