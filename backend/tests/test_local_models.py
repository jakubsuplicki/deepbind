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
        # 18 GB / 24–48 GB RAM model on a 16 GB CPU box is the canonical
        # "won't fit comfortably" case — was gemma4-26b-a4b before the
        # 2026-05-05 catalog cleanup; qwen3-30b-a3b-instruct-2507 has the
        # same compatibility shape and is the surviving Tier B primary.
        model = get_model_by_id("qwen3-30b-a3b-instruct-2507")
        rec = score_model(model, hw, [])
        assert rec.compatibility in ("warning", "unsupported")

    def test_unsupported_not_enough_disk(self):
        from services.ollama_service import score_model, get_model_by_id

        hw = self._make_hw(ram=64.0, disk=5.0)  # 5 GB disk, model needs 26 GB
        model = get_model_by_id("qwen3-30b-a3b-instruct-2507")
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
        assert "qwen3-30b-a3b-thinking-2507" in ids


# ── ADR 005 — Tier-for-hardware mapping ─────────────────────────────────────


class TestTierForHardware:
    """Boundary cases per ADR 005 §A catalog tier definitions.

    The mapping rule:
      - Tier C: 96+ GB unified RAM, OR datacenter VRAM ≥ 80 GB.
      - Tier B: discrete GPU ≥ 24 GB (RTX 4090+), OR 48+ GB unified Apple Silicon.
      - Tier A: everything else.
    """

    @staticmethod
    def _hw(ram, gpu="apple", vram=None, apple=True, os_="macos"):
        from services.ollama_service import HardwareProfile
        return HardwareProfile(
            os=os_, arch="arm64" if apple else "x64",
            total_ram_gb=ram, free_disk_gb=200, cpu_cores=10,
            gpu_vendor=gpu, gpu_vram_gb=vram, is_apple_silicon=apple,
            tier="strong",
        )

    def test_low_ram_apple_silicon(self):
        from services.ollama_service import tier_for_hardware
        assert tier_for_hardware(self._hw(8)) == "A"
        assert tier_for_hardware(self._hw(16)) == "A"

    def test_24gb_apple_silicon_is_tier_a(self):
        from services.ollama_service import tier_for_hardware
        # ADR §A: "24 GB Apple Silicon is firmly Tier A"
        assert tier_for_hardware(self._hw(24)) == "A"

    def test_32gb_apple_silicon_stays_tier_a(self):
        from services.ollama_service import tier_for_hardware
        # ADR §A: "32 GB unified is Tier A unless GPU >= 16 GB". Apple Silicon
        # has no separate VRAM signal — stays in A.
        assert tier_for_hardware(self._hw(32)) == "A"

    def test_32gb_cpu_with_rtx_4060_stays_tier_a(self):
        from services.ollama_service import tier_for_hardware
        # ADR §A explicitly lists "RTX 4060/4070 8 GB" under Tier A.
        assert tier_for_hardware(self._hw(32, "nvidia", 8.0, apple=False)) == "A"

    def test_32gb_cpu_with_rtx_4090_graduates_to_tier_b(self):
        from services.ollama_service import tier_for_hardware
        # 24 GB VRAM is the Tier B cutoff (RTX 4090). RAM doesn't constrain
        # promotion when the discrete GPU has the headroom.
        assert tier_for_hardware(self._hw(32, "nvidia", 24.0, apple=False)) == "B"

    def test_48gb_apple_silicon_is_tier_b(self):
        from services.ollama_service import tier_for_hardware
        # ADR §A: "48–64 GB unified" is Tier B.
        assert tier_for_hardware(self._hw(48)) == "B"

    def test_64gb_apple_silicon_is_tier_b(self):
        from services.ollama_service import tier_for_hardware
        assert tier_for_hardware(self._hw(64)) == "B"

    def test_96gb_apple_silicon_is_tier_c(self):
        from services.ollama_service import tier_for_hardware
        # ADR §A: "96+ GB unified" is Tier C.
        assert tier_for_hardware(self._hw(96)) == "C"

    def test_128gb_with_a100_is_tier_c(self):
        from services.ollama_service import tier_for_hardware
        assert tier_for_hardware(self._hw(128, "nvidia", 80.0, apple=False)) == "C"

    def test_single_h100_promotes_modest_ram_to_tier_c(self):
        from services.ollama_service import tier_for_hardware
        # H100 80 GB on a 64 GB CPU box → Tier C (the H100 is the constraint
        # gpt-oss-120b is sized against, not host RAM).
        assert tier_for_hardware(self._hw(64, "nvidia", 80.0, apple=False)) == "C"

    def test_rtx_4080_16gb_does_not_graduate_to_tier_b(self):
        from services.ollama_service import tier_for_hardware
        # ADR §A draws Tier B at "RTX 4090 24 GB", not 16-GB-class cards.
        # An RTX 4080 (16 GB) on a 32 GB box stays Tier A.
        assert tier_for_hardware(self._hw(32, "nvidia", 16.0, apple=False)) == "A"


