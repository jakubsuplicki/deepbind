"""Unit tests for ingest gate (ADR 013).

The gate primitives are re-exported from ``tests.eval.latency.gate`` —
those are tested in ``tests/eval/latency/test_gate.py``. These tests
focus on the ingest-specific helpers: :func:`compare_runs` against the
ingest DEFAULT_METRICS, and :func:`compare_stage_metric` for per-stage
comparisons.
"""

from __future__ import annotations

from tests.eval.ingest.gate import (
    DEFAULT_METRICS,
    Direction,
    Verdict,
    compare_runs,
    compare_stage_metric,
)


def _mk_cell(*, total_p50: float, total_p95: float, total_mean: float) -> dict:
    return {
        "total_ms_p50": total_p50,
        "total_ms_p95": total_p95,
        "total_ms_mean": total_mean,
    }


def test_default_metrics_are_all_lower_is_better():
    """No HIGHER_IS_BETTER metrics in ingest — every stage is wall-clock duration."""
    for direction in DEFAULT_METRICS.values():
        assert direction is Direction.LOWER_IS_BETTER


def test_compare_runs_detects_improvement_across_total_metrics():
    pairs = [
        (_mk_cell(total_p50=1000, total_p95=1500, total_mean=1100),
         _mk_cell(total_p50=500, total_p95=750, total_mean=550)),
        (_mk_cell(total_p50=2000, total_p95=2500, total_mean=2100),
         _mk_cell(total_p50=1000, total_p95=1500, total_mean=1100)),
        (_mk_cell(total_p50=3000, total_p95=3500, total_mean=3100),
         _mk_cell(total_p50=1500, total_p95=2000, total_mean=1600)),
    ]
    decisions = compare_runs(pairs)
    assert decisions["total_ms_p50"].verdict is Verdict.IMPROVEMENT
    assert decisions["total_ms_p95"].verdict is Verdict.IMPROVEMENT


def test_compare_runs_detects_regression():
    pairs = [
        (_mk_cell(total_p50=500, total_p95=750, total_mean=550),
         _mk_cell(total_p50=1000, total_p95=1500, total_mean=1100)),
        (_mk_cell(total_p50=600, total_p95=850, total_mean=650),
         _mk_cell(total_p50=1200, total_p95=1700, total_mean=1300)),
        (_mk_cell(total_p50=550, total_p95=800, total_mean=600),
         _mk_cell(total_p50=1100, total_p95=1600, total_mean=1200)),
    ]
    decisions = compare_runs(pairs)
    assert decisions["total_ms_p50"].verdict is Verdict.REGRESSION


def test_compare_stage_metric_pulls_named_stage_from_paired_cells():
    """compare_stage_metric should pick the matching stage in each side and
    drop pairs where either side is missing the stage."""
    a_cell = {
        "stage_stats": [
            {"name": "extract", "p50_ms": 1000, "p95_ms": 1200},
            {"name": "embed_batch", "p50_ms": 5000, "p95_ms": 5500},
        ],
    }
    b_cell = {
        "stage_stats": [
            {"name": "extract", "p50_ms": 800, "p95_ms": 900},
            {"name": "embed_batch", "p50_ms": 2500, "p95_ms": 2800},
        ],
    }
    pairs = [(a_cell, b_cell)] * 5  # B halves embed time on every pair — clear improvement

    decision = compare_stage_metric(pairs, stage_name="embed_batch", metric="p50_ms")
    assert decision.metric_name == "embed_batch.p50_ms"
    # B p50 is 2500 vs A's 5000 on every pair — paired diff is exactly -2500
    # so the bootstrap CI is [-2500, -2500], excludes zero, verdict is IMPROVEMENT.
    assert decision.verdict is Verdict.IMPROVEMENT
    assert decision.mean_difference == -2500.0


def test_compare_stage_metric_skips_pairs_missing_stage():
    """If a pair lacks the stage in either side it's dropped from the comparison.

    Different scenarios produce different stage stats; the pairing logic
    must tolerate that without crashing.
    """
    a_cell_with = {"stage_stats": [{"name": "extract", "p50_ms": 100}]}
    a_cell_without = {"stage_stats": [{"name": "chunk", "p50_ms": 50}]}
    b_cell_with = {"stage_stats": [{"name": "extract", "p50_ms": 90}]}
    b_cell_without = {"stage_stats": [{"name": "chunk", "p50_ms": 45}]}

    pairs = [
        (a_cell_with, b_cell_with),
        (a_cell_without, b_cell_without),  # this pair lacks "extract" — should drop
        (a_cell_with, b_cell_with),
        (a_cell_with, b_cell_with),
    ]
    decision = compare_stage_metric(pairs, stage_name="extract", metric="p50_ms")
    # Three pairs survived; CI runs against 3 paired values
    assert decision.n_pairs == 3
