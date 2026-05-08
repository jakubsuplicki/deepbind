"""Sidecar warmup — preload heavy ML components at startup.

The chat hot path lazy-loads several large ONNX / spaCy / HuggingFace
artifacts the *first* time they are touched:

  * fastembed embedder      (~2-4 s, ~1 GB RAM)   — services/embedding_service.py
  * fastembed reranker      (~3-8 s, ~600 MB RAM) — services/reranker_service.py
  * spaCy NER pipeline      (~1-3 s)              — services/entity_extraction.py
  * HuggingFace tokenizers  (~50-200 ms)          — services/token_counting.py

Without warmup, those costs land on a user-facing turn — typically turn 2 of
a cold app start, since turn 1 has empty history (so retrieval may short-
circuit) and turn 1's `_save_session_bg` is the *first* call site to touch
spaCy. The next turn then both runs the retrieval pipeline (lazy-loads
embedder + reranker) *and* contends with the still-loading spaCy import on
the same event loop — producing the 20-30 s wall-clock stall users see.

This service exercises each component once on dummy input at sidecar boot.
It runs as a fire-and-forget background task in the FastAPI lifespan, so
startup is non-blocking. Status is surfaced through `/api/health/warm` for
the frontend, which can soften the "first message takes a moment" experience
into something honest rather than silent latency.

Design notes
------------
- Components warmed **sequentially** in a single worker thread. Parallel
  ONNX loads contend for the same CPU + memory bandwidth; sequential is
  predictable and only ~10-15 s total on M-series hardware.
- All work runs in a `threading.Thread`, never on the event loop. The
  FastAPI lifespan only fires the thread; it does not await completion.
- Status snapshot is thread-safe and immutable from the caller's view.
- Failures are non-fatal: a component that fails to warm is logged and
  marked `failed`. The chat hot path will still try to load it on demand
  and degrade per its own contract (e.g., reranker → fusion-only retrieval).
- Idempotent: `start()` is a no-op once warmup has begun.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status state
# ---------------------------------------------------------------------------
# Each component progresses pending → running → ready / failed / skipped.
# `skipped` is used when a component is intentionally disabled (e.g., reranker
# off via JARVIS_DISABLE_RERANKER=1) — it is not an error.
_COMPONENTS = ("embedder", "reranker", "ner", "tokenizer")
_TERMINAL_STATES = {"ready", "failed", "skipped"}

_lock = threading.Lock()
_status: Dict[str, dict] = {
    name: {"state": "pending", "duration_ms": None, "error": None}
    for name in _COMPONENTS
}
_started: bool = False
_completed: bool = False
_started_at: Optional[float] = None
_completed_at: Optional[float] = None
_thread: Optional[threading.Thread] = None
_done_event = threading.Event()


def _set(component: str, state: str, *, duration_ms: Optional[float] = None, error: Optional[str] = None) -> None:
    with _lock:
        _status[component]["state"] = state
        if duration_ms is not None:
            _status[component]["duration_ms"] = round(duration_ms, 1)
        if error is not None:
            _status[component]["error"] = error


def status() -> dict:
    """Snapshot of warmup progress. Safe to call from any thread."""
    with _lock:
        return {
            "started": _started,
            "completed": _completed,
            "started_at": _started_at,
            "completed_at": _completed_at,
            "components": {k: dict(v) for k, v in _status.items()},
        }


def is_ready() -> bool:
    """True iff every component has reached a terminal state."""
    with _lock:
        return _completed


def wait_for_ready(timeout: Optional[float] = None) -> bool:
    """Block until warmup completes or *timeout* elapses. Returns readiness."""
    return _done_event.wait(timeout=timeout)


# ---------------------------------------------------------------------------
# Per-component warmup
# ---------------------------------------------------------------------------
def _warm_embedder() -> None:
    _set("embedder", "running")
    t0 = time.monotonic()
    try:
        from services import embedding_service
        # `embed_query` exercises both model load AND query-prefix path —
        # the actual code path used in retrieval. A single 1-token call
        # is enough; ONNX warmup happens on first `model.embed([...])`.
        embedding_service.embed_query("warmup")
        elapsed = (time.monotonic() - t0) * 1000
        _set("embedder", "ready", duration_ms=elapsed)
        logger.info("Warmup: embedder ready (%.0f ms)", elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.monotonic() - t0) * 1000
        _set("embedder", "failed", duration_ms=elapsed, error=repr(exc))
        logger.warning("Warmup: embedder failed after %.0f ms: %s", elapsed, exc)


def _warm_reranker() -> None:
    _set("reranker", "running")
    t0 = time.monotonic()
    try:
        from services import reranker_service
        if reranker_service.is_disabled():
            elapsed = (time.monotonic() - t0) * 1000
            _set("reranker", "skipped", duration_ms=elapsed)
            logger.info("Warmup: reranker skipped (JARVIS_DISABLE_RERANKER=1)")
            return
        if not reranker_service.is_available():
            elapsed = (time.monotonic() - t0) * 1000
            _set("reranker", "skipped", duration_ms=elapsed)
            logger.info("Warmup: reranker skipped (fastembed cross-encoder not importable)")
            return
        # Two-doc rerank exercises tokenizer + cross-encoder forward pass.
        # The reranker degrades to None on internal failure; that's still
        # a successful warmup of the import + model-load path.
        reranker_service.rerank("warmup query", ["dummy document one", "dummy document two"])
        elapsed = (time.monotonic() - t0) * 1000
        _set("reranker", "ready", duration_ms=elapsed)
        logger.info("Warmup: reranker ready (%.0f ms)", elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.monotonic() - t0) * 1000
        _set("reranker", "failed", duration_ms=elapsed, error=repr(exc))
        logger.warning("Warmup: reranker failed after %.0f ms: %s", elapsed, exc)


def _warm_ner() -> None:
    _set("ner", "running")
    t0 = time.monotonic()
    try:
        from services import entity_extraction
        # Realistic-ish text so spaCy's pipeline (tokenizer + tagger + NER)
        # all execute, not just the model load.
        entity_extraction.extract_entities("Warmup note: John Smith met with ACME Corp on 2026-01-15.")
        elapsed = (time.monotonic() - t0) * 1000
        _set("ner", "ready", duration_ms=elapsed)
        logger.info("Warmup: NER ready (%.0f ms)", elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.monotonic() - t0) * 1000
        _set("ner", "failed", duration_ms=elapsed, error=repr(exc))
        logger.warning("Warmup: NER failed after %.0f ms: %s", elapsed, exc)


def _warm_tokenizer() -> None:
    _set("tokenizer", "running")
    t0 = time.monotonic()
    try:
        from services import token_counting
        # Importing `tokenizers` (Rust extension) is the dominant cost; loading
        # any specific tokenizer.json after that is ~10-50 ms. Walk the bundled
        # allowlist and prime each — small, and avoids per-model cold-start
        # penalties when users switch chat models mid-session. Failures (e.g.,
        # tokenizers package stripped) downgrade silently to char/4 elsewhere
        # so the warmup just records the load attempt.
        primed = 0
        # Prefer the bundled allowlist directly; if it's somehow unavailable,
        # fall back to a single representative id.
        ids = getattr(token_counting, "_BUNDLED_TOKENIZER_IDS", frozenset({"Qwen/Qwen3-8B"}))
        for tid in ids:
            if token_counting.get_tokenizer(tid) is not None:
                primed += 1
        elapsed = (time.monotonic() - t0) * 1000
        if primed == 0:
            # Allowlist intact but every load returned None — most likely the
            # tokenizers package itself is unavailable. Mark `skipped` rather
            # than `failed`: the chat path's char/4 fallback is the documented
            # contract, not an error.
            _set("tokenizer", "skipped", duration_ms=elapsed)
            logger.info("Warmup: tokenizer skipped (no bundled tokenizers loadable)")
        else:
            _set("tokenizer", "ready", duration_ms=elapsed)
            logger.info("Warmup: tokenizer ready, %d primed (%.0f ms)", primed, elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.monotonic() - t0) * 1000
        _set("tokenizer", "failed", duration_ms=elapsed, error=repr(exc))
        logger.warning("Warmup: tokenizer failed after %.0f ms: %s", elapsed, exc)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _run_all() -> None:
    """Sequential warmup loop. Runs in a worker thread.

    Each per-component warmer is invoked under its own try/except so one
    rogue warmer (e.g., a programming bug in a future component) can't
    abort the whole loop. The real warmers above already swallow their own
    failures; this is belt-and-braces so the orchestrator's contract holds
    even if a future warmer is added without that wrapper.
    """
    global _completed, _completed_at
    overall_t0 = time.monotonic()
    # Order matters: tokenizer first (fast, primes the `tokenizers` Rust
    # extension), then NER (smallest ML model), then embedder, then
    # reranker (largest). If the user happens to dispatch a chat turn
    # mid-warmup, the most-likely-needed components finish first.
    warmers = (
        ("tokenizer", _warm_tokenizer),
        ("ner",       _warm_ner),
        ("embedder",  _warm_embedder),
        ("reranker",  _warm_reranker),
    )
    try:
        for name, warmer in warmers:
            try:
                warmer()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Warmup: %s warmer raised unexpectedly: %s", name, exc)
                _set(name, "failed", error=repr(exc))
    finally:
        with _lock:
            _completed = True
            _completed_at = time.time()
        elapsed = (time.monotonic() - overall_t0) * 1000
        logger.info("Warmup: completed in %.0f ms total", elapsed)
        _done_event.set()


def start() -> Optional[threading.Thread]:
    """Begin warmup in a background thread. Idempotent.

    Returns the worker thread (for tests / lifespan join), or None if warmup
    was already started — callers should treat that as a no-op.
    """
    global _started, _started_at, _thread
    with _lock:
        if _started:
            return None
        _started = True
        _started_at = time.time()

    thread = threading.Thread(
        target=_run_all,
        name="jarvis-warmup",
        daemon=True,
    )
    _thread = thread
    thread.start()
    logger.info("Warmup: started background thread")
    return thread


def reset_for_tests() -> None:
    """Reset all module state. Test-only — production has no reason to call."""
    global _started, _completed, _started_at, _completed_at, _thread
    with _lock:
        _started = False
        _completed = False
        _started_at = None
        _completed_at = None
        _thread = None
        for name in _COMPONENTS:
            _status[name] = {"state": "pending", "duration_ms": None, "error": None}
    _done_event.clear()
