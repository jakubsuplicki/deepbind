"""Tests for the first-run pull orchestrator (ADR 005 §B).

Covers the pipeline state machine — probing → pulling_primary → marker write
→ pulling_fallback → running_probe → complete — plus the skip path, the
already-running idempotency guard, the marker-already-present early-exit,
and the non-fatal fallback / probe failure paths.

The pull layer is mocked at `pull_model_events` so tests don't depend on a
running Ollama; the chat-model-probe is mocked at `iter_probe_events` for
the same reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state():
    """Wipe orchestrator module state between tests."""
    from services import first_run_orchestrator
    first_run_orchestrator.reset_for_tests()
    yield
    first_run_orchestrator.reset_for_tests()


@pytest.fixture
def workspace(tmp_path) -> Path:
    """A fake workspace with `app/` already created."""
    (tmp_path / "app").mkdir()
    return tmp_path


def _success_pull_events(*, model: str):
    """Build an async generator that mimics a successful Ollama pull."""
    async def _gen(model_arg, base_url):
        yield {"status": "pulling manifest"}
        yield {"status": "downloading", "completed": 500_000_000, "total": 5_200_000_000, "digest": "sha256:abc"}
        yield {"status": "downloading", "completed": 5_200_000_000, "total": 5_200_000_000, "digest": "sha256:abc"}
        yield {"status": "verifying sha256 digest"}
        yield {"status": "writing manifest"}
        yield {"status": "success"}
    return _gen


def _failing_pull_events(*, error: str):
    async def _gen(model_arg, base_url):
        yield {"status": "pulling manifest"}
        yield {"status": "error", "error": error}
    return _gen


def _stub_probe_complete():
    async def _gen(*, base_url, ollama_version):
        yield {"event": "started", "candidate_count": 1}
        yield {
            "event": "complete",
            "result": {
                "schema_version": 1,
                "timestamp_utc": "2026-04-30T00:00:00Z",
                "ollama_version": "0.22.0",
                "platform": "darwin-arm64-macos14",
                "ram_gb": 24,
                "recommended_model": "qwen3:8b",
                "safe_fallback_used": False,
                "candidates_evaluated": [],
                "user_override": None,
            },
        }
    return _gen


# ── Marker file helpers ─────────────────────────────────────────────────────


class TestMarkerHelpers:
    def test_marker_path_resolves_under_app_dir(self, workspace):
        from services.first_run_orchestrator import marker_path
        assert marker_path(workspace) == workspace / "app" / ".first_run_complete"

    def test_is_first_run_complete_false_when_missing(self, workspace):
        from services.first_run_orchestrator import is_first_run_complete
        assert is_first_run_complete(workspace) is False

    def test_is_first_run_complete_true_when_present(self, workspace):
        from services.first_run_orchestrator import is_first_run_complete, marker_path
        marker_path(workspace).write_text("{}", encoding="utf-8")
        assert is_first_run_complete(workspace) is True


# ── Pipeline happy path ─────────────────────────────────────────────────────


class TestPipelineHappyPath:
    @pytest.mark.anyio
    async def test_tier_a_runs_full_pipeline_to_complete(self, workspace):
        """Probe → pull primary (qwen3:8b) → marker write → pull fallback
        (qwen3:4b-instruct-2507-q4_K_M) → run probe → complete."""
        from services import first_run_orchestrator
        from services.ollama_service import HardwareProfile

        # Tier-A hardware: 24 GB Apple Silicon
        hw = HardwareProfile(
            os="macos", arch="arm64", total_ram_gb=24.0, free_disk_gb=200,
            cpu_cores=10, gpu_vendor="apple", is_apple_silicon=True, tier="balanced",
        )

        with (
            patch("services.first_run_orchestrator.probe_hardware", return_value=hw),
            patch("services.first_run_orchestrator.pull_model_events",
                  side_effect=_success_pull_events(model="any")),
            patch("services.first_run_orchestrator._run_probe",
                  new_callable=AsyncMock, return_value=True),
        ):
            await first_run_orchestrator.start_async(workspace_path=workspace)
            status = await first_run_orchestrator.wait_for_test()

        assert status.state == "complete"
        assert status.tier == "A"
        assert status.primary_model_id == "qwen3-8b"
        assert status.primary_ollama_model == "qwen3:8b"
        assert status.fallback_model_id == "qwen3-4b-instruct-2507"
        assert status.primary.status == "success"
        assert status.fallback.status == "success"
        assert status.probe_failed is False
        assert status.fallback_failed is False
        assert status.marker_written is True

        # Marker file written with the expected payload
        marker = (workspace / "app" / ".first_run_complete").read_text(encoding="utf-8")
        payload = json.loads(marker)
        assert payload["tier"] == "A"
        assert payload["primary_model"] == "qwen3:8b"
        assert payload["skipped"] is False

    @pytest.mark.anyio
    async def test_tier_c_picks_gpt_oss_120b_primary(self, workspace):
        from services import first_run_orchestrator
        from services.ollama_service import HardwareProfile

        # Tier-C: H100 80 GB
        hw = HardwareProfile(
            os="linux", arch="x64", total_ram_gb=128.0, free_disk_gb=500,
            cpu_cores=32, gpu_vendor="nvidia", gpu_vram_gb=80.0,
            is_apple_silicon=False, tier="workstation",
        )

        with (
            patch("services.first_run_orchestrator.probe_hardware", return_value=hw),
            patch("services.first_run_orchestrator.pull_model_events",
                  side_effect=_success_pull_events(model="any")),
            patch("services.first_run_orchestrator._run_probe",
                  new_callable=AsyncMock, return_value=True),
        ):
            await first_run_orchestrator.start_async(workspace_path=workspace)
            status = await first_run_orchestrator.wait_for_test()

        assert status.tier == "C"
        assert status.primary_model_id == "gpt-oss-120b"
        # Tier C fallback per ladder is qwen3-30b-a3b-instruct-2507
        assert status.fallback_model_id == "qwen3-30b-a3b-instruct-2507"
        assert status.state == "complete"


# ── Failure paths ───────────────────────────────────────────────────────────


class TestPipelineFailures:
    @pytest.mark.anyio
    async def test_primary_pull_failure_is_fatal(self, workspace):
        """Pre-marker errors leave the user with no chat model; state=failed,
        marker NOT written, fallback NOT attempted."""
        from services import first_run_orchestrator
        from services.ollama_service import HardwareProfile

        hw = HardwareProfile(
            os="macos", arch="arm64", total_ram_gb=24.0, free_disk_gb=200,
            cpu_cores=10, gpu_vendor="apple", is_apple_silicon=True, tier="balanced",
        )

        with (
            patch("services.first_run_orchestrator.probe_hardware", return_value=hw),
            patch("services.first_run_orchestrator.pull_model_events",
                  side_effect=_failing_pull_events(error="manifest 404")),
        ):
            await first_run_orchestrator.start_async(workspace_path=workspace)
            status = await first_run_orchestrator.wait_for_test()

        assert status.state == "failed"
        assert "manifest 404" in (status.last_error or "")
        assert status.marker_written is False
        assert status.marker_written is False
        assert (workspace / "app" / ".first_run_complete").exists() is False
        assert status.fallback_model_id == "qwen3-4b-instruct-2507"  # still planned
        assert status.fallback.status == "idle"  # but never attempted

    @pytest.mark.anyio
    async def test_fallback_pull_failure_is_non_fatal(self, workspace):
        """Primary lands → marker written → fallback fails → state still
        reaches 'complete' with fallback_failed=True."""
        from services import first_run_orchestrator
        from services.ollama_service import HardwareProfile

        hw = HardwareProfile(
            os="macos", arch="arm64", total_ram_gb=24.0, free_disk_gb=200,
            cpu_cores=10, gpu_vendor="apple", is_apple_silicon=True, tier="balanced",
        )

        # Two pulls: primary succeeds, fallback fails. The orchestrator calls
        # pull_model_events once per pull — alternate the side_effect.
        call_count = {"n": 0}
        async def _alternating(model_arg, base_url):
            call_count["n"] += 1
            if call_count["n"] == 1:
                async for ev in _success_pull_events(model="primary")(model_arg, base_url):
                    yield ev
            else:
                async for ev in _failing_pull_events(error="network refused")(model_arg, base_url):
                    yield ev

        with (
            patch("services.first_run_orchestrator.probe_hardware", return_value=hw),
            patch("services.first_run_orchestrator.pull_model_events", side_effect=_alternating),
            patch("services.first_run_orchestrator._run_probe",
                  new_callable=AsyncMock, return_value=True),
        ):
            await first_run_orchestrator.start_async(workspace_path=workspace)
            status = await first_run_orchestrator.wait_for_test()

        assert status.state == "complete"
        assert status.fallback_failed is True
        assert "network refused" in (status.fallback.error or "")
        assert status.marker_written is True  # primary still wrote it

    @pytest.mark.anyio
    async def test_probe_failure_is_non_fatal(self, workspace):
        from services import first_run_orchestrator
        from services.ollama_service import HardwareProfile

        hw = HardwareProfile(
            os="macos", arch="arm64", total_ram_gb=24.0, free_disk_gb=200,
            cpu_cores=10, gpu_vendor="apple", is_apple_silicon=True, tier="balanced",
        )

        with (
            patch("services.first_run_orchestrator.probe_hardware", return_value=hw),
            patch("services.first_run_orchestrator.pull_model_events",
                  side_effect=_success_pull_events(model="any")),
            patch("services.first_run_orchestrator._run_probe",
                  new_callable=AsyncMock, return_value=False),
        ):
            await first_run_orchestrator.start_async(workspace_path=workspace)
            status = await first_run_orchestrator.wait_for_test()

        assert status.state == "complete"
        assert status.probe_failed is True
        assert status.marker_written is True

    @pytest.mark.anyio
    async def test_run_probe_persists_catalog_snapshot(self, workspace, monkeypatch):
        """The orchestrator's `_run_probe` reconstructs ProbeResult from
        the event payload and persists it. The catalog snapshot from the
        payload MUST be carried through — without it, every subsequent
        boot's `needs_rerun()` computes
        `set(current_models) - set([]) = all current models` and fires
        the misleading "A new local model is available" banner forever.
        """
        from services import first_run_orchestrator

        # Mock the probe stream to emit a `complete` event with a non-
        # empty catalog snapshot. The orchestrator must carry the field
        # through to the persisted payload.
        async def _fake_probe_events(**_kwargs):
            yield {
                "event": "complete",
                "result": {
                    "schema_version": 1,
                    "timestamp_utc": "2026-05-07T00:00:00Z",
                    "ollama_version": "0.18.0",
                    "platform": "macos-arm64",
                    "ram_gb": 24,
                    "recommended_model": "qwen3:8b",
                    "safe_fallback_used": False,
                    "candidates_evaluated": [],
                    "user_override": None,
                    "catalog_models": ["qwen3:1.7b", "qwen3:4b", "qwen3:8b"],
                },
            }

        from services.ollama_service import RuntimeStatus

        async def _fake_runtime(_base_url):
            return RuntimeStatus(
                reachable=True, version="0.18.0", base_url="http://x", models=[],
            )

        # Point get_settings().workspace_path at the test workspace so
        # the probe writes config.json into the right tree.
        from services import workspace_service as _ws_mod  # noqa: F401
        monkeypatch.setattr(
            "config.get_settings",
            lambda: type("S", (), {"workspace_path": workspace})(),
        )
        # `probe_runtime` and `iter_probe_events` are both imported lazily
        # *inside* `_run_probe` from their source modules, so we patch at
        # the source — patching the orchestrator's namespace would no-op.
        monkeypatch.setattr(
            "services.chat_model_probe.iter_probe_events",
            _fake_probe_events,
        )
        monkeypatch.setattr(
            "services.ollama_service.probe_runtime",
            _fake_runtime,
        )

        ok = await first_run_orchestrator._run_probe(base_url="http://x")
        assert ok is True

        config = json.loads((workspace / "app" / "config.json").read_text())
        persisted = config.get("chat_model_probe")
        assert persisted is not None, "probe should have persisted to config.json"
        assert persisted["catalog_models"] == [
            "qwen3:1.7b", "qwen3:4b", "qwen3:8b",
        ], (
            "catalog snapshot must be carried from event payload to persisted "
            "record — empty list here means `needs_rerun` will fire forever"
        )


# ── Idempotency / lifecycle guards ──────────────────────────────────────────


class TestStartIdempotency:
    @pytest.mark.anyio
    async def test_skip_writes_no_marker(self, workspace):
        from services import first_run_orchestrator

        result = await first_run_orchestrator.start_async(
            skip=True, workspace_path=workspace,
        )

        assert result == {"result": "skipped"}
        assert first_run_orchestrator.current_status().state == "skipped"
        assert (workspace / "app" / ".first_run_complete").exists() is False

    @pytest.mark.anyio
    async def test_existing_marker_skips_pipeline(self, workspace):
        from services import first_run_orchestrator
        # Pre-create the marker — caller already finished a prior run.
        (workspace / "app" / ".first_run_complete").write_text(
            json.dumps({"schema_version": 1, "tier": "A", "primary_model": "qwen3:8b"}),
            encoding="utf-8",
        )

        result = await first_run_orchestrator.start_async(workspace_path=workspace)

        assert result["result"] == "already_complete"
        assert first_run_orchestrator.current_status().state == "complete"
        assert first_run_orchestrator.current_status().marker_written is True

    @pytest.mark.anyio
    async def test_concurrent_start_returns_already_running(self, workspace):
        """Second /start while pipeline is in flight must NOT spawn a 2nd task."""
        import asyncio
        from services import first_run_orchestrator
        from services.ollama_service import HardwareProfile

        hw = HardwareProfile(
            os="macos", arch="arm64", total_ram_gb=24.0, free_disk_gb=200,
            cpu_cores=10, gpu_vendor="apple", is_apple_silicon=True, tier="balanced",
        )

        # Pull events that hang on a sleep so we can race start() against itself.
        gate = asyncio.Event()
        async def _slow_pull(model_arg, base_url):
            yield {"status": "pulling manifest"}
            await gate.wait()
            yield {"status": "success"}

        with (
            patch("services.first_run_orchestrator.probe_hardware", return_value=hw),
            patch("services.first_run_orchestrator.pull_model_events", side_effect=_slow_pull),
            patch("services.first_run_orchestrator._run_probe",
                  new_callable=AsyncMock, return_value=True),
        ):
            r1 = await first_run_orchestrator.start_async(workspace_path=workspace)
            # Yield once so the runner enters _pipeline and reaches probing/pulling
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            r2 = await first_run_orchestrator.start_async(workspace_path=workspace)
            gate.set()
            await first_run_orchestrator.wait_for_test()

        assert r1["result"] == "started"
        assert r2["result"] == "already_running"


# ── Endpoint contracts (FastAPI router) ─────────────────────────────────────


@pytest.fixture
def client():
    from main import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app()
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestFirstRunEndpoints:
    @pytest.mark.anyio
    async def test_status_returns_idle_on_fresh_state(self, client, workspace):
        from services import first_run_orchestrator

        with patch("config.get_settings") as gs:
            settings = MagicMock()
            settings.workspace_path = workspace
            gs.return_value = settings

            resp = await client.get("/api/local/first-run/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "idle"
        assert data["marker_present"] is False
        assert data["primary"]["status"] == "idle"
        assert data["fallback"]["status"] == "idle"

    @pytest.mark.anyio
    async def test_start_skip_path_returns_skipped(self, client, workspace):
        with patch("config.get_settings") as gs:
            settings = MagicMock()
            settings.workspace_path = workspace
            gs.return_value = settings

            resp = await client.post(
                "/api/local/first-run/start",
                json={"skip": True},
            )

        assert resp.status_code == 200
        assert resp.json() == {"result": "skipped"}

    @pytest.mark.anyio
    async def test_start_already_complete_when_marker_present(self, client, workspace):
        (workspace / "app" / ".first_run_complete").write_text(
            json.dumps({"schema_version": 1, "tier": "A", "primary_model": "qwen3:8b"}),
            encoding="utf-8",
        )

        with patch("config.get_settings") as gs:
            settings = MagicMock()
            settings.workspace_path = workspace
            gs.return_value = settings

            resp = await client.post("/api/local/first-run/start", json={})

        assert resp.status_code == 200
        assert resp.json()["result"] == "already_complete"


# ── Lifespan cancel ─────────────────────────────────────────────────────────


class TestCancelAndWait:
    @pytest.mark.anyio
    async def test_cancel_idle_is_noop(self):
        from services import first_run_orchestrator
        # No task started — cancel should not raise
        await first_run_orchestrator.cancel_and_wait()
        assert first_run_orchestrator.current_status().state == "idle"

    @pytest.mark.anyio
    async def test_cancel_in_flight_marks_failed(self, workspace):
        import asyncio
        from services import first_run_orchestrator
        from services.ollama_service import HardwareProfile

        hw = HardwareProfile(
            os="macos", arch="arm64", total_ram_gb=24.0, free_disk_gb=200,
            cpu_cores=10, gpu_vendor="apple", is_apple_silicon=True, tier="balanced",
        )

        gate = asyncio.Event()
        async def _hanging(model_arg, base_url):
            yield {"status": "pulling manifest"}
            await gate.wait()  # never set
            yield {"status": "success"}

        with (
            patch("services.first_run_orchestrator.probe_hardware", return_value=hw),
            patch("services.first_run_orchestrator.pull_model_events", side_effect=_hanging),
        ):
            await first_run_orchestrator.start_async(workspace_path=workspace)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await first_run_orchestrator.cancel_and_wait()

        assert first_run_orchestrator.current_status().state == "failed"
        assert first_run_orchestrator.current_status().last_error == "cancelled"
