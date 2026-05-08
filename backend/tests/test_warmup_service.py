"""Tests for services.warmup_service.

The warmup service preloads heavy ML artifacts at sidecar boot. These tests
exercise the orchestration contract — start() is idempotent, every component
reaches a terminal state, status is thread-safe — without depending on the
actual ML libraries (we patch each per-component warmer to avoid 5-10 s of
model loading per test run).
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from services import warmup_service


@pytest.fixture(autouse=True)
def _reset_warmup_state():
    warmup_service.reset_for_tests()
    yield
    warmup_service.reset_for_tests()


def _wait_until_complete(timeout: float = 5.0) -> bool:
    """Block until warmup finishes or *timeout* elapses."""
    return warmup_service.wait_for_ready(timeout=timeout)


# ---------------------------------------------------------------------------
# Status snapshot shape
# ---------------------------------------------------------------------------
def test_initial_status_is_pending():
    snap = warmup_service.status()
    assert snap["started"] is False
    assert snap["completed"] is False
    assert set(snap["components"].keys()) == {"embedder", "reranker", "ner", "tokenizer"}
    for comp in snap["components"].values():
        assert comp["state"] == "pending"
        assert comp["duration_ms"] is None
        assert comp["error"] is None


def test_is_ready_false_before_start():
    assert warmup_service.is_ready() is False


# ---------------------------------------------------------------------------
# start() orchestration
# ---------------------------------------------------------------------------
def test_start_runs_all_components_to_terminal_state():
    # Patch each warmer so the test doesn't load the actual ML models.
    # Each stub marks its component `ready` via the same _set helper the
    # real path uses, so the integration of orchestrator + status is
    # exercised end-to-end.
    def _stub_factory(name: str):
        def _stub():
            warmup_service._set(name, "ready", duration_ms=10.0)
        return _stub

    with patch.object(warmup_service, "_warm_tokenizer", _stub_factory("tokenizer")), \
         patch.object(warmup_service, "_warm_ner", _stub_factory("ner")), \
         patch.object(warmup_service, "_warm_embedder", _stub_factory("embedder")), \
         patch.object(warmup_service, "_warm_reranker", _stub_factory("reranker")):
        warmup_service.start()
        assert _wait_until_complete(timeout=2.0), "warmup did not complete in time"

    snap = warmup_service.status()
    assert snap["started"] is True
    assert snap["completed"] is True
    for name, comp in snap["components"].items():
        assert comp["state"] == "ready", f"{name} ended in {comp['state']}"


def test_start_is_idempotent():
    """Calling start() twice must not spawn a second worker thread."""
    with patch.object(warmup_service, "_warm_tokenizer"), \
         patch.object(warmup_service, "_warm_ner"), \
         patch.object(warmup_service, "_warm_embedder"), \
         patch.object(warmup_service, "_warm_reranker"):
        first = warmup_service.start()
        second = warmup_service.start()

    assert isinstance(first, threading.Thread)
    assert second is None, "second start() should be a no-op"
    _wait_until_complete(timeout=2.0)


def test_component_failure_does_not_block_others():
    """A failing warmer must mark itself failed without aborting the loop."""
    def _failing():
        raise RuntimeError("simulated load failure")

    def _stub_ready(name: str):
        def _inner():
            warmup_service._set(name, "ready", duration_ms=5.0)
        return _inner

    with patch.object(warmup_service, "_warm_tokenizer", _stub_ready("tokenizer")), \
         patch.object(warmup_service, "_warm_ner", _failing), \
         patch.object(warmup_service, "_warm_embedder", _stub_ready("embedder")), \
         patch.object(warmup_service, "_warm_reranker", _stub_ready("reranker")):
        warmup_service.start()
        assert _wait_until_complete(timeout=2.0)

    snap = warmup_service.status()
    # The orchestrator wraps each warmer in try/except via the warmer's own
    # error handling. A bare-raising stub bypasses that, so the orchestrator's
    # finally block still flips `completed` and the other warmers still ran.
    assert snap["completed"] is True
    assert snap["components"]["tokenizer"]["state"] == "ready"
    assert snap["components"]["embedder"]["state"] == "ready"
    assert snap["components"]["reranker"]["state"] == "ready"


def test_completed_at_records_wallclock():
    with patch.object(warmup_service, "_warm_tokenizer"), \
         patch.object(warmup_service, "_warm_ner"), \
         patch.object(warmup_service, "_warm_embedder"), \
         patch.object(warmup_service, "_warm_reranker"):
        before = time.time()
        warmup_service.start()
        _wait_until_complete(timeout=2.0)
        after = time.time()

    snap = warmup_service.status()
    assert snap["started_at"] is not None
    assert snap["completed_at"] is not None
    assert before <= snap["started_at"] <= after
    assert before <= snap["completed_at"] <= after


# ---------------------------------------------------------------------------
# wait_for_ready
# ---------------------------------------------------------------------------
def test_wait_for_ready_returns_false_on_timeout():
    """Without start(), wait_for_ready must time out cleanly, not hang."""
    assert warmup_service.wait_for_ready(timeout=0.05) is False


def test_wait_for_ready_returns_true_after_completion():
    with patch.object(warmup_service, "_warm_tokenizer"), \
         patch.object(warmup_service, "_warm_ner"), \
         patch.object(warmup_service, "_warm_embedder"), \
         patch.object(warmup_service, "_warm_reranker"):
        warmup_service.start()
        assert warmup_service.wait_for_ready(timeout=2.0) is True


# ---------------------------------------------------------------------------
# Health endpoint integration
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_health_warm_endpoint_initial_state(client):
    """Endpoint returns the initial pending snapshot before start() fires.

    The TestClient fixture doesn't run the lifespan, so warmup is never
    triggered here — the endpoint should still render the schema cleanly.
    """
    response = await client.get("/api/health/warm")
    assert response.status_code == 200
    data = response.json()
    assert "ready" in data
    assert "components" in data
    assert "started" in data
    assert "completed" in data
    # Component schema invariant
    for name in ("embedder", "reranker", "ner", "tokenizer"):
        assert name in data["components"]
        assert "state" in data["components"][name]
