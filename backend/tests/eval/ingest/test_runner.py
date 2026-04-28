"""Unit tests for ingest runner aggregation + grid orchestration (ADR 013)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tests.eval.ingest.harness import IngestHarness, IngestRun, StageTiming
from tests.eval.ingest.runner import aggregate, run_grid_sync, skipped_stats
from tests.eval.ingest.scenarios import end_to_end, extract_only, Stage


def _mk_run(*, total: float, stage_times: dict[str, float], err: str | None = None) -> IngestRun:
    stages = tuple(
        StageTiming(name=name, duration_ms=ms, units={"chars": 100})
        for name, ms in stage_times.items()
    )
    return IngestRun(
        scenario_name="x",
        fixture_path="/tmp/x.pdf",
        total_ms=total,
        stages=stages,
        error=err,
    )


def test_aggregate_computes_total_percentiles():
    runs = [
        _mk_run(total=100, stage_times={"extract": 50, "chunk": 30}),
        _mk_run(total=200, stage_times={"extract": 60, "chunk": 40}),
        _mk_run(total=300, stage_times={"extract": 70, "chunk": 50}),
        _mk_run(total=400, stage_times={"extract": 80, "chunk": 60}),
        _mk_run(total=500, stage_times={"extract": 90, "chunk": 70}),
    ]
    s = aggregate(runs, scenario=end_to_end(Path("/tmp/x.pdf")))
    assert s.n_timed_runs == 5
    assert s.n_errors == 0
    assert s.total_ms_p50 == 300
    assert s.total_ms_p95 == 500


def test_aggregate_computes_per_stage_percentiles():
    runs = [
        _mk_run(total=100, stage_times={"extract": 10, "chunk": 90}),
        _mk_run(total=200, stage_times={"extract": 20, "chunk": 180}),
        _mk_run(total=300, stage_times={"extract": 30, "chunk": 270}),
    ]
    s = aggregate(runs, scenario=end_to_end(Path("/tmp/x.pdf")))
    by_name = {st.name: st for st in s.stage_stats}
    assert by_name["extract"].p50_ms == 20
    assert by_name["chunk"].p50_ms == 180


def test_aggregate_orders_stages_by_pipeline_order():
    """Stage stats output must be in pipeline order regardless of dict iteration."""
    runs = [
        _mk_run(
            total=100,
            stage_times={
                "entity_extract": 40,
                "extract": 10,
                "embed_batch": 30,
                "section_detect": 5,
                "chunk": 15,
            },
        ),
    ]
    s = aggregate(runs, scenario=end_to_end(Path("/tmp/x.pdf")))
    names = [st.name for st in s.stage_stats]
    assert names == ["extract", "section_detect", "chunk", "embed_batch", "entity_extract"]


def test_aggregate_excludes_errored_runs():
    runs = [
        _mk_run(total=100, stage_times={"extract": 50}),
        _mk_run(total=0, stage_times={}, err="boom"),
        _mk_run(total=300, stage_times={"extract": 150}),
    ]
    s = aggregate(runs, scenario=end_to_end(Path("/tmp/x.pdf")))
    assert s.n_timed_runs == 2
    assert s.n_errors == 1
    assert s.errors == ("boom",)
    assert s.total_ms_mean == 200


def test_aggregate_handles_all_errors():
    runs = [
        _mk_run(total=0, stage_times={}, err="boom"),
        _mk_run(total=0, stage_times={}, err="boom2"),
    ]
    s = aggregate(runs, scenario=end_to_end(Path("/tmp/x.pdf")))
    assert s.n_timed_runs == 0
    assert s.n_errors == 2
    assert s.total_ms_p50 == 0.0
    assert s.stage_stats == ()


def test_aggregate_records_units_median_per_stage():
    """Unit medians are what makes the JSON interpretable across runs."""
    runs = [
        IngestRun(
            scenario_name="x",
            fixture_path="/tmp/x.pdf",
            total_ms=100,
            stages=(StageTiming(name="chunk", duration_ms=50, units={"chunks_emitted": 100}),),
        ),
        IngestRun(
            scenario_name="x",
            fixture_path="/tmp/x.pdf",
            total_ms=110,
            stages=(StageTiming(name="chunk", duration_ms=55, units={"chunks_emitted": 100}),),
        ),
    ]
    s = aggregate(runs, scenario=end_to_end(Path("/tmp/x.pdf")))
    by_name = {st.name: st for st in s.stage_stats}
    assert by_name["chunk"].units_median["chunks_emitted"] == 100.0


def test_skipped_stats_marks_scenario_as_informational():
    s = skipped_stats(
        scenario=end_to_end(Path("/tmp/missing.pdf")),
        reason="fixture not found",
    )
    assert s.skip_reason == "fixture not found"
    assert s.n_timed_runs == 0
    assert s.n_errors == 0
    assert s.total_ms_p50 == 0.0


# ── Grid orchestration with stub harness ────────────────────────────────────


@dataclass
class _StubHarness:
    """Returns deterministic IngestRun objects so tests don't need real ML deps."""

    duration_per_stage_ms: float = 50.0

    def run_scenario(self, scenario, prepared):  # type: ignore[no-untyped-def]
        if scenario.stage is Stage.END_TO_END:
            stages = tuple(
                StageTiming(name=n, duration_ms=self.duration_per_stage_ms, units={"chars": 10})
                for n in ("extract", "section_detect", "chunk", "embed_batch", "entity_extract")
            )
            total = self.duration_per_stage_ms * 5
        else:
            stages = (StageTiming(name=scenario.stage.value, duration_ms=self.duration_per_stage_ms, units={}),)
            total = self.duration_per_stage_ms
        return IngestRun(
            scenario_name=scenario.name,
            fixture_path=str(scenario.fixture_path),
            total_ms=total,
            stages=stages,
        )


def test_run_grid_sync_skips_missing_fixture(tmp_path):
    """A fixture that doesn't exist must produce a skip_reason cell, not a crash."""
    missing = tmp_path / "does-not-exist.pdf"
    result = run_grid_sync(
        scenarios=[end_to_end(missing)],
        seeds=(1, 2),
        n_warmup_runs=0,
        harness=_StubHarness(),
    )
    assert len(result.stats) == 1
    s = result.stats[0]
    assert s.skip_reason is not None
    assert "not found" in s.skip_reason
    assert s.n_timed_runs == 0


def test_run_grid_sync_runs_n_seeds_per_scenario(tmp_path, monkeypatch):
    """With a present fixture, grid runs warm-up + N timed runs."""
    fx = tmp_path / "x.pdf"
    fx.write_bytes(b"%PDF-1.4\nfake")

    # The runner calls prepare_inputs for non-end-to-end scenarios; for end-to-end
    # it skips that and calls run_scenario directly. Use end-to-end so we don't
    # need to monkeypatch the upstream stages.
    result = run_grid_sync(
        scenarios=[end_to_end(fx)],
        seeds=(1, 2, 3),
        n_warmup_runs=1,
        harness=_StubHarness(),
    )
    s = result.stats[0]
    assert s.n_timed_runs == 3
    assert s.n_errors == 0
    assert s.total_ms_p50 == 250.0  # 5 stages × 50ms each