# ── ADR 005 — First-run defaults + downgrade ladder ─────────────────────────


class TestFirstRunDefault:
    def test_tier_a_primary_is_qwen3_8b(self):
        from services.ollama_service import first_run_default_for
        assert first_run_default_for("A").id == "qwen3-8b"

    def test_tier_b_primary_is_qwen3_30b_a3b_instruct_2507(self):
        from services.ollama_service import first_run_default_for
        assert first_run_default_for("B").id == "qwen3-30b-a3b-instruct-2507"

    def test_tier_c_primary_is_gpt_oss_120b(self):
        from services.ollama_service import first_run_default_for
        assert first_run_default_for("C").id == "gpt-oss-120b"

    def test_unknown_tier_returns_none(self):
        from services.ollama_service import first_run_default_for
        assert first_run_default_for("Z") is None


class TestDowngradeLadder:
    def test_tier_a_ladder_top_to_floor(self):
        from services.ollama_service import downgrade_ladder_for
        ladder = downgrade_ladder_for("A")
        ids = [e.id for e in ladder]
        # ADR 005 §A — Tier A ladder: opt-in 30B-A3B → 8B → 4B-Instruct-2507
        # (the -2507 variant per §A "downgrade ladder target", not plain qwen3-4b).
        assert ids == ["qwen3-30b-a3b-instruct-2507", "qwen3-8b", "qwen3-4b-instruct-2507"]

    def test_tier_b_ladder_top_to_floor(self):
        from services.ollama_service import downgrade_ladder_for
        ladder = downgrade_ladder_for("B")
        ids = [e.id for e in ladder]
        # ADR 005 §C — Tier B: opt-in gpt-oss-120b → 30B-A3B → 8B → 4B
        assert ids == ["gpt-oss-120b", "qwen3-30b-a3b-instruct-2507", "qwen3-8b", "qwen3-4b"]

    def test_tier_c_ladder_top_to_floor(self):
        from services.ollama_service import downgrade_ladder_for
        ladder = downgrade_ladder_for("C")
        ids = [e.id for e in ladder]
        # ADR 005 §C — Tier C: gpt-oss-120b → 30B-A3B → 8B → 4B
        assert ids == ["gpt-oss-120b", "qwen3-30b-a3b-instruct-2507", "qwen3-8b", "qwen3-4b"]

    def test_unknown_tier_returns_empty_ladder(self):
        from services.ollama_service import downgrade_ladder_for
        assert downgrade_ladder_for("Z") == []

    def test_ladder_positions_descend_strictly(self):
        from services.ollama_service import downgrade_ladder_for
        for tier in ("A", "B", "C"):
            positions = [e.ladder_positions[tier] for e in downgrade_ladder_for(tier)]
            assert positions == sorted(positions, reverse=True)
            assert len(positions) == len(set(positions)), \
                f"Duplicate ladder position in tier {tier}"

    def test_exclude_opt_in_drops_tier_b_ceiling(self):
        from services.ollama_service import downgrade_ladder_for
        # gpt-oss-120b is opt-in for Tier B (it's the first-run default for C,
        # not B). Excluding opt-in entries from the Tier B ladder should drop it.
        ids_full = [e.id for e in downgrade_ladder_for("B")]
        ids_no_opt_in = [e.id for e in downgrade_ladder_for("B", include_opt_in=False)]
        assert "gpt-oss-120b" in ids_full
        assert "gpt-oss-120b" not in ids_no_opt_in
        # The Tier B primary itself must remain.
        assert "qwen3-30b-a3b-instruct-2507" in ids_no_opt_in


