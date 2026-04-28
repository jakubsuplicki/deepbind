"""Unit tests for the chat-model self-test probe (ADR 012)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from services.chat_model_probe import (
    HARDWARE_FIT_DEFAULT_CTX,
    PROBE_CONFIG_KEY,
    REALISTIC_TPS_PASS,
    WARM_SHORT_PASS_MS,
    ProbeResult,
    ProbeVerdict,
    _matches_thinking_prose,
    effective_chat_model,
    persist_probe_result,
    probe_hardware_fit,
    read_probe_result,
    recommend_chat_model,
)
from services.ollama_service import ModelCatalogEntry
from tests.eval.latency.harness import TimedResponse


# ── Thinking-prose pattern panel ────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Okay, the user asked me to say",
        "Let me think about this.",
        "The user wants a single word",
        "Hmm, that's an interesting question",
        "First, I need to consider",
        "Wait, that doesn't sound right",
        "So, the answer is...",
        "I need to figure out what they want",
    ],
)
def test_matches_thinking_prose_catches_known_leaks(text):
    assert _matches_thinking_prose(text)


@pytest.mark.parametrize(
    "text",
    [
        "Hi.",
        "Hello!",
        "Hey",
        "Greetings.",
        "The capital of France is Paris.",
        "42",
        "",  # empty falls through to "not a leak" — handled by callers
    ],
)
def test_matches_thinking_prose_clean_outputs_pass(text):
    assert not _matches_thinking_prose(text)


# ── Hardware-fit probe ──────────────────────────────────────────────────────


def _entry(*, id: str, gb: float, kv_per_token: int = 4096) -> ModelCatalogEntry:
    return ModelCatalogEntry(
        id=id,
        preset="balanced",
        ollama_model=id,
        litellm_model=f"ollama_chat/{id}",
        label=id,
        download_size_gb=gb,
        context_window="32K",
        context_tokens=32_768,
        recommended_ram_min_gb=int(gb * 1.5),
        recommended_ram_max_gb=int(gb * 4),
        min_disk_gb=gb + 1,
        cpu_friendly=False,
        gpu_preferred=True,
        strengths=[],
        best_for=[],
        native_tools=False,
        bytes_per_kv_token=kv_per_token,
    )


def test_hardware_fit_passes_when_model_well_under_threshold():
    # 14 GB model, 24 GB RAM, 80% threshold = 19.2 GB allowed
    entry = _entry(id="qwen3:14b", gb=9.0)
    fits, footprint = probe_hardware_fit(
        entry,
        available_ram_bytes=24 * (1024**3),
    )
    assert fits
    # 9 GB weights + 4096 bytes/token × 16384 tokens = ~9 GB + ~0.06 GB
    assert footprint == int(9.0 * (1024**3)) + 4096 * HARDWARE_FIT_DEFAULT_CTX


def test_hardware_fit_fails_when_model_too_big():
    # 30 GB model, 24 GB RAM — clearly over
    entry = _entry(id="qwen3:huge", gb=30.0)
    fits, _ = probe_hardware_fit(
        entry,
        available_ram_bytes=24 * (1024**3),
    )
    assert not fits


def test_hardware_fit_fails_at_threshold_edge():
    # Right at the 80% boundary — check the inequality is non-strict in the right direction
    available = 10 * (1024**3)
    threshold_bytes = int(0.80 * available)  # 8 GB
    # Build an entry whose footprint exceeds threshold by 1 byte
    entry = _entry(id="qwen3:edge", gb=8.0)
    fits, footprint = probe_hardware_fit(
        entry,
        available_ram_bytes=available,
    )
    # Footprint = 8 GB + (4096 × 16384) bytes ≈ 8.06 GB > 8 GB threshold
    assert footprint > threshold_bytes
    assert not fits


def test_hardware_fit_kv_arch_difference_is_load_bearing():
    """Mamba/SWA models with smaller bytes_per_kv_token should have smaller footprints."""
    transformer = _entry(id="t", gb=10.0, kv_per_token=4096)
    mamba = _entry(id="m", gb=10.0, kv_per_token=512)  # state-space cache is fixed-size
    available = 32 * (1024**3)
    _, t_fp = probe_hardware_fit(transformer, available_ram_bytes=available)
    _, m_fp = probe_hardware_fit(mamba, available_ram_bytes=available)
    assert m_fp < t_fp  # mamba has smaller KV footprint


# ── Orchestrator with stubbed Ollama ────────────────────────────────────────


@dataclass
class _StubOllamaClient:
    """A stub OllamaTimedClient that returns scripted TimedResponses.

    Per-model behavior is configured via the ``responses`` dict, keyed by
    model name. Each model has a list of canned responses that are popped
    in order — so we can simulate (correctness, warm-short, realistic) in
    sequence.
    """

    responses: dict[str, list[TimedResponse]]

    async def call(
        self,
        *,
        model: str,
        system_prompt: str,
        user_message: str,
        max_output_tokens: int,
        seed: int = 42,
        scenario_name: str = "unknown",
    ) -> TimedResponse:
        if model not in self.responses or not self.responses[model]:
            return TimedResponse(
                scenario_name=scenario_name,
                model_id=f"ollama:{model}",
                ttft_ms=0,
                decode_tps=0,
                total_ms=0,
                output_tokens=0,
                prompt_tokens=0,
                response_text="",
                error="stub: no more canned responses",
            )
        return self.responses[model].pop(0)


def _ok(model: str, *, scenario: str, text: str, tps: float, total: float) -> TimedResponse:
    return TimedResponse(
        scenario_name=scenario,
        model_id=f"ollama:{model}",
        ttft_ms=200.0,
        decode_tps=tps,
        total_ms=total,
        output_tokens=8,
        prompt_tokens=20,
        response_text=text,
    )


@pytest.mark.asyncio
async def test_recommend_picks_first_passing_candidate(monkeypatch):
    """Largest-first iteration; first model that passes all three probes wins."""
    # All three fit comfortably under 80% of 24 GB (~19 GB threshold).
    # "big" exists to verify largest-first iteration; it'll fail correctness
    # so the orchestrator falls through to "medium" which passes.
    big = _entry(id="qwen3:big", gb=15.0)
    medium = _entry(id="qwen3:medium", gb=8.0)
    small = _entry(id="qwen3:small", gb=4.0)

    # Stub responses: big fails correctness (thinking-leak), medium passes everything
    stub = _StubOllamaClient(
        responses={
            "qwen3:big": [
                _ok("qwen3:big", scenario="probe-correctness",
                    text="Okay, the user asked me to say", tps=10, total=600),
            ],
            "qwen3:medium": [
                _ok("qwen3:medium", scenario="probe-correctness",
                    text="Hi.", tps=20, total=200),
                _ok("qwen3:medium", scenario="warm-short",
                    text="Hi.", tps=20, total=300),  # passes warm threshold
                _ok("qwen3:medium", scenario="chat-realistic-shallow",
                    text="The CSS bug.", tps=14, total=2000),  # passes realistic threshold
            ],
            "qwen3:small": [
                _ok("qwen3:small", scenario="probe-correctness", text="Hi.",
                    tps=30, total=100),
            ],
        }
    )

    # Monkeypatch the OllamaTimedClient constructor so the orchestrator gets our stub
    import services.chat_model_probe as probe_mod

    monkeypatch.setattr(probe_mod, "OllamaTimedClient", lambda **_kw: stub)
    # Also stub probe_hardware so we don't fail on the test runner's hardware
    from services.ollama_service import HardwareProfile
    monkeypatch.setattr(
        probe_mod, "probe_hardware",
        lambda: HardwareProfile(
            os="macos", arch="arm64", total_ram_gb=24.0, free_disk_gb=100.0,
            cpu_cores=10, gpu_vendor="apple", gpu_vram_gb=None,
            is_apple_silicon=True, tier="strong",
        ),
    )

    result = await recommend_chat_model(candidates=[big, medium, small])

    assert result.recommended_model == "qwen3:medium"
    assert not result.safe_fallback_used
    # big should be recorded as fail_correctness
    big_evidence = next(e for e in result.candidates_evaluated if e.model == "qwen3:big")
    assert big_evidence.verdict == ProbeVerdict.FAIL_CORRECTNESS.value
    # medium should be pass
    medium_evidence = next(e for e in result.candidates_evaluated if e.model == "qwen3:medium")
    assert medium_evidence.verdict == ProbeVerdict.PASS.value
    # small should not be evaluated (we stopped after medium passed)
    small_in_evidence = any(e.model == "qwen3:small" for e in result.candidates_evaluated)
    assert not small_in_evidence


@pytest.mark.asyncio
async def test_recommend_returns_safe_fallback_when_nothing_passes(monkeypatch):
    """All candidates fail → recommended_model is None, safe_fallback_used True."""
    only = _entry(id="qwen3:leaky", gb=5.0)
    stub = _StubOllamaClient(
        responses={
            "qwen3:leaky": [
                _ok("qwen3:leaky", scenario="probe-correctness",
                    text="Okay, let me think about that", tps=10, total=600),
            ],
        }
    )
    import services.chat_model_probe as probe_mod
    from services.ollama_service import HardwareProfile

    monkeypatch.setattr(probe_mod, "OllamaTimedClient", lambda **_kw: stub)
    monkeypatch.setattr(
        probe_mod, "probe_hardware",
        lambda: HardwareProfile(
            os="macos", arch="arm64", total_ram_gb=24.0, free_disk_gb=100.0,
            cpu_cores=10, gpu_vendor="apple", gpu_vram_gb=None,
            is_apple_silicon=True, tier="strong",
        ),
    )

    result = await recommend_chat_model(candidates=[only])

    assert result.recommended_model is None
    assert result.safe_fallback_used


@pytest.mark.asyncio
async def test_recommend_skips_oversized_candidates_without_calling_ollama(monkeypatch):
    """Hardware-fit prefilter must skip too-large models before Ollama is called."""
    huge = _entry(id="qwen3:huge", gb=200.0)  # absurdly large
    small = _entry(id="qwen3:small", gb=4.0)
    stub = _StubOllamaClient(
        responses={
            "qwen3:small": [
                _ok("qwen3:small", scenario="probe-correctness", text="Hi.", tps=20, total=200),
                _ok("qwen3:small", scenario="warm-short", text="Hi.", tps=20, total=300),
                _ok("qwen3:small", scenario="chat-realistic-shallow",
                    text="answer", tps=14, total=2000),
            ],
            # No responses for "qwen3:huge" — if the probe calls it, the stub returns an error
        }
    )
    import services.chat_model_probe as probe_mod
    from services.ollama_service import HardwareProfile

    monkeypatch.setattr(probe_mod, "OllamaTimedClient", lambda **_kw: stub)
    monkeypatch.setattr(
        probe_mod, "probe_hardware",
        lambda: HardwareProfile(
            os="macos", arch="arm64", total_ram_gb=24.0, free_disk_gb=100.0,
            cpu_cores=10, gpu_vendor="apple", gpu_vram_gb=None,
            is_apple_silicon=True, tier="strong",
        ),
    )

    result = await recommend_chat_model(candidates=[huge, small])

    huge_evidence = next(e for e in result.candidates_evaluated if e.model == "qwen3:huge")
    assert huge_evidence.verdict == ProbeVerdict.FAIL_HARDWARE_FIT.value
    assert result.recommended_model == "qwen3:small"


# ── Persistence ────────────────────────────────────────────────────────────


def test_persist_and_read_probe_result_round_trip(tmp_path: Path):
    config = tmp_path / "config.json"
    result = ProbeResult(
        schema_version=1,
        timestamp_utc="2026-04-28T12:34:56Z",
        ollama_version="0.18.0",
        platform="darwin-arm64",
        ram_gb=24,
        recommended_model="qwen3:14b",
        safe_fallback_used=False,
        candidates_evaluated=(),
        user_override=None,
    )
    persist_probe_result(result, config_path=config)
    loaded = read_probe_result(config)
    assert loaded is not None
    assert loaded["recommended_model"] == "qwen3:14b"
    assert loaded["schema_version"] == 1


def test_persist_does_not_clobber_other_config_keys(tmp_path: Path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"some_other_setting": "x", "another": 42}),
        encoding="utf-8",
    )
    result = ProbeResult(
        schema_version=1,
        timestamp_utc="2026-04-28T12:34:56Z",
        ollama_version=None,
        platform="darwin-arm64",
        ram_gb=24,
        recommended_model="qwen3:14b",
        safe_fallback_used=False,
        candidates_evaluated=(),
        user_override=None,
    )
    persist_probe_result(result, config_path=config)

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["some_other_setting"] == "x"
    assert data["another"] == 42
    assert data[PROBE_CONFIG_KEY]["recommended_model"] == "qwen3:14b"


def test_effective_chat_model_prefers_user_override(tmp_path: Path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                PROBE_CONFIG_KEY: {
                    "recommended_model": "qwen3:14b",
                    "user_override": "qwen3:30b-a3b-instruct-2507",
                }
            }
        ),
        encoding="utf-8",
    )
    assert effective_chat_model(config) == "qwen3:30b-a3b-instruct-2507"


def test_effective_chat_model_falls_back_to_recommendation(tmp_path: Path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {PROBE_CONFIG_KEY: {"recommended_model": "qwen3:14b", "user_override": None}}
        ),
        encoding="utf-8",
    )
    assert effective_chat_model(config) == "qwen3:14b"


def test_effective_chat_model_returns_none_before_probe_runs(tmp_path: Path):
    config = tmp_path / "config.json"
    # File doesn't exist yet
    assert effective_chat_model(config) is None
    # File exists but no probe key
    config.write_text(json.dumps({"other": "x"}), encoding="utf-8")
    assert effective_chat_model(config) is None
