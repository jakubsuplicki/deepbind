"""Unit tests for the bootstrap-CI gate (ADR 011)."""

from __future__ import annotations

from tests.eval.latency.gate import (
    DEFAULT_METRICS,
    Direction,
    Verdict,
    compare_metric,
    compare_runs,
)


def test_lower_is_better_improvement_when_b_clearly_faster():
    # B is consistently faster than A — TTFT improvement
    a = [1000.0, 1100.0, 1050.0, 1200.0, 980.0]
    b = [800.0, 850.0, 820.0, 900.0, 780.0]
    decision = compare_metric(
        metric_name="ttft_ms_p50",
        direction=Direction.LOWER_IS_BETTER,
        a=a,
        b=b,
    )
    assert decision.verdict is Verdict.IMPROVEMENT
    assert decision.mean_difference < 0  # B - A is negative when B is lower
    assert decision.ci_high < 0  # CI excludes zero on the improvement side


def test_lower_is_better_regression_when_b_clearly_slower():
    a = [800.0, 850.0, 820.0, 900.0, 780.0]
    b = [1000.0, 1100.0, 1050.0, 1200.0, 980.0]
    decision = compare_metric(
        metric_name="ttft_ms_p50",
        direction=Direction.LOWER_IS_BETTER,
        a=a,
        b=b,
    )
    assert decision.verdict is Verdict.REGRESSION
    assert decision.mean_difference > 0


def test_higher_is_better_improvement_for_throughput():
    # decode_tps: higher is better
    a = [40.0, 38.0, 42.0, 41.0, 39.0]
    b = [55.0, 58.0, 56.0, 60.0, 54.0]
    decision = compare_metric(
        metric_name="decode_tps_p50",
        direction=Direction.HIGHER_IS_BETTER,
        a=a,
        b=b,
    )
    assert decision.verdict is Verdict.IMPROVEMENT
    assert decision.mean_difference > 0


def test_equivalent_when_ci_straddles_zero():
    # Same distribution → no significant difference
    a = [100.0, 110.0, 95.0, 105.0, 102.0]
    b = [99.0, 108.0, 97.0, 103.0, 100.0]
    decision = compare_metric(
        metric_name="ttft_ms_p50",
        direction=Direction.LOWER_IS_BETTER,
        a=a,
        b=b,
    )
    assert decision.verdict is Verdict.EQUIVALENT


def test_insufficient_data_when_too_few_pairs():
    decision = compare_metric(
        metric_name="ttft_ms_p50",
        direction=Direction.LOWER_IS_BETTER,
        a=[100.0],
        b=[80.0],
    )
    assert decision.verdict is Verdict.INSUFFICIENT_DATA
    assert decision.n_pairs == 1


def test_compare_runs_applies_all_default_metrics():
    """compare_runs should produce one decision per metric in DEFAULT_METRICS."""
    pairs = [
        (
            {
                "ttft_ms_p50": 1000,
                "ttft_ms_p95": 1200,
                "total_ms_p50": 5000,
                "total_ms_p95": 6000,
                "decode_tps_p50": 30,
                "decode_tps_p95": 28,
            },
            {
                "ttft_ms_p50": 800,
                "ttft_ms_p95": 900,
                "total_ms_p50": 4500,
                "total_ms_p95": 5200,
                "decode_tps_p50": 45,
                "decode_tps_p95": 42,
            },
        )
        for _ in range(5)
    ]
    decisions = compare_runs(pairs)
    assert set(decisions.keys()) == set(DEFAULT_METRICS.keys())
    # All metrics show B as improvement (B is faster + higher throughput)
    for name, dec in decisions.items():
        assert dec.verdict is Verdict.IMPROVEMENT, name


def test_gate_is_deterministic_across_runs():
    """Same input → same verdict + same CI bounds (rng_seed pinned)."""
    a = [100.0, 110.0, 95.0, 105.0, 102.0]
    b = [80.0, 90.0, 75.0, 85.0, 82.0]
    d1 = compare_metric(
        metric_name="ttft_ms_p50",
        direction=Direction.LOWER_IS_BETTER,
        a=a,
        b=b,
    )
    d2 = compare_metric(
        metric_name="ttft_ms_p50",
        direction=Direction.LOWER_IS_BETTER,
        a=a,
        b=b,
    )
    assert d1.ci_low == d2.ci_low
    assert d1.ci_high == d2.ci_high
    assert d1.verdict == d2.verdict
