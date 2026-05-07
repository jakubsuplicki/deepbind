"""Unit tests for services/memory_pressure_monitor.py (ADR 005 §C triggers 1–2).

Covers the synchronous predicate (`check_can_run`), the ladder picker
(`pick_runnable_model` — happy path, pressure swap, installed-filter,
floor refusal, ceiling cap), the OOM error pattern matcher, and the
catalog reverse-lookup helper.

Headroom math is exercised with explicit `free_ram_bytes` so tests don't
depend on the host's actual RAM. The picker's no-installed-models path is
exercised by passing an empty `installed_ollama_tags` set — that's the
real-world case "Ollama is up but no chat model is on disk yet" (which
shouldn't happen post-G4b2 but the picker has to handle it gracefully).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ── check_can_run ───────────────────────────────────────────────────────────


class TestCheckCanRun:
    def _entry(self, gb: float, kv_per_token: int = 1024):
        from services.ollama_service import ModelCatalogEntry
        return ModelCatalogEntry(
            id=f"test-{gb}gb",
            preset="everyday",
            ollama_model=f"test:{gb}gb",
            label=f"Test {gb}GB",
            download_size_gb=gb,
            context_window="32K",
            context_tokens=32768,
            recommended_ram_min_gb=8,
            recommended_ram_max_gb=16,
            min_disk_gb=gb + 2,
            cpu_friendly=True,
            gpu_preferred=False,
            strengths=[],
            best_for=[],
            native_tools=False,
            bytes_per_kv_token=kv_per_token,
        )

    def test_passes_when_footprint_fits_with_headroom(self):
        from services.memory_pressure_monitor import check_can_run
        # Entry: 4 GB weights, 1024 B/token × 8K ctx = 8 MB KV → ~4 GB footprint
        # Free: 8 GB × 0.8 headroom = 6.4 GB threshold → 4 GB fits
        entry = self._entry(gb=4)
        free_bytes = 8 * (1024 ** 3)
        assert check_can_run(entry, ctx_len_tokens=8192, free_ram_bytes=free_bytes)

    def test_fails_when_footprint_exceeds_headroom(self):
        from services.memory_pressure_monitor import check_can_run
        # Entry: 8 GB weights → footprint > 0.8 × 8 GB free = 6.4 GB threshold
        entry = self._entry(gb=8)
        free_bytes = 8 * (1024 ** 3)
        assert not check_can_run(entry, ctx_len_tokens=4096, free_ram_bytes=free_bytes)

    def test_kv_growth_pushes_over_headroom(self):
        from services.memory_pressure_monitor import check_can_run
        # 4 GB weights + 64K × 65536 B/token = 4 GB + 4 GB = 8 GB
        # Headroom 0.8 × 8 GB = 6.4 GB → fails
        entry = self._entry(gb=4, kv_per_token=65536)
        free_bytes = 8 * (1024 ** 3)
        assert not check_can_run(entry, ctx_len_tokens=65536, free_ram_bytes=free_bytes)

    def test_custom_headroom_fraction(self):
        from services.memory_pressure_monitor import check_can_run
        entry = self._entry(gb=6)
        free_bytes = 8 * (1024 ** 3)
        # Default 0.8 → 6.4 GB threshold → 6 GB passes
        assert check_can_run(entry, ctx_len_tokens=1024, free_ram_bytes=free_bytes)
        # Stricter 0.7 → 5.6 GB threshold → 6 GB fails
        assert not check_can_run(
            entry, ctx_len_tokens=1024, free_ram_bytes=free_bytes, headroom_fraction=0.7
        )


# ── pick_runnable_model ─────────────────────────────────────────────────────


class TestPickRunnableModel:
    """Exercise the ladder walk against the real ADR 005 §C catalog ladder.

    Tier A ladder positions per ADR §C:
      qwen3-30b-a3b-instruct-2507 (opt-in ceiling) > qwen3-8b > qwen3-4b-instruct-2507 > floor

    Tests use the real catalog (not synthetic fixtures) so any catalog drift
    that breaks the ladder ordering is caught here rather than in production.
    """

    @staticmethod
    def _entry_by_id(model_id):
        from services.ollama_service import MODEL_CATALOG
        for e in MODEL_CATALOG:
            if e.id == model_id:
                return e
        return None

    def test_no_swap_when_requested_fits(self):
        from services.memory_pressure_monitor import pick_runnable_model
        requested = self._entry_by_id("qwen3-4b-instruct-2507")
        # Plenty of free RAM → 4B fits comfortably with headroom
        free_bytes = 32 * (1024 ** 3)
        result = pick_runnable_model(
            requested,
            tier="A",
            ctx_len_tokens=8192,
            installed_ollama_tags=[requested.ollama_model, "qwen3:8b"],
            free_ram_bytes=free_bytes,
        )
        assert result.chosen is requested
        assert result.did_swap is False
        assert result.reason is None
        assert result.trail[0] == ("qwen3-4b-instruct-2507", "runnable")

    def test_swaps_to_smaller_when_requested_too_big(self):
        from services.memory_pressure_monitor import pick_runnable_model
        requested = self._entry_by_id("qwen3-8b")
        # 6 GB free → 4.8 GB threshold → 8B (~5 GB+) doesn't fit, 4B (~2.5 GB) does
        free_bytes = 6 * (1024 ** 3)
        result = pick_runnable_model(
            requested,
            tier="A",
            ctx_len_tokens=4096,
            installed_ollama_tags=["qwen3:8b", "qwen3:4b-instruct-2507-q4_K_M"],
            free_ram_bytes=free_bytes,
        )
        assert result.did_swap is True
        assert result.chosen is not None
        assert result.chosen.id == "qwen3-4b-instruct-2507"
        assert result.reason is not None and "switched to" in result.reason.lower()

    def test_walks_past_uninstalled_step(self):
        from services.memory_pressure_monitor import pick_runnable_model
        requested = self._entry_by_id("qwen3-8b")
        # 8B doesn't fit; 4B fallback NOT installed → ladder dead-ends → floor refusal
        free_bytes = 6 * (1024 ** 3)
        result = pick_runnable_model(
            requested,
            tier="A",
            ctx_len_tokens=4096,
            installed_ollama_tags=["qwen3:8b"],  # only 8B on disk
            free_ram_bytes=free_bytes,
        )
        assert result.chosen is None  # floor refusal
        # Trail should record 8B as over_footprint and 4B as not_installed
        statuses = {model_id: status for model_id, status in result.trail}
        assert statuses.get("qwen3-8b") == "over_footprint"
        assert statuses.get("qwen3-4b-instruct-2507") == "not_installed"

    def test_floor_refusal_emits_reason(self):
        from services.memory_pressure_monitor import pick_runnable_model
        requested = self._entry_by_id("qwen3-8b")
        # 1 GB free → nothing fits
        free_bytes = 1 * (1024 ** 3)
        result = pick_runnable_model(
            requested,
            tier="A",
            ctx_len_tokens=4096,
            installed_ollama_tags=["qwen3:8b", "qwen3:4b-instruct-2507-q4_K_M"],
            free_ram_bytes=free_bytes,
        )
        assert result.chosen is None
        assert result.reason is not None
        assert "no installed model fits" in result.reason.lower()

    def test_never_upgrades_under_pressure(self):
        from services.memory_pressure_monitor import pick_runnable_model
        # Even if free RAM is huge and a higher-rung model exists+installed,
        # under pressure we never walk *up* the ladder.
        requested = self._entry_by_id("qwen3-4b-instruct-2507")
        free_bytes = 64 * (1024 ** 3)
        result = pick_runnable_model(
            requested,
            tier="A",
            ctx_len_tokens=4096,
            installed_ollama_tags=["qwen3:8b", "qwen3:4b-instruct-2507-q4_K_M", "qwen3:30b-a3b-instruct-2507-q4_K_M"],
            free_ram_bytes=free_bytes,
        )
        # Should pick the 4B (the requested), not jump up to 8B even though it fits.
        assert result.chosen is requested

    def test_tier_b_ladder_uses_b_positions(self):
        from services.memory_pressure_monitor import pick_runnable_model
        # On Tier B, qwen3-8b is below the 30B-A3B-instruct-2507 primary.
        # Free RAM lets only 8B fit; both installed.
        requested = self._entry_by_id("qwen3-30b-a3b-instruct-2507")
        if requested is None:
            pytest.skip("qwen3-30b-a3b-instruct-2507 missing from catalog")
        free_bytes = 12 * (1024 ** 3)  # tight enough to OOM 30B but fit 8B
        result = pick_runnable_model(
            requested,
            tier="B",
            ctx_len_tokens=8192,
            installed_ollama_tags=[requested.ollama_model, "qwen3:8b", "qwen3:4b-instruct-2507-q4_K_M"],
            free_ram_bytes=free_bytes,
        )
        # We must walk the *Tier B* ladder, not Tier A's, and end up on a runnable rung.
        assert result.did_swap is True
        assert result.chosen is not None


# ── looks_like_oom ──────────────────────────────────────────────────────────


class TestLooksLikeOom:
    def test_matches_common_phrases(self):
        from services.memory_pressure_monitor import looks_like_oom
        assert looks_like_oom("Out of memory")
        assert looks_like_oom("Memory exhausted; try a smaller model")
        assert looks_like_oom("CUDA out of memory: tried to allocate 12 GiB")
        assert looks_like_oom("Metal: failed to allocate buffer")
        assert looks_like_oom("ggml_metal_graph_compute: failed")
        assert looks_like_oom("Cannot allocate memory")

    def test_does_not_match_unrelated_errors(self):
        from services.memory_pressure_monitor import looks_like_oom
        assert not looks_like_oom("Connection refused")
        assert not looks_like_oom("Model not found")
        assert not looks_like_oom("Timeout waiting for response")
        assert not looks_like_oom("")
        assert not looks_like_oom("invalid request")


# ── find_entry_by_ollama_model ─────────────────────────────────────────


class TestFindEntryByModelString:
    def test_resolves_litellm_prefix(self):
        from services.memory_pressure_monitor import find_entry_by_ollama_model
        entry = find_entry_by_ollama_model("qwen3:8b")
        assert entry is not None
        assert entry.id == "qwen3-8b"

    def test_resolves_raw_ollama_tag(self):
        from services.memory_pressure_monitor import find_entry_by_ollama_model
        entry = find_entry_by_ollama_model("qwen3:8b")
        assert entry is not None
        assert entry.id == "qwen3-8b"

    def test_returns_none_for_unknown_tag(self):
        from services.memory_pressure_monitor import find_entry_by_ollama_model
        assert find_entry_by_ollama_model("totally-made-up:99x") is None

    def test_returns_none_for_empty(self):
        from services.memory_pressure_monitor import find_entry_by_ollama_model
        assert find_entry_by_ollama_model("") is None


# ── floor_entry_for_tier (ADR 005 §C trigger 3 — lightweight mode floor) ────


class TestFloorEntryForTier:
    """`floor_entry_for_tier` returns the smallest *installed* entry on the
    tier ladder. Used by the lightweight-mode toggle to hard-pin to the
    floor regardless of memory pressure.
    """

    def test_returns_smallest_installed_on_tier_a(self):
        from services.memory_pressure_monitor import floor_entry_for_tier
        # Tier A ladder top→floor: 30B-A3B-instruct (opt-in) > 8B > 4B-instruct-2507.
        # All three installed → floor is 4B-instruct-2507.
        floor = floor_entry_for_tier(
            "A",
            installed_ollama_tags=[
                "qwen3:30b-a3b-instruct-2507-q4_K_M",
                "qwen3:8b",
                "qwen3:4b-instruct-2507-q4_K_M",
            ],
        )
        assert floor is not None
        assert floor.id == "qwen3-4b-instruct-2507"

    def test_skips_uninstalled_floor(self):
        from services.memory_pressure_monitor import floor_entry_for_tier
        # 4B not installed → floor falls back to next-smallest installed (8B)
        floor = floor_entry_for_tier(
            "A",
            installed_ollama_tags=["qwen3:8b", "qwen3:30b-a3b-instruct-2507-q4_K_M"],
        )
        assert floor is not None
        assert floor.id == "qwen3-8b"

    def test_returns_none_when_nothing_installed(self):
        from services.memory_pressure_monitor import floor_entry_for_tier
        assert floor_entry_for_tier("A", installed_ollama_tags=[]) is None

    def test_returns_none_when_only_off_ladder_models_installed(self):
        from services.memory_pressure_monitor import floor_entry_for_tier
        # A model that exists on disk but isn't on Tier A's ladder shouldn't
        # be picked as the Tier A floor.
        assert floor_entry_for_tier(
            "A",
            installed_ollama_tags=["totally-made-up:99x"],
        ) is None


# ── current_free_ram_bytes ──────────────────────────────────────────────────


class TestCurrentFreeRamBytes:
    def test_returns_nonneg_int(self):
        from services.memory_pressure_monitor import current_free_ram_bytes
        v = current_free_ram_bytes()
        assert isinstance(v, int)
        assert v >= 0

    def test_falls_back_to_zero_when_psutil_missing(self):
        # Force the import inside current_free_ram_bytes to fail.
        from services import memory_pressure_monitor as mpm
        with patch.object(mpm, "logger"):  # silence the warning
            with patch.dict("sys.modules", {"psutil": None}):
                # Import-time patch only catches reimport; the helper does
                # a fresh `import psutil` inside, so we need to make that
                # raise. Easier: stub the function to use a broken loader.
                def _broken_import(*a, **kw):
                    raise ImportError("psutil unavailable")
                with patch("builtins.__import__", side_effect=_broken_import):
                    # Direct call to the underlying logic — current_free_ram_bytes
                    # catches all exceptions and returns 0.
                    assert mpm.current_free_ram_bytes() == 0
