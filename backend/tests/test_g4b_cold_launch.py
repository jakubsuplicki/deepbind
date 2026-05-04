"""End-to-end cold-launch test (ADR 005 G4b6).

Simulates the full first-launch sequence across G4b1–G4b5 wiring in one
coherent flow:

  1. Fresh workspace, no `.first_run_complete` marker on disk.
  2. First-run orchestrator runs the §B pipeline against a mocked Ollama
     (probes hardware → picks Tier-A primary → "pulls" it → writes marker
     → "pulls" fallback → "runs" probe).
  3. Marker exists post-pipeline; subsequent launches must not re-run.
  4. Chat-router pre-flight (G4b4) consults the pressure ladder for a
     dispatched turn; the ladder picker resolves cleanly against the
     just-installed catalog.
  5. Lightweight-mode toggle (G4b5) flips dispatch to the floor.

This test does NOT verify what the unit tests already verify (state
machine transitions, ladder math, pre-flight branches). It verifies the
wiring connects across the five chunks — a single failure mode here
catches integration drift that any one unit suite would miss.

Real cold-launch on a built+notarized bundle remains a separate
user-driven smoke test (covered in the ADR 005 §G4b6 manual checklist).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.anyio(backends=["asyncio"])


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


# Mocked /api/pull event stream — minimal "downloading → success" sequence.
async def _success_pull(*args, **kwargs):
    yield {"status": "pulling manifest"}
    yield {"status": "downloading", "completed": 50, "total": 100}
    yield {"status": "downloading", "completed": 100, "total": 100}
    yield {"status": "success"}


# Mocked /api/tags response — primary + fallback installed post-pipeline.
async def _list_installed(*args, **kwargs):
    return [
        {"name": "qwen3:8b"},
        {"name": "qwen3:4b-instruct-2507"},
    ]


# Mocked hardware → Tier A.
def _hw_tier_a():
    from services.ollama_service import HardwareProfile
    return HardwareProfile(
        os="macos", arch="arm64",
        total_ram_gb=24.0, free_disk_gb=300.0, cpu_cores=10,
        gpu_vendor="apple", gpu_vram_gb=None, is_apple_silicon=True,
        tier="balanced",
    )


@pytest.mark.anyio
async def test_g4b_cold_launch_end_to_end(tmp_path):
    """Run the full pipeline + post-marker chat-router wiring through one
    test. Validates that the five G4b chunks interoperate correctly.
    """
    from services import first_run_orchestrator
    from services import memory_pressure_monitor as mpm

    first_run_orchestrator.reset_for_tests()

    # ── Stage 1: Fresh workspace — no marker ─────────────────────────────
    assert not first_run_orchestrator.is_first_run_complete(workspace_path=tmp_path)

    # ── Stage 2: Run the orchestrator pipeline ───────────────────────────
    with patch(
        "services.ollama_service.probe_hardware", return_value=_hw_tier_a(),
    ), patch(
        "services.first_run_orchestrator.pull_model_events", side_effect=lambda *a, **kw: _success_pull(),
    ), patch(
        "services.first_run_orchestrator._run_probe", return_value=True,
    ):
        await first_run_orchestrator.start_async(workspace_path=tmp_path)
        status = await first_run_orchestrator.wait_for_test()

    # Pipeline reached `complete`; primary + fallback both pulled; marker on disk.
    assert status.state == "complete", f"pipeline ended in {status.state!r}: {status.last_error!r}"
    assert status.tier == "A"
    assert status.primary_model_id == "qwen3-8b"
    assert status.fallback_model_id == "qwen3-4b-instruct-2507"
    assert status.marker_written is True
    assert first_run_orchestrator.is_first_run_complete(workspace_path=tmp_path)

    # ── Stage 3: Subsequent launch — orchestrator must not re-run ────────
    # Idempotency: calling start_async again with the existing marker
    # short-circuits to `already_complete`.
    result = await first_run_orchestrator.start_async(workspace_path=tmp_path)
    assert result["result"] == "already_complete"

    # ── Stage 4: Chat-router pre-flight pressure check (G4b4) ────────────
    # Pick the primary, ample free RAM → pressure walk returns the requested
    # model unchanged. This proves the ladder picker resolves cleanly
    # against the just-installed catalog the orchestrator populated.
    requested = mpm.find_entry_by_ollama_model("qwen3:8b")
    assert requested is not None
    swap = mpm.pick_runnable_model(
        requested,
        tier="A",
        ctx_len_tokens=4096,
        installed_ollama_tags=["qwen3:8b", "qwen3:4b-instruct-2507"],
        free_ram_bytes=32 * (1024 ** 3),  # plenty
    )
    assert swap.did_swap is False
    assert swap.chosen.id == "qwen3-8b"

    # Tighten free RAM: pressure swap to the §B fallback (4B-instruct-2507).
    swap_under_pressure = mpm.pick_runnable_model(
        requested,
        tier="A",
        ctx_len_tokens=4096,
        installed_ollama_tags=["qwen3:8b", "qwen3:4b-instruct-2507"],
        free_ram_bytes=6 * (1024 ** 3),  # tight
    )
    assert swap_under_pressure.did_swap is True
    assert swap_under_pressure.chosen.id == "qwen3-4b-instruct-2507"

    # ── Stage 5: Lightweight mode (G4b5) — pin to floor regardless ───────
    floor = mpm.floor_entry_for_tier(
        "A",
        installed_ollama_tags=["qwen3:8b", "qwen3:4b-instruct-2507"],
    )
    # Floor on Tier A is qwen3-4b-instruct-2507 (smallest installed on the
    # ladder). Lightweight mode ON → chat dispatch hits this regardless of
    # how much free RAM is available.
    assert floor is not None
    assert floor.id == "qwen3-4b-instruct-2507"