class TestCatalogLicenseAnnotation:
    def test_qwen_and_granite_entries_apache_2(self):
        from services.ollama_service import MODEL_CATALOG
        for entry in MODEL_CATALOG:
            if entry.id.startswith("qwen3-") or entry.id.startswith("granite-"):
                assert entry.license == "Apache-2.0", \
                    f"{entry.id} should be Apache-2.0, got {entry.license}"

    def test_gpt_oss_120b_apache_2(self):
        from services.ollama_service import get_model_by_id
        # gpt-oss is OpenAI's Apache 2.0 release; license filter must pass it.
        assert get_model_by_id("gpt-oss-120b").license == "Apache-2.0"

    def test_no_non_permissive_entries(self):
        # ADR 005 §A — catalog-discipline rule: only Apache-2.0 / MIT entries
        # are allowed in the v1 bundle. The Literal type on
        # ModelCatalogEntry.license already rejects "non-permissive" at
        # construction time; this runtime assertion is the redundant guard
        # that catches metadata drift in case a future PR widens the type.
        from services.ollama_service import MODEL_CATALOG
        for entry in MODEL_CATALOG:
            assert entry.license in ("Apache-2.0", "MIT"), \
                f"{entry.id} has non-permissive license {entry.license!r} — " \
                f"violates ADR 005 §A catalog-discipline rule"

    def test_removed_non_permissive_ids_stay_removed(self):
        # Catalog cleanup 2026-05-05 (audit finding #6) deleted these four
        # entries (Mistral Research License + Gemma Terms of Use). Re-adding
        # any of them is a regression — guard it explicitly so a future PR
        # that "just adds the model back" trips this test, not a buyer's
        # legal review.
        from services.ollama_service import get_model_by_id
        for stale_id in ("ministral-3-8b", "gemma4-e4b",
                         "devstral-small-2-24b", "gemma4-26b-a4b"):
            assert get_model_by_id(stale_id) is None, \
                f"{stale_id} reintroduced — see audit finding #6 / ADR 005 §A"


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
        # specific entries that must be present. Bound dropped from 13 → 11
        # on 2026-05-05 when the four non-permissive entries were removed
        # under audit finding #6.
        catalog = get_catalog()
        assert len(catalog) >= 11

    def test_all_presets_present(self):
        from services.ollama_service import get_catalog

        presets = {m.preset for m in get_catalog()}
        # "code" was the devstral-small-2-24b slot; the entry was removed
        # under the 2026-05-05 catalog cleanup (Mistral Research License) and
        # no Apache-2.0 / MIT replacement exists in the v1 catalog. Coding
        # workloads route to qwen3-30b-a3b-instruct-2507 / granite-4-h-small
        # via the balanced / best-local presets instead.
        expected_subset = {"fast", "everyday", "balanced", "long-docs", "reasoning", "best-local", "plumbing"}
        assert expected_subset.issubset(presets)

    def test_internal_models_marked(self):
        from services.ollama_service import get_catalog

        # All entries with unverified Ollama tags carry internal=True so the
        # user picker doesn't expose them. Promotion to user-pickable requires
        # tag verification per the catalog correctness pass.
        internal_ids = {m.id for m in get_catalog() if m.internal}
        expected_internal = {
            "qwen3-4b-instruct-2507",
            "qwen3-14b",
            "qwen3-30b-a3b-instruct-2507",
            "qwen3-30b-a3b-thinking-2507",
            "granite-4-h-micro",
            "granite-4-h-tiny",
            "granite-4-h-small",
            "gpt-oss-120b",
        }
        assert expected_internal.issubset(internal_ids)

    def test_qwen3_native_contexts_corrected(self):
        from services.ollama_service import get_model_by_id

        # Pre-correction these were 40K/40K/256K — now native is 32K everywhere.
        assert get_model_by_id("qwen3-1.7b").context_tokens == 32768
        assert get_model_by_id("qwen3-4b").context_tokens == 32768
        assert get_model_by_id("qwen3-8b").context_tokens == 32768

    # `test_devstral_native_context_corrected` removed 2026-05-05 — devstral-small-2-24b
    # was deleted from the catalog under audit finding #6 (Mistral Research License is
    # non-permissive). Native-context regression coverage for the surviving long-context
    # entries lives in `test_qwen3_30b_a3b_instruct_2507_present` (256K) and
    # `test_qwen3_4b_instruct_2507_distinct_from_qwen3_4b` below.

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

    def test_catalog_uses_raw_ollama_model_strings(self):
        """ADR 015 — catalog stores the raw Ollama tag (e.g. `qwen3:8b`), no
        LiteLLM-style `ollama_chat/` prefix anywhere. The prefix is gone."""
        from services.ollama_service import get_catalog

        for m in get_catalog():
            assert not m.ollama_model.startswith("ollama_chat/")
            assert ":" in m.ollama_model  # tag form like "qwen3:8b"

    def test_lookup_by_id(self):
        from services.ollama_service import get_model_by_id

        model = get_model_by_id("qwen3-8b")
        assert model is not None
        assert model.label == "Qwen3 8B"
        assert model.preset == "balanced"

    def test_lookup_missing_returns_none(self):
        from services.ollama_service import get_model_by_id

        assert get_model_by_id("nonexistent") is None


