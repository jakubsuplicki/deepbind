"""Unit tests for runner aggregation + machine info capture (ADR 011)."""

from __future__ import annotations

from dataclasses import dataclass

from tests.eval.latency.harness import TimedResponse
from tests.eval.latency.runner import (
    aggregate,
    capture_machine_info,
    run_grid_sync,
)
from tests.eval.latency.scenarios import warm_short


def _mk_run(*, ttft: float, tps: float, total: float, err: str | None = None) -> TimedResponse:
    return TimedResponse(
        scenario_name="x",
        model_id="ollama:qwen3:8b",
        ttft_ms=ttft,
        decode_tps=tps,
        total_ms=total,
        output_tokens=10,
        prompt_tokens=12,
        response_text="hi",
        error=err,
    )


def test_aggregate_computes_percentiles():
    runs = [
        _mk_run(ttft=100, tps=50, total=200),
        _mk_run(ttft=200, tps=40, total=300),
        _mk_run(ttft=300, tps=30, total=400),
        _mk_run(ttft=400, tps=20, total=500),
        _mk_run(ttft=500, tps=10, total=600),
    ]
    s = aggregate(runs, model_id="ollama:qwen3:8b", scenario_name="x")
    assert s.n_timed_runs == 5
    assert s.n_errors == 0
    assert s.ttft_ms_p50 == 300
    assert s.ttft_ms_p95 == 500
    assert s.ttft_ms_mean == 300


def test_aggregate_excludes_errored_runs():
    runs = [
        _mk_run(ttft=100, tps=50, total=200),
        _mk_run(ttft=0, tps=0, total=0, err="HTTP 500: oom"),
        _mk_run(ttft=300, tps=30, total=400),
    ]
    s = aggregate(runs, model_id="ollama:qwen3:8b", scenario_name="x")
    assert s.n_timed_runs == 2
    assert s.n_errors == 1
    assert s.ttft_ms_mean == 200  # (100 + 300) / 2; the 0 from the error excluded
    assert s.errors == ("HTTP 500: oom",)


def test_aggregate_handles_all_errors():
    runs = [
        _mk_run(ttft=0, tps=0, total=0, err="boom"),
        _mk_run(ttft=0, tps=0, total=0, err="boom2"),
    ]
    s = aggregate(runs, model_id="ollama:qwen3:8b", scenario_name="x")
    assert s.n_timed_runs == 0
    assert s.n_errors == 2
    assert s.ttft_ms_p50 == 0.0


def test_capture_machine_info_returns_populated_struct():
    info = capture_machine_info(knob_stack=["flash_attention"])
    assert info.platform  # non-empty
    assert info.model_label  # non-empty
    assert info.knob_stack == ("flash_attention",)


def test_capture_machine_info_dedupes_and_sorts_knob_stack():
    info = capture_machine_info(knob_stack=["b", "a", "a", "c"])
    assert info.knob_stack == ("a", "b", "c")


# ── Grid orchestration with a stub harness ──────────────────────────────────


@dataclass
class _StubOllama:
    """Stub OllamaTimedClient that returns deterministic TimedResponses."""

    ttft_ms: float = 250.0
    decode_tps: float = 40.0
    total_ms: float = 800.0

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
        return TimedResponse(
            scenario_name=scenario_name,
            model_id=f"ollama:{model}",
            ttft_ms=self.ttft_ms + seed,  # vary so percentiles aren't all equal
            decode_tps=self.decode_tps,
            total_ms=self.total_ms + seed,
            output_tokens=8,
            prompt_tokens=20,
            response_text="hello",
        )


def test_run_grid_sync_completes_with_stub():
    result = run_grid_sync(
        scenarios=[warm_short()],
        models=["qwen3:8b"],
        seeds=(1, 2, 3),
        n_warmup_runs=1,
        ollama_client=_StubOllama(),
        pulled_models={"qwen3:8b"},
    )
    assert len(result.stats) == 1
    s = result.stats[0]
    assert s.n_timed_runs == 3
    assert s.n_errors == 0
    assert s.ttft_ms_p50 > 0


def test_run_grid_skips_models_not_pulled_in_ollama():
    """Models not in pulled_models should be recorded as skipped, not erroring 5x."""
    from tests.eval.latency.scenarios import warm_short

    result = run_grid_sync(
        scenarios=[warm_short()],
        models=["qwen3:8b", "qwen3:14b"],
        seeds=(1, 2),
        n_warmup_runs=0,
        ollama_client=_StubOllama(),
        pulled_models={"qwen3:14b"},  # 8b is missing
    )
    by_model = {s.model_id: s for s in result.stats}
    # 8b should be skipped — no measurement, no errors
    eight_b = by_model["ollama:qwen3:8b"]
    assert eight_b.skip_reason is not None
    assert "not pulled" in eight_b.skip_reason
    assert eight_b.n_timed_runs == 0
    assert eight_b.n_errors == 0
    # 14b should run normally
    fourteen_b = by_model["ollama:qwen3:14b"]
    assert fourteen_b.skip_reason is None
    assert fourteen_b.n_timed_runs == 2


def test_aggregate_captures_sample_response_text():
    """The first non-errored run's response_text should be in the aggregate."""
    runs = [
        TimedResponse(
            scenario_name="x",
            model_id="ollama:qwen3:14b",
            ttft_ms=100, decode_tps=20, total_ms=300,
            output_tokens=3, prompt_tokens=10,
            response_text="Hi there.",
        ),
        TimedResponse(
            scenario_name="x",
            model_id="ollama:qwen3:14b",
            ttft_ms=110, decode_tps=18, total_ms=320,
            output_tokens=3, prompt_tokens=10,
            response_text="Different text on second run",
        ),
    ]
    s = aggregate(runs, model_id="ollama:qwen3:14b", scenario_name="x")
    assert s.sample_response_text == "Hi there."  # first non-errored run wins


def test_aggregate_truncates_long_response_text():
    long_text = "x" * 1000
    runs = [
        TimedResponse(
            scenario_name="x", model_id="m",
            ttft_ms=1, decode_tps=1, total_ms=1,
            output_tokens=1, prompt_tokens=1,
            response_text=long_text,
        )
    ]
    s = aggregate(runs, model_id="m", scenario_name="x")
    assert len(s.sample_response_text) == 500
