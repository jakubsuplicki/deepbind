import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from services.system_prompt import StreamEvent

pytestmark = pytest.mark.anyio(backends=["asyncio"])


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def isolate_workspace(tmp_path, monkeypatch):
    """Prevent tests from writing session files to the real workspace."""
    settings = MagicMock()
    settings.workspace_path = tmp_path
    for mod in ["services.session_service", "services.memory_service",
                "services.graph_service", "services.context_builder",
                "services.preference_service", "services.token_tracking",
                "services.workspace_service"]:
        try:
            monkeypatch.setattr(f"{mod}.get_settings", lambda: settings)
        except AttributeError:
            pass
    for d in ["app", "app/sessions", "memory", "memory/inbox",
              "memory/preferences", "graph"]:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    # Initialise the SQLite schema so retrieval queries don't fail
    import sqlite3
    from models.database import SCHEMA_SQL, FTS_SQL, TRIGGER_SQL
    with sqlite3.connect(str(tmp_path / "app" / "jarvis.db")) as conn:
        conn.executescript(SCHEMA_SQL + FTS_SQL + TRIGGER_SQL)


def _fake_stream(*events: StreamEvent):
    """Create an AsyncMock that yields StreamEvents."""

    async def _gen(**kwargs):
        for e in events:
            yield e

    return _gen


