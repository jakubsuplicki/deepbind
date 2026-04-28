"""Tests for local model support — ollama service + router."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

# ── Tier Classification ──────────────────────────────────────────────────────


class TestClassifyTier:
    def test_light(self):
        from services.ollama_service import classify_tier
        assert classify_tier(8.0) == "light"
        assert classify_tier(12.0) == "light"
        assert classify_tier(15.9) == "light"

    def test_balanced(self):
        from services.ollama_service import classify_tier
        assert classify_tier(16.0) == "balanced"
        assert classify_tier(24.0) == "balanced"
        assert classify_tier(31.9) == "balanced"

    def test_strong(self):
        from services.ollama_service import classify_tier
        assert classify_tier(32.0) == "strong"
        assert classify_tier(40.0) == "strong"
        assert classify_tier(47.9) == "strong"

    def test_workstation(self):
        from services.ollama_service import classify_tier
        assert classify_tier(48.0) == "workstation"
        assert classify_tier(64.0) == "workstation"
        assert classify_tier(128.0) == "workstation"


# ── Model Scoring ────────────────────────────────────────────────────────────


class TestScoreModel:
    def _make_hw(self, ram=32.0, disk=100.0, gpu="apple", apple_silicon=True):
        from services.ollama_service import HardwareProfile
        return HardwareProfile(
            os="macos",
            arch="arm64",
            total_ram_gb=ram,
            free_disk_gb=disk,
            cpu_cores=10,
            gpu_vendor=gpu,
            is_apple_silicon=apple_silicon,
            tier="strong" if ram >= 32 else "balanced",
        )

    def test_great_compat_small_model_big_ram(self):
        from services.ollama_service import score_model, get_model_by_id

        hw = self._make_hw(ram=32.0)
        model = get_model_by_id("qwen3-1.7b")
        rec = score_model(model, hw, [])
        assert rec.compatibility == "great"
        assert rec.score >= 80

    def test_warning_big_model_small_ram(self):
        from services.ollama_service import score_model, get_model_by_id

        hw = self._make_hw(ram=16.0, gpu=None, apple_silicon=False)
        model = get_model_by_id("gemma4-26b-a4b")
        rec = score_model(model, hw, [])
        assert rec.compatibility in ("warning", "unsupported")

    def test_unsupported_not_enough_disk(self):
        from services.ollama_service import score_model, get_model_by_id

        hw = self._make_hw(ram=64.0, disk=5.0)  # 5 GB disk, model needs 18 GB
        model = get_model_by_id("gemma4-26b-a4b")
        rec = score_model(model, hw, [])
        assert rec.compatibility == "unsupported"
        assert rec.score == 0
        assert "disk" in rec.reason.lower()

    def test_installed_detection(self):
        from services.ollama_service import score_model, get_model_by_id

        hw = self._make_hw(ram=32.0)
        model = get_model_by_id("qwen3-8b")
        rec = score_model(model, hw, ["qwen3:8b"])
        assert rec.installed is True

    def test_active_detection(self):
        from services.ollama_service import score_model, get_model_by_id

        hw = self._make_hw(ram=32.0)
        model = get_model_by_id("qwen3-8b")
        # active=True only when the model is both selected AND installed
        rec = score_model(model, hw, ["qwen3:8b"], active_model_id="qwen3-8b")
        assert rec.active is True

    def test_active_false_when_selected_but_not_installed(self):
        from services.ollama_service import score_model, get_model_by_id

        hw = self._make_hw(ram=32.0)
        model = get_model_by_id("qwen3-8b")
        # Selected in config but not installed in Ollama → active must be False
        rec = score_model(model, hw, [], active_model_id="qwen3-8b")
        assert rec.active is False

    def test_cpu_friendly_bonus(self):
        from services.ollama_service import score_model, get_model_by_id

        hw = self._make_hw(ram=16.0, gpu=None, apple_silicon=False)
        model = get_model_by_id("qwen3-1.7b")
        rec = score_model(model, hw, [])
        # cpu_friendly model on CPU-only machine should get bonus
        assert rec.score >= 80


class TestRecommendTop3:
    @pytest.mark.anyio
    async def test_recommend_marks_top_3(self):
        from services.ollama_service import build_catalog, HardwareProfile

        hw = HardwareProfile(
            os="macos", arch="arm64", total_ram_gb=32.0, free_disk_gb=100.0,
            cpu_cores=10, gpu_vendor="apple", is_apple_silicon=True, tier="strong",
        )

        with patch("services.ollama_service.list_installed_models", new_callable=AsyncMock, return_value=[]):
            catalog = await build_catalog(hw, active_model_id=None)

        recommended = [m for m in catalog if m.recommended]
        assert len(recommended) == 3
        # All recommended should be compatible
        for m in recommended:
            assert m.compatibility in ("great", "good")

    @pytest.mark.anyio
    async def test_build_catalog_filters_internal_by_default(self):
        from services.ollama_service import build_catalog, HardwareProfile

        hw = HardwareProfile(
            os="macos", arch="arm64", total_ram_gb=32.0, free_disk_gb=100.0,
            cpu_cores=10, gpu_vendor="apple", is_apple_silicon=True, tier="strong",
        )

        with patch("services.ollama_service.list_installed_models", new_callable=AsyncMock, return_value=[]):
            catalog = await build_catalog(hw, active_model_id=None)

        ids = {m.model_id for m in catalog}
        for internal_id in (
            "gemma4-26b-a4b",
            "qwen3-4b-instruct-2507",
            "qwen3-14b",
            "qwen3-30b-a3b-instruct-2507",
            "granite-4-h-micro",
            "granite-4-h-tiny",
            "granite-4-h-small",
        ):
            assert internal_id not in ids

    @pytest.mark.anyio
    async def test_build_catalog_include_internal_returns_internal_entries(self):
        from services.ollama_service import build_catalog, HardwareProfile

        hw = HardwareProfile(
            os="macos", arch="arm64", total_ram_gb=32.0, free_disk_gb=100.0,
            cpu_cores=10, gpu_vendor="apple", is_apple_silicon=True, tier="strong",
        )

        with patch("services.ollama_service.list_installed_models", new_callable=AsyncMock, return_value=[]):
            catalog = await build_catalog(hw, active_model_id=None, include_internal=True)

        ids = {m.model_id for m in catalog}
        # Sample of expected internal entries (full set covered by
        # test_internal_models_marked at the catalog layer)
        assert "granite-4-h-micro" in ids
        assert "qwen3-30b-a3b-instruct-2507" in ids
        assert "gemma4-26b-a4b" in ids


# ── Hardware Probe ───────────────────────────────────────────────────────────


class TestHardwareProbe:
    def test_returns_valid_profile(self):
        from services.ollama_service import probe_hardware

        hw = probe_hardware()
        assert hw.os in ("macos", "linux", "windows")
        assert hw.arch in ("arm64", "x64")
        assert hw.total_ram_gb > 0
        assert hw.cpu_cores > 0
        assert hw.tier in ("light", "balanced", "strong", "workstation")


# ── Runtime Probe ────────────────────────────────────────────────────────────


class TestRuntimeProbe:
    @pytest.mark.anyio
    async def test_ollama_not_running(self):
        from services.ollama_service import probe_runtime

        # Probe a port that's almost certainly not Ollama
        status = await probe_runtime("http://localhost:19999")
        assert status.running is False
        assert status.reachable is False

    @pytest.mark.anyio
    async def test_runtime_returns_status_object(self):
        from services.ollama_service import probe_runtime

        status = await probe_runtime()
        assert status.runtime == "ollama"
        assert isinstance(status.installed, bool)
        assert isinstance(status.running, bool)


# ── Runtime Load Probe ───────────────────────────────────────────────────────


class TestRuntimeLoadProbe:
    @pytest.mark.anyio
    async def test_returns_valid_shape(self):
        from services.ollama_service import probe_runtime_load

        # Hit a port that's almost certainly not Ollama so the call doesn't depend
        # on the local Ollama state. System signals should still populate.
        load = await probe_runtime_load("http://localhost:19999")
        assert load.total_ram_gb > 0
        assert 0.0 <= load.ram_pct <= 100.0
        assert 0.0 <= load.swap_pct <= 100.0
        assert load.timestamp_utc.endswith("Z")
        assert load.ollama_reachable is False
        assert load.loaded_models == []

    @pytest.mark.anyio
    async def test_apple_silicon_reports_unified_memory(self):
        """On Darwin, gpu_vram_total/used must be None (unified memory)."""
        import platform
        from services.ollama_service import probe_runtime_load

        load = await probe_runtime_load("http://localhost:19999")
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            assert load.gpu_vendor == "apple"
            assert load.gpu_vram_total_gb is None
            assert load.gpu_vram_used_gb is None

    @pytest.mark.anyio
    async def test_loaded_models_parsed(self):
        """Mock Ollama /api/ps and confirm LoadedOllamaModel records parse."""
        from services.ollama_service import probe_runtime_load

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {
                    "name": "qwen3:8b",
                    "size": 5_400_000_000,
                    "size_vram": 5_400_000_000,
                    "expires_at": "2026-04-28T10:00:00Z",
                },
                {
                    "name": "qwen3:1.7b",
                    "size": 1_500_000_000,
                    "size_vram": 0,
                    "expires_at": None,
                },
            ]
        }

        with patch("services.ollama_service.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            load = await probe_runtime_load("http://localhost:11434")

        assert load.ollama_reachable is True
        assert len(load.loaded_models) == 2
        assert load.loaded_models[0].name == "qwen3:8b"
        assert load.loaded_models[0].size == 5_400_000_000
        assert load.loaded_models[0].size_vram == 5_400_000_000
        assert load.loaded_models[1].size_vram == 0

    @pytest.mark.anyio
    async def test_unreachable_ollama_keeps_system_signals(self):
        """When Ollama is unreachable, system signals must still populate."""
        import httpx
        from services.ollama_service import probe_runtime_load

        with patch("services.ollama_service.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            load = await probe_runtime_load()

        assert load.ollama_reachable is False
        assert load.loaded_models == []
        # System signals still populated even when Ollama is down
        assert load.total_ram_gb > 0


class TestRuntimeLoadEndpoint:
    @pytest.mark.anyio
    async def test_endpoint_returns_runtime_load_shape(self, client):
        # Mock Ollama as unreachable so the test doesn't depend on local state
        with patch("services.ollama_service._list_loaded_ollama_models",
                   new_callable=AsyncMock,
                   return_value=(False, [])):
            resp = await client.get("/api/local/runtime/load")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_ram_gb" in data
        assert "ram_pct" in data
        assert "swap_pct" in data
        assert "loaded_models" in data
        assert "ollama_reachable" in data
        assert data["ollama_reachable"] is False
        assert "timestamp_utc" in data


class TestOllamaBaseUrlValidation:
    def test_keeps_localhost(self):
        from services.ollama_service import _normalize_and_validate_ollama_base_url

        assert _normalize_and_validate_ollama_base_url("http://localhost:11434") == "http://localhost:11434"

    def test_keeps_loopback_ip(self):
        from services.ollama_service import _normalize_and_validate_ollama_base_url

        assert _normalize_and_validate_ollama_base_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434"

    def test_rejects_non_local_host(self):
        from services.ollama_service import _normalize_and_validate_ollama_base_url
        from services.ollama_service import DEFAULT_OLLAMA_BASE_URL

        assert _normalize_and_validate_ollama_base_url("http://evil.example:11434") == DEFAULT_OLLAMA_BASE_URL

    def test_rejects_bad_scheme(self):
        from services.ollama_service import _normalize_and_validate_ollama_base_url
        from services.ollama_service import DEFAULT_OLLAMA_BASE_URL

        assert _normalize_and_validate_ollama_base_url("file:///etc/passwd") == DEFAULT_OLLAMA_BASE_URL

    def test_rejects_userinfo(self):
        from services.ollama_service import _normalize_and_validate_ollama_base_url
        from services.ollama_service import DEFAULT_OLLAMA_BASE_URL

        assert _normalize_and_validate_ollama_base_url("http://user:pass@localhost:11434") == DEFAULT_OLLAMA_BASE_URL


# ── Model Catalog ────────────────────────────────────────────────────────────


class TestModelCatalog:
    def test_catalog_size_lower_bound(self):
        from services.ollama_service import get_catalog

        # Lower bound — checks for accidental removal. Adding entries is fine
        # without breaking this test; per-id presence checks below cover the
        # specific entries that must be present.
        catalog = get_catalog()
        assert len(catalog) >= 13

    def test_all_presets_present(self):
        from services.ollama_service import get_catalog

        presets = {m.preset for m in get_catalog()}
        expected_subset = {"fast", "everyday", "balanced", "long-docs", "reasoning", "code", "best-local", "plumbing"}
        assert expected_subset.issubset(presets)

    def test_internal_models_marked(self):
        from services.ollama_service import get_catalog

        # All entries with unverified Ollama tags carry internal=True so the
        # user picker doesn't expose them. Promotion to user-pickable requires
        # tag verification per the catalog correctness pass.
        internal_ids = {m.id for m in get_catalog() if m.internal}
        expected_internal = {
            "gemma4-26b-a4b",
            "qwen3-4b-instruct-2507",
            "qwen3-14b",
            "qwen3-30b-a3b-instruct-2507",
            "granite-4-h-micro",
            "granite-4-h-tiny",
            "granite-4-h-small",
        }
        assert expected_internal.issubset(internal_ids)

    def test_qwen3_native_contexts_corrected(self):
        from services.ollama_service import get_model_by_id

        # Pre-correction these were 40K/40K/256K — now native is 32K everywhere.
        assert get_model_by_id("qwen3-1.7b").context_tokens == 32768
        assert get_model_by_id("qwen3-4b").context_tokens == 32768
        assert get_model_by_id("qwen3-8b").context_tokens == 32768

    def test_devstral_native_context_corrected(self):
        from services.ollama_service import get_model_by_id

        # Pre-correction was 384K (RoPE-extended). Native is 256K.
        assert get_model_by_id("devstral-small-2-24b").context_tokens == 262144

    def test_qwen3_30b_a3b_instruct_2507_present(self):
        from services.ollama_service import get_model_by_id

        # v1 canonical chat model. Currently internal because the Ollama tag
        # is unverified; flips to user-pickable when the tag verifies against
        # the live registry.
        m = get_model_by_id("qwen3-30b-a3b-instruct-2507")
        assert m is not None
        assert m.preset == "best-local"
        assert m.context_tokens == 262144
        assert m.native_tools is True
        assert m.internal is True

    def test_qwen3_4b_instruct_2507_distinct_from_qwen3_4b(self):
        from services.ollama_service import get_model_by_id

        # The base qwen3-4b is 32K; the -2507 variant is the 256K-native sibling.
        base = get_model_by_id("qwen3-4b")
        ext = get_model_by_id("qwen3-4b-instruct-2507")
        assert base.context_tokens == 32768
        assert ext.context_tokens == 262144

    def test_litellm_model_has_prefix(self):
        from services.ollama_service import get_catalog

        for m in get_catalog():
            assert m.litellm_model.startswith("ollama_chat/")

    def test_lookup_by_id(self):
        from services.ollama_service import get_model_by_id

        model = get_model_by_id("qwen3-8b")
        assert model is not None
        assert model.label == "Qwen3 8B"
        assert model.preset == "balanced"

    def test_lookup_missing_returns_none(self):
        from services.ollama_service import get_model_by_id

        assert get_model_by_id("nonexistent") is None


# ── LLMConfig Changes ───────────────────────────────────────────────────────


class TestLLMConfig:
    def test_api_base_field(self):
        from services.llm_service import LLMConfig

        config = LLMConfig(
            provider="ollama",
            model="ollama_chat/qwen3:8b",
            api_key="ollama",
            api_base="http://localhost:11434",
        )
        assert config.api_base == "http://localhost:11434"

    def test_timeout_field(self):
        from services.llm_service import LLMConfig

        config = LLMConfig(
            provider="ollama",
            model="ollama_chat/qwen3:8b",
            api_key="ollama",
            timeout=600,
        )
        assert config.timeout == 600

    def test_defaults_are_none(self):
        from services.llm_service import LLMConfig

        config = LLMConfig(
            provider="openai",
            model="gpt-4o",
            api_key="sk-test",
        )
        assert config.api_base is None
        assert config.timeout is None


class TestLLMServiceResolveModel:
    def test_ollama_model_keeps_prefix(self):
        from services.llm_service import LLMConfig, LLMService

        config = LLMConfig(
            provider="ollama",
            model="ollama_chat/qwen3:8b",
            api_key="ollama",
        )
        svc = LLMService(config)
        assert svc._litellm_model == "ollama_chat/qwen3:8b"

    def test_ollama_model_adds_prefix(self):
        from services.llm_service import LLMConfig, LLMService

        config = LLMConfig(
            provider="ollama",
            model="qwen3:8b",
            api_key="ollama",
        )
        svc = LLMService(config)
        assert svc._litellm_model == "ollama_chat/qwen3:8b"

    def test_openai_model_unchanged(self):
        from services.llm_service import LLMConfig, LLMService

        config = LLMConfig(
            provider="openai",
            model="gpt-4o",
            api_key="sk-test",
        )
        svc = LLMService(config)
        assert svc._litellm_model == "gpt-4o"

    def test_google_model_gets_prefix(self):
        from services.llm_service import LLMConfig, LLMService

        config = LLMConfig(
            provider="google",
            model="gemini-2.5-flash",
            api_key="test",
        )
        svc = LLMService(config)
        assert svc._litellm_model == "gemini/gemini-2.5-flash"


# ── Chat.py _make_llm ───────────────────────────────────────────────────────


class TestMakeLlm:
    def test_ollama_returns_llm_service(self):
        from routers.chat import _make_llm
        from services.llm_service import LLMService

        llm = _make_llm("ollama", "ollama_chat/qwen3:8b", "")
        assert isinstance(llm, LLMService)
        assert llm.config.provider == "ollama"
        assert llm.config.api_base == "http://localhost:11434"

    def test_ollama_custom_base_url(self):
        from routers.chat import _make_llm
        from services.llm_service import LLMService

        llm = _make_llm("ollama", "ollama_chat/qwen3:8b", "", base_url="http://myhost:9999")
        assert isinstance(llm, LLMService)
        assert llm.config.api_base == "http://myhost:9999"

    def test_ollama_timeout_is_1800(self):
        from routers.chat import _make_llm

        llm = _make_llm("ollama", "ollama_chat/qwen3:8b", "")
        assert llm.config.timeout == 1800

    def test_anthropic_returns_claude_service(self):
        from routers.chat import _make_llm
        from services.claude import ClaudeService

        llm = _make_llm(None, None, "sk-ant-test")
        assert isinstance(llm, ClaudeService)

    def test_openai_returns_llm_service(self):
        from routers.chat import _make_llm
        from services.llm_service import LLMService

        llm = _make_llm("openai", "gpt-4o", "sk-test")
        assert isinstance(llm, LLMService)
        assert llm.config.provider == "openai"


# ── Provider Timeout Map ────────────────────────────────────────────────────


class TestProviderTimeouts:
    def test_ollama_timeout(self):
        from services.llm_service import PROVIDER_TIMEOUTS
        assert PROVIDER_TIMEOUTS["ollama"] == 1800

    def test_cloud_timeouts(self):
        from services.llm_service import PROVIDER_TIMEOUTS
        assert PROVIDER_TIMEOUTS["anthropic"] == 120
        assert PROVIDER_TIMEOUTS["openai"] == 120
        assert PROVIDER_TIMEOUTS["google"] == 120


# ── Active Model Config ─────────────────────────────────────────────────────


class TestActiveModelConfig:
    def test_set_and_get(self, tmp_path):
        """Test setting and getting active local model via config.json."""
        from services.ollama_service import (
            set_active_local_model,
            get_active_local_model,
            clear_active_local_model,
        )

        # Patch config.get_settings which is imported lazily
        with patch("config.get_settings") as mock_settings:
            settings = MagicMock()
            settings.workspace_path = tmp_path
            mock_settings.return_value = settings

            # Create app dir
            (tmp_path / "app").mkdir()

            set_active_local_model("qwen3-8b", "ollama_chat/qwen3:8b")
            active = get_active_local_model()
            assert active is not None
            assert active["model_id"] == "qwen3-8b"
            assert active["litellm_model"] == "ollama_chat/qwen3:8b"
            assert active["active"] is True

            clear_active_local_model()
            active = get_active_local_model()
            assert active is None  # active=False means get returns None


# ── Router Endpoint Tests (using FastAPI test client) ────────────────────────


@pytest.fixture
def client():
    """Create a test client for the FastAPI app."""
    from main import create_app
    from httpx import ASGITransport

    app = create_app()
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestLocalModelsAPI:
    @pytest.mark.anyio
    async def test_hardware_endpoint(self, client):
        resp = await client.get("/api/local/hardware")
        assert resp.status_code == 200
        data = resp.json()
        assert "os" in data
        assert "total_ram_gb" in data
        assert "tier" in data

    @pytest.mark.anyio
    async def test_runtime_endpoint(self, client):
        resp = await client.get("/api/local/runtime")
        assert resp.status_code == 200
        data = resp.json()
        assert data["runtime"] == "ollama"
        assert "installed" in data
        assert "running" in data

    @pytest.mark.anyio
    async def test_catalog_endpoint(self, client):
        with patch("services.ollama_service.list_installed_models", new_callable=AsyncMock, return_value=[]):
            resp = await client.get("/api/local/models/catalog")
        assert resp.status_code == 200
        data = resp.json()
        # Lower bound — internal entries (unverified Ollama tags) filtered out.
        assert len(data) >= 6
        # Check that recommendations are scored
        for item in data:
            assert "compatibility" in item
            assert "score" in item
            assert "recommended" in item

        # No internal models leaked through to the user picker
        ids = {item["model_id"] for item in data}
        for internal_id in (
            "gemma4-26b-a4b",
            "qwen3-4b-instruct-2507",
            "qwen3-14b",
            "qwen3-30b-a3b-instruct-2507",
            "granite-4-h-micro",
            "granite-4-h-tiny",
            "granite-4-h-small",
        ):
            assert internal_id not in ids, f"{internal_id} (internal) leaked to /catalog"

    @pytest.mark.anyio
    async def test_select_endpoint(self, client, tmp_path):
        with patch("config.get_settings") as mock_settings:
            settings = MagicMock()
            settings.workspace_path = tmp_path
            mock_settings.return_value = settings
            (tmp_path / "app").mkdir()

            resp = await client.post("/api/local/models/select", json={
                "model_id": "qwen3-8b",
                "litellm_model": "ollama_chat/qwen3:8b",
            })
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