# ── LLMConfig / LLMService — deleted with ADR 015 ─────────────────────────────
# The TestLLMConfig and TestLLMServiceResolveModel classes that lived here
# previously tested the LiteLLM-era multi-provider config and model-string
# resolution. Both are replaced by `OllamaDispatchConfig` / `OllamaDispatcher`
# (covered in `tests/test_ollama_dispatcher.py`) — single-target, no
# multi-provider model-string mangling needed.


# ── Chat.py _make_llm ───────────────────────────────────────────────────────


class TestMakeLlm:
    def test_ollama_returns_dispatcher(self):
        # ADR 015 §B — Ollama path now uses `OllamaDispatcher` (official
        # `ollama` Python client), no longer `LLMService` / LiteLLM.
        from routers.chat import _make_llm
        from services.ollama_dispatcher import OllamaDispatcher

        llm = _make_llm("ollama", "qwen3:8b", "")
        assert isinstance(llm, OllamaDispatcher)
        assert llm.config.api_base == "http://localhost:11434"

    def test_ollama_custom_base_url(self):
        from routers.chat import _make_llm
        from services.ollama_dispatcher import OllamaDispatcher

        llm = _make_llm("ollama", "qwen3:8b", "", base_url="http://myhost:9999")
        assert isinstance(llm, OllamaDispatcher)
        assert llm.config.api_base == "http://myhost:9999"

    def test_ollama_timeout_is_1800(self):
        from routers.chat import _make_llm

        llm = _make_llm("ollama", "qwen3:8b", "")
        assert llm.config.timeout == 1800

    def test_default_returns_dispatcher(self):
        # ADR 015: with no explicit provider/model the router still
        # constructs an OllamaDispatcher pointed at the default model.
        from routers.chat import _make_llm
        from services.ollama_dispatcher import OllamaDispatcher

        llm = _make_llm(None, None, "")
        assert isinstance(llm, OllamaDispatcher)


# ── Provider Timeout Map — deleted with ADR 015 ────────────────────────────
# The TestProviderTimeouts class that lived here previously tested
# `PROVIDER_TIMEOUTS` from the LiteLLM service. Single-target now: timeout
# is `OllamaDispatchConfig.timeout` and is exercised in
# `tests/test_ollama_dispatcher.py::test_default_config_uses_module_defaults`.


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

            set_active_local_model("qwen3-8b", "qwen3:8b")
            active = get_active_local_model()
            assert active is not None
            assert active["model_id"] == "qwen3-8b"
            assert active["ollama_model"] == "qwen3:8b"
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
        # Was 6 before the 2026-05-05 catalog cleanup removed the three
        # non-permissive non-internal entries (ministral-3-8b, gemma4-e4b,
        # devstral-small-2-24b). Surviving user-pickable: qwen3-1.7b/4b/8b.
        assert len(data) >= 3
        # Check that recommendations are scored
        for item in data:
            assert "compatibility" in item
            assert "score" in item
            assert "recommended" in item

        # No internal models leaked through to the user picker
        ids = {item["model_id"] for item in data}
        for internal_id in (
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
                "ollama_model": "qwen3:8b",
            })
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"


# ── Chat-model self-test endpoints (ADR 012) ────────────────────────────────


