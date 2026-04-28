"""Ingest benchmark grid runner — orchestrate, aggregate, capture machine info (ADR 013).

Mirrors ``tests/eval/latency/runner.py`` discipline: sequential
execution of (scenario × seed) cells, 1 warm-up + N timed runs,
per-cell aggregation to p50/p95/mean/stdev.

Ingest scenarios don't have a "model" axis the way the chat-latency
harness does — the embedding model + spaCy model are global singletons
loaded once per process. So a "cell" is just a scenario, not (model ×
scenario). The grid axis is scenarios; seeds drive only the run count
(stages here are deterministic given identical inputs, but capturing N
runs gives a real variance measurement to feed the bootstrap-CI gate).

Machine info reuses :func:`tests.eval.latency.runner.capture_machine_info`
since the same hardware identity (model_label, ram_gb) anchors both
baselines.
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .harness import IngestHarness, IngestRun, PreparedInputs, StageTiming
from .scenarios import IngestScenario, Stage

# Re-export from latency.runner so callers don't need to import from two places
from ..latency.runner import (  # noqa: F401 — re-exported for callers
    MachineInfo,
    capture_machine_info,
)


# ── Stage stats ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StageStats:
    """Aggregated per-stage metrics across N timed runs.

    ``units`` carries the median of size descriptors so the JSON shows
    e.g. ``{"chunks_emitted": 4096}`` next to "chunk took 4.2 s p50."
    Per-run units are kept on the underlying :class:`IngestRun` records
    so the floor test can spot variance in shape (a fixture that
    suddenly produces 2× the chunks would show up as a units shift even
    if duration didn't move).
    """

    name: str
    p50_ms: float
    p95_ms: float
    mean_ms: float
    stdev_ms: float
    units_median: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ScenarioStats:
    """Aggregated metrics for one scenario across N timed runs.

    For end-to-end scenarios ``stage_stats`` holds per-stage breakdown
    plus the total. For stage-isolated scenarios ``stage_stats`` has one
    entry (the target stage) and total_ms* mirrors that stage's timings.

    ``skip_reason`` mirrors the latency harness sentinel — a scenario
    can be skipped (e.g. fixture missing) and recorded as informational
    rather than as a measurement failure.
    """

    scenario_name: str
    fixture_path: str
    stage: str  # Stage.value
    n_timed_runs: int
    n_errors: int
    total_ms_p50: float
    total_ms_p95: float
    total_ms_mean: float
    total_ms_stdev: float
    stage_stats: tuple[StageStats, ...] = field(default_factory=tuple)
    runs: tuple[IngestRun, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
    skip_reason: Optional[str] = None


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * pct))))
    return s[k]


def _safe_stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.stdev(values)


def _aggregate_units(stagings: list[StageTiming]) -> dict[str, float]:
    """Median of unit descriptors across N runs of the same stage.

    Median (not mean) because outliers in chunk count would skew the
    interpretation. The chunker is deterministic given identical input,
    so all values should match — a divergence here is a bug, not noise.
    """
    if not stagings:
        return {}
    keys: set[str] = set()
    for st in stagings:
        keys.update(st.units.keys())
    out: dict[str, float] = {}
    for key in keys:
        vals = [
            float(st.units[key])
            for st in stagings
            if key in st.units and isinstance(st.units[key], (int, float))
        ]
        if vals:
            out[key] = float(statistics.median(vals))
    return out


def skipped_stats(
    *,
    scenario: IngestScenario,
    reason: str,
) -> ScenarioStats:
    """Build a sentinel ScenarioStats for a scenario that wasn't measured.

    Used when the fixture is missing on disk. ``n_timed_runs = n_errors = 0``
    so the floor test treats the cell as informational rather than a
    measurement failure.
    """
    return ScenarioStats(
        scenario_name=scenario.name,
        fixture_path=str(scenario.fixture_path),
        stage=scenario.stage.value,
        n_timed_runs=0,
        n_errors=0,
        total_ms_p50=0.0,
        total_ms_p95=0.0,
        total_ms_mean=0.0,
        total_ms_stdev=0.0,
        skip_reason=reason,
    )


def aggregate(
    runs: Sequence[IngestRun],
    *,
    scenario: IngestScenario,
) -> ScenarioStats:
    """Compute p50/p95/mean/stdev across timed runs for one scenario."""
    ok = [r for r in runs if r.error is None]
    err = [r.error for r in runs if r.error is not None]
    totals = [r.total_ms for r in ok]

    # Pivot stage timings: stage_name -> [StageTiming, ...] across runs
    by_stage: dict[str, list[StageTiming]] = {}
    for r in ok:
        for st in r.stages:
            by_stage.setdefault(st.name, []).append(st)

    stage_stats: list[StageStats] = []
    for name, stagings in by_stage.items():
        durs = [st.duration_ms for st in stagings]
        stage_stats.append(
            StageStats(
                name=name,
                p50_ms=_percentile(durs, 0.5),
                p95_ms=_percentile(durs, 0.95),
                mean_ms=statistics.mean(durs) if durs else 0.0,
                stdev_ms=_safe_stdev(durs),
                units_median=_aggregate_units(stagings),
            )
        )
    # Stable order: pipeline order if end-to-end, alphabetical otherwise
    from .scenarios import END_TO_END_STAGE_ORDER

    order_index = {name: i for i, name in enumerate(END_TO_END_STAGE_ORDER)}
    stage_stats.sort(key=lambda s: (order_index.get(s.name, 99), s.name))

    return ScenarioStats(
        scenario_name=scenario.name,
        fixture_path=str(scenario.fixture_path),
        stage=scenario.stage.value,
        n_timed_runs=len(ok),
        n_errors=len(err),
        total_ms_p50=_percentile(totals, 0.5),
        total_ms_p95=_percentile(totals, 0.95),
        total_ms_mean=statistics.mean(totals) if totals else 0.0,
        total_ms_stdev=_safe_stdev(totals),
        stage_stats=tuple(stage_stats),
        runs=tuple(runs),
        errors=tuple(err),
    )


# ── Inputs preparation ──────────────────────────────────────────────────────


def prepare_inputs(scenario: IngestScenario) -> Optional[PreparedInputs]:
    """Build upstream-stage outputs once per scenario.

    Returns None for end-to-end scenarios — those rebuild fresh each run
    so their measured durations include the full pipeline cost. For
    stage-isolated scenarios the prepared inputs are reused across the
    warm-up + N timed runs so we measure only the target stage.

    Failures here propagate as exceptions; the caller surfaces them as
    a skipped scenario with the error message.
    """
    if scenario.stage is Stage.END_TO_END:
        return None

    if scenario.stage is Stage.EXTRACT:
        # Extract scenario re-runs the extraction itself; no prep needed
        # but the harness still receives a PreparedInputs to keep the
        # dispatcher signature uniform. Empty fields are fine — the
        # extract path doesn't read them.
        return PreparedInputs(
            extracted_text="",
            sections=[],
            chunks=[],
            chunk_texts=[],
        )

    from .harness import time_chunk, time_extract, time_section_detect

    text, _ = time_extract(scenario.fixture_path)
    sections, _ = time_section_detect(text)
    chunks, _ = time_chunk(text)
    chunk_texts = [c.text for c in chunks]
    return PreparedInputs(
        extracted_text=text,
        sections=sections,
        chunks=chunks,
        chunk_texts=chunk_texts,
    )


# ── Grid orchestration ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class GridResult:
    """Full output of one ``run_grid`` invocation."""

    machine_info: MachineInfo
    fixture_path: str
    seeds: tuple[int, ...]
    n_warmup_runs: int
    n_timed_runs: int
    stats: tuple[ScenarioStats, ...]


async def run_grid(
    *,
    scenarios: Sequence[IngestScenario],
    seeds: Sequence[int] = (1, 2, 3),
    n_warmup_runs: int = 1,
    harness: Optional[IngestHarness] = None,
    machine_info: Optional[MachineInfo] = None,
    progress: Optional[callable] = None,  # type: ignore[type-arg]
) -> GridResult:
    """Run every scenario × seeds with warm-up + N timed runs.

    Sequential execution — embedding & spaCy NER are CPU-bound on the
    same cores; running concurrently would just trash cache and produce
    noisy timings.

    The default seed count is 3 (not 5 like chat-latency) because each
    end-to-end run on the 911 Report is ~30s and the variance is
    smaller than chat-latency (no model sampling, deterministic text
    pipeline). Three runs is enough to detect a >5% regression with
    bootstrap CI; the user can pass ``--seeds 1,2,3,4,5`` for tighter
    CIs at the cost of run time.

    Failures on one scenario are captured into :class:`ScenarioStats`
    errors and the grid continues — a fixture that's missing on this
    machine shouldn't kill the whole run.
    """
    h = harness or IngestHarness()
    info = machine_info or capture_machine_info()

    n_timed = len(seeds)
    results: list[ScenarioStats] = []

    for scenario in scenarios:
        if not scenario.fixture_path.exists():
            if progress is not None:
                progress(f"  {scenario.name}: SKIP (fixture not found: {scenario.fixture_path})")
            results.append(
                skipped_stats(
                    scenario=scenario,
                    reason=f"fixture not found at {scenario.fixture_path}",
                )
            )
            continue

        if progress is not None:
            progress(f"  {scenario.name}: preparing inputs...")

        try:
            prepared = await asyncio.to_thread(prepare_inputs, scenario)
        except Exception as exc:  # noqa: BLE001
            if progress is not None:
                progress(f"  {scenario.name}: PREP FAILED: {exc}")
            results.append(
                skipped_stats(
                    scenario=scenario,
                    reason=f"prepare_inputs failed: {type(exc).__name__}: {exc}",
                )
            )
            continue

        # Warm-up runs (discarded) — primes the embedding model + spaCy NLP
        for w in range(n_warmup_runs):
            if progress is not None:
                progress(f"  {scenario.name}: warm-up {w + 1}/{n_warmup_runs}...")
            await asyncio.to_thread(h.run_scenario, scenario, prepared)

        timed: list[IngestRun] = []
        for i, _seed in enumerate(seeds, 1):
            if progress is not None:
                progress(f"  {scenario.name}: timed run {i}/{n_timed}...")
            run = await asyncio.to_thread(h.run_scenario, scenario, prepared)
            timed.append(run)

        results.append(aggregate(timed, scenario=scenario))

    fixture_label = (
        scenarios[0].fixture_path.name if scenarios else ""
    )
    return GridResult(
        machine_info=info,
        fixture_path=str(scenarios[0].fixture_path) if scenarios else "",
        seeds=tuple(seeds),
        n_warmup_runs=n_warmup_runs,
        n_timed_runs=n_timed,
        stats=tuple(results),
    )


def run_grid_sync(
    *,
    scenarios: Sequence[IngestScenario],
    seeds: Sequence[int] = (1, 2, 3),
    n_warmup_runs: int = 1,
    harness: Optional[IngestHarness] = None,
    machine_info: Optional[MachineInfo] = None,
) -> GridResult:
    """Sync wrapper for tests / scripts that don't drive an event loop."""
    return asyncio.run(
        run_grid(
            scenarios=scenarios,
            seeds=seeds,
            n_warmup_runs=n_warmup_runs,
            harness=harness,
            machine_info=machine_info,
        )
    )
