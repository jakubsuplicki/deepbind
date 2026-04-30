"""Tests for services/reindex_supervisor.py (ADR 003 §I).

The supervisor wraps the embedding-reindex pass behind a state machine so the
FastAPI lifespan can fire-and-forget on cold start. These tests stub out the
actual fastembed call (it's gated behind ``is_available()`` and produces real
ONNX inferences when the model is installed) so we exercise the supervisor's
state transitions in isolation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from services import reindex_supervisor


pytestmark = pytest.mark.anyio(backends=["asyncio"])


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture(autouse=True)
def _reset_supervisor():
    """Each test gets a clean module-level state."""
    reindex_supervisor.reset_for_tests()
    yield
    reindex_supervisor.reset_for_tests()


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "memory").mkdir()
    (tmp_path / "app").mkdir()
    return tmp_path


@pytest.mark.anyio
async def test_idle_at_rest():
    s = reindex_supervisor.current_status()
    assert s.state == "idle"
    assert s.scanned == 0
    assert s.total == 0
    assert s.last_error is None
    assert reindex_supervisor.is_running() is False


@pytest.mark.anyio
async def test_start_returns_idle_when_fastembed_unavailable(ws: Path):
    """is_available() == False means the supervisor records a no-op pass and
    flips back to idle without recording an error."""
    with patch("services.embedding_service.is_available", return_value=False):
        result = await reindex_supervisor.start_async(workspace_path=ws)
    assert result == "started"
    s = await reindex_supervisor.wait_for_test()
    assert s.state == "idle"
    assert s.last_error is None
    assert s.last_run_count == 0


@pytest.mark.anyio
async def test_start_is_single_flight(ws: Path):
    """A second start_async() while a job is running must return
    ``already_running`` and must NOT spawn a second task."""
    started_event = asyncio.Event()
    release_event = asyncio.Event()

    async def slow_pass(*_args, **_kwargs):
        started_event.set()
        await release_event.wait()

    with patch.object(reindex_supervisor, "_embed_pass", slow_pass):
        first = await reindex_supervisor.start_async(workspace_path=ws)
        assert first == "started"
        await started_event.wait()
        # In flight — second kick should be a no-op.
        second = await reindex_supervisor.start_async(workspace_path=ws)
        assert second == "already_running"
        assert reindex_supervisor.is_running() is True
        # Let the first job finish.
        release_event.set()
        await reindex_supervisor.wait_for_test()
    assert reindex_supervisor.current_status().state == "idle"


@pytest.mark.anyio
async def test_failure_is_recorded(ws: Path):
    """An unexpected exception inside _embed_pass surfaces as state=failed
    + populated last_error, not a crashed event loop."""

    async def boom(*_args, **_kwargs):
        raise RuntimeError("simulated reindex blowup")

    with patch.object(reindex_supervisor, "_embed_pass", boom):
        await reindex_supervisor.start_async(workspace_path=ws)
        s = await reindex_supervisor.wait_for_test()
    assert s.state == "failed"
    assert s.last_error is not None
    assert "simulated reindex blowup" in s.last_error


@pytest.mark.anyio
async def test_progress_pct():
    """progress_pct snapshots correctly across the running/idle boundary."""
    s = reindex_supervisor.ReindexStatus(state="running", scanned=3, total=10)
    assert s.progress_pct == 30.0

    # idle + total=0 -> 0
    s2 = reindex_supervisor.ReindexStatus(state="idle", scanned=0, total=0)
    assert s2.progress_pct == 0.0

    # running + total=0 (very-cold-start race) -> reports 100 so the toast
    # collapses immediately rather than spinning forever
    s3 = reindex_supervisor.ReindexStatus(state="running", scanned=0, total=0)
    assert s3.progress_pct == 100.0


@pytest.mark.anyio
async def test_cancel_and_wait(ws: Path):
    """cancel_and_wait() must stop an in-flight task without leaking."""
    release_event = asyncio.Event()

    async def slow_pass(*_args, **_kwargs):
        try:
            await release_event.wait()
        except asyncio.CancelledError:
            raise

    with patch.object(reindex_supervisor, "_embed_pass", slow_pass):
        await reindex_supervisor.start_async(workspace_path=ws)
        assert reindex_supervisor.is_running() is True
        await reindex_supervisor.cancel_and_wait()
    s = reindex_supervisor.current_status()
    assert s.state == "idle"


@pytest.mark.anyio
async def test_to_dict_shape():
    """to_dict() output matches the API response schema fields."""
    s = reindex_supervisor.ReindexStatus(
        state="running",
        started_at=1.0,
        scanned=7,
        total=10,
        last_run_count=0,
    )
    d = s.to_dict()
    assert set(d.keys()) == {
        "state",
        "started_at",
        "finished_at",
        "scanned",
        "total",
        "progress_pct",
        "last_error",
        "last_run_count",
    }
    assert d["state"] == "running"
    assert d["progress_pct"] == 70.0