def _fake_tool_then_text(tool_event: StreamEvent, follow_up_text: str = ""):
    """First call yields tool_use, subsequent calls yield text (for follow-up)."""
    call_count = 0

    async def _gen(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield tool_event
        elif follow_up_text:
            yield StreamEvent(type="text_delta", content=follow_up_text)

    return _gen


@pytest.fixture
def mock_api_key():
    with patch("routers.chat.get_api_key", return_value="sk-ant-test-key"):
        yield


@pytest.fixture
def mock_claude_stream():
    """Patch the chat dispatcher to return fake events.

    ADR 015: the router only constructs ``OllamaDispatcher`` now (single
    target). The fixture is named ``mock_claude_stream`` for legacy
    test-call-site compatibility but the underlying patch target is the
    Ollama dispatcher.
    """
    with patch("routers.chat.OllamaDispatcher") as mock_cls:
        instance = mock_cls.return_value
        yield instance


# --- WebSocket tests ---


@pytest.mark.anyio
async def test_ws_connect_succeeds(mock_api_key):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with ac.stream("GET", "/api/chat/ws") as _:
            pass
    # If we get here without exception, the WS route exists.
    # Full WS testing below uses starlette testclient.


@pytest.mark.anyio
async def test_ws_connect_returns_session_id(mock_api_key):
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/api/chat/ws") as ws:
            data = ws.receive_json()
            assert data["type"] == "session_start"
            assert "session_id" in data


@pytest.mark.anyio
async def test_ws_send_message_receives_chunks(mock_api_key, mock_claude_stream):
    mock_claude_stream.stream_response = _fake_stream(
        StreamEvent(type="text_delta", content="Hello "),
        StreamEvent(type="text_delta", content="world"),
    )

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/api/chat/ws") as ws:
            ws.receive_json()  # session_start
            ws.send_json({"content": "Hi"})

            chunks = []
            while True:
                msg = ws.receive_json()
                if msg["type"] == "done":
                    break
                chunks.append(msg)

            text_chunks = [c for c in chunks if c["type"] == "text_delta"]
            assert len(text_chunks) == 2
            assert text_chunks[0]["content"] == "Hello "


@pytest.mark.anyio
async def test_ws_chunks_form_complete_response(mock_api_key, mock_claude_stream):
    mock_claude_stream.stream_response = _fake_stream(
        StreamEvent(type="text_delta", content="Hello "),
        StreamEvent(type="text_delta", content="world!"),
    )

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/api/chat/ws") as ws:
            ws.receive_json()  # session_start
            ws.send_json({"content": "Hi"})

            full_text = ""
            while True:
                msg = ws.receive_json()
                if msg["type"] == "done":
                    break
                if msg["type"] == "text_delta":
                    full_text += msg["content"]

            assert full_text == "Hello world!"


@pytest.mark.anyio
async def test_ws_tool_use_event(mock_api_key, mock_claude_stream):
    mock_claude_stream.stream_response = _fake_tool_then_text(
        StreamEvent(
            type="tool_use",
            name="search_notes",
            tool_input={"query": "test"},
            tool_use_id="tool_1",
        ),
    )

    with patch("routers.chat.execute_tool", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = '[{"path": "test.md"}]'

        from starlette.testclient import TestClient

        with TestClient(app) as client:
            with client.websocket_connect("/api/chat/ws") as ws:
                ws.receive_json()  # session_start
                ws.send_json({"content": "search for test"})

                events = []
                while True:
                    msg = ws.receive_json()
                    events.append(msg)
                    if msg["type"] == "done":
                        break

                tool_events = [e for e in events if e["type"] == "tool_use"]
                assert len(tool_events) == 1
                assert tool_events[0]["name"] == "search_notes"


@pytest.mark.anyio
async def test_ws_tool_result_event(mock_api_key, mock_claude_stream):
    mock_claude_stream.stream_response = _fake_tool_then_text(
        StreamEvent(
            type="tool_use",
            name="search_notes",
            tool_input={"query": "test"},
            tool_use_id="tool_1",
        ),
    )

    with patch("routers.chat.execute_tool", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = '[{"path": "test.md"}]'

        from starlette.testclient import TestClient

        with TestClient(app) as client:
            with client.websocket_connect("/api/chat/ws") as ws:
                ws.receive_json()  # session_start
                ws.send_json({"content": "search"})

                events = []
                while True:
                    msg = ws.receive_json()
                    events.append(msg)
                    if msg["type"] == "done":
                        break

                result_events = [e for e in events if e["type"] == "tool_result"]
                assert len(result_events) == 1
                assert "test.md" in result_events[0]["content"]


@pytest.mark.anyio
async def test_ws_session_history_grows(mock_api_key, mock_claude_stream):
    call_count = 0

    async def _stream(**kwargs):
        nonlocal call_count
        call_count += 1
        yield StreamEvent(type="text_delta", content=f"Reply {call_count}")

    mock_claude_stream.stream_response = _stream

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/api/chat/ws") as ws:
            start = ws.receive_json()
            sid = start["session_id"]

            ws.send_json({"content": "first"})
            while ws.receive_json()["type"] != "done":
                pass

            ws.send_json({"content": "second"})
            while ws.receive_json()["type"] != "done":
                pass

            from services.session_service import get_messages

            msgs = get_messages(sid)
            assert len(msgs) == 4  # user, assistant, user, assistant


@pytest.mark.anyio
async def test_ws_invalid_json(mock_api_key):
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/api/chat/ws") as ws:
            ws.receive_json()  # session_start
            ws.send_text("not json at all")
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "Invalid JSON" in msg["content"]


@pytest.mark.anyio
async def test_ws_empty_message(mock_api_key):
    from starlette.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/api/chat/ws") as ws:
            ws.receive_json()  # session_start
            ws.send_json({"content": ""})
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "required" in msg["content"].lower()


@pytest.mark.anyio
async def test_ws_disconnect_cleanup(mock_api_key, mock_claude_stream):
    mock_claude_stream.stream_response = _fake_stream(
        StreamEvent(type="text_delta", content="hi"),
    )

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/api/chat/ws") as ws:
            start = ws.receive_json()
            sid = start["session_id"]

    # After disconnect, session is kept for potential resume (not deleted)
    from services.session_service import get_session

    session = get_session(sid)
    assert session is not None
    assert session["id"] == sid


@pytest.mark.anyio
async def test_ws_uses_client_api_key_when_provided(mock_claude_stream):
    """When the WS message includes api_key, use it instead of server-stored key."""
    mock_claude_stream.stream_response = _fake_stream(
        StreamEvent(type="text_delta", content="OK"),
    )

    # No server-side key configured — get_api_key returns None
    with patch("routers.chat.get_api_key", return_value=None):
        from starlette.testclient import TestClient

        with TestClient(app) as client:
            with client.websocket_connect("/api/chat/ws") as ws:
                ws.receive_json()  # session_start
                ws.send_json({
                    "content": "Hello",
                    "api_key": "sk-ant-client-key-123",
                    "provider": "anthropic",
                })

                events = []
                while True:
                    msg = ws.receive_json()
                    events.append(msg)
                    if msg["type"] == "done":
                        break

                # Should have received text, not an error about missing API key
                text_events = [e for e in events if e["type"] == "text_delta"]
                assert len(text_events) >= 1
                assert text_events[0]["content"] == "OK"


@pytest.mark.anyio
async def test_ws_falls_back_to_server_key_without_client_key(mock_api_key, mock_claude_stream):
    """When no api_key in WS message, fall back to server-stored key."""
    mock_claude_stream.stream_response = _fake_stream(
        StreamEvent(type="text_delta", content="Fallback"),
    )

    from starlette.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/api/chat/ws") as ws:
            ws.receive_json()  # session_start
            ws.send_json({"content": "Hello"})  # no api_key

            events = []
            while True:
                msg = ws.receive_json()
                events.append(msg)
                if msg["type"] == "done":
                    break

            text_events = [e for e in events if e["type"] == "text_delta"]
            assert len(text_events) >= 1
            assert text_events[0]["content"] == "Fallback"


# ── WS send queue integration (the lock-decoupling fix) ─────────────────────
#
# These tests pin the property the chat router depends on: with the per-WS
# send queue attached (production path), `_send_event(ws, ...)` returns
# promptly even when `ws.send_json` is blocked. Without that property, a
# napped WKWebView between turns holds the per-session dispatch lock for
# 20-30s, making the user's next message wait through the previous turn's
# trailing `done` send.


@pytest.mark.anyio
async def test_send_event_routes_through_queue_when_attached():
    """With `attach()` on the WS, `_send_event` must enqueue events for
    the consumer task to drain — not call `ws.send_json` from the caller
    side."""
    import asyncio as _asyncio
    from routers.chat import _send_event
    from services.ws_send_queue import attach, detach

    received: list[dict] = []

    async def _send_json(payload):
        received.append(payload)

    ws = type("FakeWS", (), {})()
    ws.send_json = _send_json  # type: ignore[attr-defined]
    bus = attach(ws, name="route-test")
    try:
        await _send_event(ws, "evt", n=1)
        await _send_event(ws, "evt", n=2)
        await _send_event(ws, "done", final=True)
        # Wait for the consumer to drain.
        for _ in range(50):
            if len(received) >= 3:
                break
            await _asyncio.sleep(0.01)
        assert [e["type"] for e in received] == ["evt", "evt", "done"]
        assert [e.get("n") for e in received[:2]] == [1, 2]
    finally:
        await bus.close()
        detach(ws)


@pytest.mark.anyio
async def test_send_event_does_not_block_on_stuck_ws_when_queue_attached():
    """**The load-bearing test.** With the queue attached, a wedged
    `ws.send_json` (e.g. WKWebView App Nap) must not block subsequent
    `_send_event` calls. Without the queue, every call would block
    until the wedge clears — which is exactly the dispatch-lock-coupling
    bug the queue exists to fix.

    Asserts that 5 follow-up sends complete in well under 1s while the
    first send is held by an asyncio.Event the consumer is awaiting."""
    import asyncio as _asyncio
    from routers.chat import _send_event
    from services.ws_send_queue import attach, detach

    send_started = _asyncio.Event()
    send_release = _asyncio.Event()

    async def _send_json(payload):
        send_started.set()
        await send_release.wait()

    ws = type("FakeWS", (), {})()
    ws.send_json = _send_json  # type: ignore[attr-defined]
    bus = attach(ws, name="stuck-test")
    try:
        # First event: consumer pulls it and gets stuck inside send_json.
        await _send_event(ws, "first")
        await _asyncio.wait_for(send_started.wait(), timeout=1.0)

        # Subsequent _send_events MUST return promptly. With the queue
        # they enqueue and return; without the queue they would block
        # until send_release is set.
        loop = _asyncio.get_event_loop()
        t0 = loop.time()
        for i in range(5):
            await _asyncio.wait_for(
                _send_event(ws, "follow_up", i=i),
                timeout=0.5,
            )
        elapsed = loop.time() - t0
        assert elapsed < 0.5, (
            f"5 _send_event calls should not couple to stuck send_json; "
            f"elapsed={elapsed:.3f}s"
        )
    finally:
        send_release.set()
        await bus.close()
        detach(ws)


@pytest.mark.anyio
async def test_send_event_falls_back_to_direct_send_without_queue():
    """Backwards-compatibility: tests / fixtures that don't call attach()
    keep working. `_send_event` falls through to `ws.send_json` directly."""
    from routers.chat import _send_event

    received: list[dict] = []

    async def _send_json(payload):
        received.append(payload)

    ws = type("FakeWS", (), {})()
    ws.send_json = _send_json  # type: ignore[attr-defined]
    # No attach() — simulating a test/dev path that constructs a bare WS.
    await _send_event(ws, "ping", x=42)
    assert received == [{"type": "ping", "x": 42}]


@pytest.mark.anyio
async def test_ws_handler_attaches_and_detaches_send_queue(mock_api_key, mock_claude_stream):
    """End-to-end: connecting a WS attaches a queue; disconnecting
    detaches it. Verified by spying on the attach/detach helpers."""
    from unittest.mock import patch

    mock_claude_stream.stream_response = _fake_stream(
        StreamEvent(type="text_delta", content="hello"),
    )

    attach_calls: list = []
    detach_calls: list = []

    real_attach = __import__("services.ws_send_queue", fromlist=["attach"]).attach
    real_detach = __import__("services.ws_send_queue", fromlist=["detach"]).detach

    def _spy_attach(ws, **kw):
        attach_calls.append(ws)
        return real_attach(ws, **kw)

    def _spy_detach(ws):
        detach_calls.append(ws)
        return real_detach(ws)

    from starlette.testclient import TestClient

    with patch("routers.chat._attach_send_queue", _spy_attach), \
         patch("routers.chat._detach_send_queue", _spy_detach):
        with TestClient(app) as client:
            with client.websocket_connect("/api/chat/ws") as ws:
                ws.receive_json()  # session_start
                ws.send_json({"content": "hi"})
                while ws.receive_json()["type"] != "done":
                    pass

    assert len(attach_calls) == 1, "attach should fire once on WS accept"
    assert len(detach_calls) == 1, "detach should fire once on WS close"
    assert attach_calls[0] is detach_calls[0], "same WS instance attached + detached"
