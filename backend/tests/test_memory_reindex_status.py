"""Router-level test for the reindex status endpoint (G5 / ADR 003 §I)."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from services import reindex_supervisor


pytestmark = pytest.mark.anyio(backends=["asyncio"])


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def _reset_supervisor():
    reindex_supervisor.reset_for_tests()
    yield
    reindex_supervisor.reset_for_tests()


@pytest.mark.anyio
async def test_status_idle_at_rest():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/memory/reindex/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "idle"
    assert body["scanned"] == 0
    assert body["total"] == 0
    assert body["last_error"] is None
    assert body["progress_pct"] == 0.0
    # Schema completeness — fields the frontend toast depends on
    assert set(body.keys()) >= {
        "state",
        "started_at",
        "finished_at",
        "scanned",
        "total",
        "progress_pct",
        "last_error",
        "last_run_count",
    }


@pytest.mark.anyio
async def test_status_reflects_running_state():
    """While the supervisor reports running, the endpoint must surface it."""
    # Manually flip state — we don't want this test depending on a real
    # fastembed pass (that's covered in supervisor tests).
    reindex_supervisor._status.state = "running"
    reindex_supervisor._status.scanned = 4
    reindex_supervisor._status.total = 10

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/memory/reindex/status")
    body = resp.json()
    assert body["state"] == "running"
    assert body["scanned"] == 4
    assert body["total"] == 10
    assert body["progress_pct"] == 40.0


@pytest.mark.anyio
async def test_reindex_embeddings_returns_started_status():
    """POST /api/memory/reindex-embeddings now returns the supervisor verdict
    instead of blocking on the embedding pass."""
    from unittest.mock import patch

    async def noop(*_a, **_kw):
        return 0

    with (
        patch("services.embedding_service.is_available", return_value=True),
        patch.object(reindex_supervisor, "_embed_pass", noop),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/memory/reindex-embeddings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "started"
        # Drain the task so it doesn't leak into other tests.
        await reindex_supervisor.wait_for_test()


@pytest.mark.anyio
async def test_reindex_embeddings_returns_503_when_unavailable():
    from unittest.mock import patch

    with patch("services.embedding_service.is_available", return_value=False):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/memory/reindex-embeddings")
    assert resp.status_code == 503
