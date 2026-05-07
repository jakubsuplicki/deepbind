"""Per-WebSocket outbound send serialization.

Decouples backend chat dispatch from WebSocket write back-pressure so the
per-session `asyncio.Lock` (which exists to keep two concurrent
`_handle_message` runs from racing the same LLM) can't be held hostage by
WS-flush latency.

## The bug this exists to fix

Without this, `await ws.send_json(...)` is called from inside the
per-session lock. On macOS WKWebView, App Nap can pause the JS-side WS
read pump after the user's UI goes idle (typically between turns: the
final `done` event fires, isLoading flips false, the page becomes
quiescent, and WKWebView naps the JSContext). The TCP receive buffer on
the client fills, kernel back-pressure stops accepting more outbound
data on the server, and the server's `await ws.send_json(...)` for the
*previous* turn's trailing `done` event blocks indefinitely. The lock
stays held the whole time.

When the user types their next message, WKWebView wakes up to handle
the keyboard event, drains the buffered frames, the server's `await`
unblocks, the lock releases, and the queued next-turn dispatch finally
runs — having waited 20-30s for what looked like "the model" but was
really just the previous turn's lock-hold. ADR 016 tried to fix this
by routing the user's *send* off the WebSocket onto HTTP+IPC; that
helped a different failure mode but didn't address the lock-wait
because the bottleneck is on the *receive* direction back into the
WebView.

## Design

- One `asyncio.Queue` per WebSocket. Producer-side `enqueue()` is fast
  (a `put`, optionally bounded). The dispatch lock is no longer
  coupled to WS write latency.
- Single consumer task per WebSocket drains the queue in FIFO order
  and writes to the WS. Single-consumer is what preserves frame
  ordering across turns — `text_delta` for turn 1 always arrives
  before `done` for turn 1 always arrives before `text_delta` for
  turn 2.
- Bounded queue prevents unbounded memory growth when the consumer is
  permanently stuck on a dead client. `enqueue()` blocks (with a
  timeout) when full so an embarrassed dispatcher can give up rather
  than wait forever.
- Fail-latching: once the consumer's `ws.send_json` raises (typically
  because the WS is being torn down), the queue marks itself as
  failed and all subsequent enqueues fast-return False. The dispatch
  layer doesn't have to handle the fallout — the WebSocket lifecycle
  will close on its own.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


# Sentinel value used to wake the consumer for a clean shutdown without
# resorting to task cancellation (which is harder to reason about and
# leaves the queue in an indeterminate state). Identity comparison only.
_CLOSE_SENTINEL: dict = {"__ws_send_queue_close__": True}


class WSSendQueue:
    """Outbound send serializer for one WebSocket.

    Lifecycle:
      1. `WSSendQueue(ws)` — construct (does not start anything).
      2. `start()` — spawn the consumer task. Idempotent.
      3. `await enqueue(event)` — hand off an event for sending.
      4. `await close()` — stop accepting new events; wait for the
         consumer to drain (with a timeout) and then exit.

    Properties:
      - FIFO ordering across all `enqueue()` calls.
      - `enqueue()` returns True/False; False means dropped (queue
        closed, consumer failed, or enqueue timed out on a full queue).
      - After `close()`, `enqueue()` is a no-op returning False.
    """

    __slots__ = (
        "_ws", "_maxsize", "_enqueue_timeout", "_name",
        "_queue", "_consumer", "_failed", "_closed",
    )

    def __init__(
        self,
        websocket: WebSocket,
        *,
        maxsize: int = 256,
        enqueue_timeout: float = 2.0,
        name: str = "",
    ) -> None:
        self._ws = websocket
        self._maxsize = maxsize
        self._enqueue_timeout = enqueue_timeout
        self._name = name or hex(id(websocket))[-8:]
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)
        self._consumer: Optional[asyncio.Task[None]] = None
        self._failed: bool = False
        self._closed: bool = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the consumer task. Idempotent."""
        if self._consumer is None:
            self._consumer = asyncio.create_task(
                self._drain(),
                name=f"ws-send-queue-{self._name}",
            )

    async def close(self, *, drain_timeout: float = 1.0) -> None:
        """Stop accepting new events; wait for the consumer to finish.

        After `close()` returns, `enqueue()` is a no-op returning False.
        Events still on the queue are best-effort: the consumer keeps
        sending until it hits the close sentinel. If it's stuck inside
        `ws.send_json` for a dead client, we give up after
        `drain_timeout` and cancel.
        """
        if self._closed:
            return
        self._closed = True

        # Push the sentinel so the consumer wakes from `queue.get()` and
        # exits. If the queue is full, drop one item to make room for
        # the sentinel — losing one trailing event on a stuck consumer
        # is better than waiting forever.
        try:
            self._queue.put_nowait(_CLOSE_SENTINEL)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(_CLOSE_SENTINEL)
            except asyncio.QueueFull:
                pass

        consumer = self._consumer
        if consumer is None:
            return
        try:
            await asyncio.wait_for(consumer, timeout=drain_timeout)
        except asyncio.TimeoutError:
            consumer.cancel()
            try:
                await consumer
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            self._consumer = None

    # ── Producer side ────────────────────────────────────────────────────

    async def enqueue(self, event: dict) -> bool:
        """Hand off an event for the consumer to send.

        Returns True if accepted, False if the queue is closed/failed
        or the put timed out (consumer stuck on a dead client).

        Never raises — the dispatch layer treats WS sends as
        best-effort, and if the WS is dying the receive loop will see
        WebSocketDisconnect and tear down on its own.
        """
        if self._closed or self._failed:
            return False
        try:
            await asyncio.wait_for(
                self._queue.put(event),
                timeout=self._enqueue_timeout,
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "ws-send-queue %s full for %.1fs (consumer stuck) — dropping event %s",
                self._name, self._enqueue_timeout, event.get("type", "?"),
            )
            return False

    # ── Consumer ─────────────────────────────────────────────────────────

    async def _drain(self) -> None:
        try:
            while True:
                event = await self._queue.get()
                if event is _CLOSE_SENTINEL:
                    return
                try:
                    await self._ws.send_json(event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._failed = True
                    logger.warning(
                        "ws-send-queue %s send failed (%s) — latching",
                        self._name, exc,
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ws-send-queue %s consumer crashed", self._name)
            self._failed = True

    # ── Introspection ────────────────────────────────────────────────────

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def qsize(self) -> int:
        return self._queue.qsize()


# ── Helpers for callers (chat router) ───────────────────────────────────


_QUEUE_ATTR = "_jarvis_send_queue"


def attach(websocket: WebSocket, **kwargs: Any) -> WSSendQueue:
    """Construct and start a WSSendQueue for the given WebSocket.

    Stores the queue on the WebSocket as an attribute so `_send_event`
    can find it without threading the queue through every call site.
    Returns the queue so the caller can `await close()` on disconnect.
    """
    queue = WSSendQueue(websocket, **kwargs)
    setattr(websocket, _QUEUE_ATTR, queue)
    queue.start()
    return queue


def queue_for(websocket: WebSocket) -> Optional[WSSendQueue]:
    """Return the attached queue, or None if none is set."""
    return getattr(websocket, _QUEUE_ATTR, None)


def detach(websocket: WebSocket) -> None:
    """Remove the queue attribute. Called from the WS handler's finally."""
    try:
        delattr(websocket, _QUEUE_ATTR)
    except AttributeError:
        pass