class TestChatModelProbeEndpoints:
    @pytest.mark.anyio
    async def test_get_returns_no_prior_probe_on_fresh_workspace(self, client, tmp_path):
        """First-launch state: no persisted record → ``needs_rerun=True``,
        reason=``no_prior_probe`` so the UI knows to trigger /run."""
        from services.ollama_service import RuntimeStatus

        (tmp_path / "app").mkdir()
        runtime = RuntimeStatus(
            runtime="ollama", installed=True, running=True,
            base_url="http://localhost:11434", reachable=True, version="0.18.0",
        )
        with (
            patch("config.get_settings") as mock_settings,
            patch("routers.local_models.probe_runtime", new_callable=AsyncMock, return_value=runtime),
        ):
            settings = MagicMock()
            settings.workspace_path = tmp_path
            mock_settings.return_value = settings

            resp = await client.get("/api/local/chat-model-probe")
            assert resp.status_code == 200
            data = resp.json()
            assert data["persisted"] is None
            assert data["needs_rerun"] is True
            assert data["rerun_reason"] == "no_prior_probe"
            assert data["runtime_reachable"] is True
            assert data["current_environment"]["ollama_version"] == "0.18.0"

    @pytest.mark.anyio
    async def test_get_returns_fresh_when_persisted_matches_environment(self, client, tmp_path):
        from services.chat_model_probe import (
            PROBE_CONFIG_KEY, _platform_string,
        )
        from services.ollama_service import RuntimeStatus

        (tmp_path / "app").mkdir()
        config_path = tmp_path / "app" / "config.json"
        config_path.write_text(
            json.dumps({
                PROBE_CONFIG_KEY: {
                    "schema_version": 1,
                    "ollama_version": "0.18.0",
                    "platform": _platform_string(),
                    "candidates_evaluated": [],
                    "catalog_models": [],
                    "recommended_model": "qwen3:14b",
                }
            }),
            encoding="utf-8",
        )
        runtime = RuntimeStatus(
            runtime="ollama", installed=True, running=True,
            base_url="http://localhost:11434", reachable=True, version="0.18.0",
        )
        with (
            patch("config.get_settings") as mock_settings,
            patch("routers.local_models.probe_runtime", new_callable=AsyncMock, return_value=runtime),
            patch("services.chat_model_probe.get_catalog", return_value=[]),
        ):
            settings = MagicMock()
            settings.workspace_path = tmp_path
            mock_settings.return_value = settings

            resp = await client.get("/api/local/chat-model-probe")
            assert resp.status_code == 200
            data = resp.json()
            assert data["persisted"]["recommended_model"] == "qwen3:14b"
            assert data["needs_rerun"] is False
            assert data["rerun_reason"] == "fresh"

    @pytest.mark.anyio
    async def test_run_refuses_when_runtime_unreachable(self, client, tmp_path):
        """Don't waste the user's time probing every candidate as
        fail_unreachable — return 503 so the UI prompts them to start Ollama."""
        from services.ollama_service import RuntimeStatus

        (tmp_path / "app").mkdir()
        runtime = RuntimeStatus(
            runtime="ollama", installed=False, running=False,
            base_url="http://localhost:11434", reachable=False, version=None,
        )
        with (
            patch("config.get_settings") as mock_settings,
            patch("routers.local_models.probe_runtime", new_callable=AsyncMock, return_value=runtime),
        ):
            settings = MagicMock()
            settings.workspace_path = tmp_path
            mock_settings.return_value = settings

            resp = await client.post("/api/local/chat-model-probe/run")
            assert resp.status_code == 503

    @pytest.mark.anyio
    async def test_override_endpoint_sets_user_override(self, client, tmp_path):
        (tmp_path / "app").mkdir()
        with patch("config.get_settings") as mock_settings:
            settings = MagicMock()
            settings.workspace_path = tmp_path
            mock_settings.return_value = settings

            resp = await client.post(
                "/api/local/chat-model-probe/override",
                json={"model": "qwen3:14b"},
            )
            assert resp.status_code == 200
            assert resp.json()["user_override"] == "qwen3:14b"

            # Clearing the override should set it back to null
            resp = await client.post(
                "/api/local/chat-model-probe/override",
                json={"model": None},
            )
            assert resp.status_code == 200
            assert resp.json()["user_override"] is None

    @pytest.mark.anyio
    async def test_run_streams_events_and_persists_result(self, client, tmp_path, monkeypatch):
        """The streaming endpoint must consume the probe generator, persist
        the final result, and emit SSE events the frontend can render."""
        from services.chat_model_probe import PROBE_CONFIG_KEY
        from services.ollama_service import RuntimeStatus

        (tmp_path / "app").mkdir()
        runtime = RuntimeStatus(
            runtime="ollama", installed=True, running=True,
            base_url="http://localhost:11434", reachable=True, version="0.18.0",
        )

        async def fake_iter(**_kw):
            yield {"event": "started", "candidate_count": 1, "available_ram_bytes": 24 * (1024**3)}
            yield {
                "event": "candidate_start", "model": "qwen3:14b",
                "index": 0, "candidate_count": 1,
            }
            yield {
                "event": "candidate_evidence",
                "evidence": {"model": "qwen3:14b", "verdict": "pass"},
            }
            yield {
                "event": "complete",
                "result": {
                    "schema_version": 1,
                    "timestamp_utc": "2026-04-29T00:00:00Z",
                    "ollama_version": "0.18.0",
                    "platform": "darwin-arm64-macos14",
                    "ram_gb": 24,
                    "recommended_model": "qwen3:14b",
                    "safe_fallback_used": False,
                    "candidates_evaluated": [{
                        "model": "qwen3:14b",
                        "verdict": "pass",
                        "correctness_response": None,
                        "hardware_fit_bytes": None,
                        "available_ram_bytes": None,
                        "warm_short_total_ms": None,
                        "realistic_tps": None,
                        "error_message": None,
                    }],
                    "user_override": None,
                },
            }

        with (
            patch("config.get_settings") as mock_settings,
            patch("routers.local_models.probe_runtime", new_callable=AsyncMock, return_value=runtime),
            patch("routers.local_models.iter_probe_events", side_effect=lambda **kw: fake_iter(**kw)),
        ):
            settings = MagicMock()
            settings.workspace_path = tmp_path
            mock_settings.return_value = settings

            resp = await client.post("/api/local/chat-model-probe/run")
            assert resp.status_code == 200
            body = resp.text
            # SSE shape: at least one started + one complete event
            assert "\"event\": \"started\"" in body
            assert "\"event\": \"complete\"" in body
            # Result was persisted
            saved = json.loads((tmp_path / "app" / "config.json").read_text(encoding="utf-8"))
            assert saved[PROBE_CONFIG_KEY]["recommended_model"] == "qwen3:14b"

    @pytest.mark.anyio
    async def test_run_preserves_existing_user_override(self, client, tmp_path):
        """A re-run shouldn't silently drop the user's override choice."""
        from services.chat_model_probe import PROBE_CONFIG_KEY
        from services.ollama_service import RuntimeStatus

        (tmp_path / "app").mkdir()
        config_path = tmp_path / "app" / "config.json"
        config_path.write_text(
            json.dumps({
                PROBE_CONFIG_KEY: {
                    "schema_version": 1,
                    "user_override": "qwen3:30b-a3b-instruct-2507",
                    "candidates_evaluated": [],
                }
            }),
            encoding="utf-8",
        )
        runtime = RuntimeStatus(
            runtime="ollama", installed=True, running=True,
            base_url="http://localhost:11434", reachable=True, version="0.18.0",
        )

        async def fake_iter(**_kw):
            yield {
                "event": "complete",
                "result": {
                    "schema_version": 1,
                    "timestamp_utc": "2026-04-29T00:00:00Z",
                    "ollama_version": "0.18.0",
                    "platform": "darwin-arm64-macos14",
                    "ram_gb": 24,
                    "recommended_model": "qwen3:14b",
                    "safe_fallback_used": False,
                    "candidates_evaluated": [],
                    "user_override": None,
                },
            }

        with (
            patch("config.get_settings") as mock_settings,
            patch("routers.local_models.probe_runtime", new_callable=AsyncMock, return_value=runtime),
            patch("routers.local_models.iter_probe_events", side_effect=lambda **kw: fake_iter(**kw)),
        ):
            settings = MagicMock()
            settings.workspace_path = tmp_path
            mock_settings.return_value = settings

            await client.post("/api/local/chat-model-probe/run")
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            assert saved[PROBE_CONFIG_KEY]["user_override"] == "qwen3:30b-a3b-instruct-2507"
            assert saved[PROBE_CONFIG_KEY]["recommended_model"] == "qwen3:14b"
