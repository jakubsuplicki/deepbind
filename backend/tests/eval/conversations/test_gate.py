"""Tests for the bootstrap-CI gate logic (ADR 010, chunk 2)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tests.eval.conversations.gate import (
    GateDecision,
    Verdict,
    _bootstrap_ci_on_difference,
    _classify_verdict,
    adr_009_gate,
    compare_strategies,
)
from tests.eval.conversations.runner import FixtureResult, TurnResult
from tests.eval.conversations.scorer import (
    FactCheckResult,
    Severity,
    TurnScore,
)


def _result(
    fixture_id: str,
    strategy: str,
    *,
    per_seed_pass: list[bool],
) -> FixtureResult:
    """Build a synthetic FixtureResult with one assistant_target turn per
    seed; each seed either passes (CLEAN_PASS) or fails (NO_ANSWER)."""
    seeds = list(range(1, len(per_seed_pass) + 1))
    turns = [
        TurnResult(
            turn_index=0,
            seed=seed,
            response_text=("ok" if passed else "no"),
            score=TurnScore(
                severity=Severity.CLEAN_PASS if passed else Severity.NO_ANSWER,
                facts=[FactCheckResult("a", passed)],
                guards=[],
            ),
            latency_ms=1.0,
        )
        for seed, passed in zip(seeds, per_seed_pass)
    ]
    return FixtureResult(
        fixture_id=fixture_id,
        strategy_name=strategy,
        chat_model_id="stub",
        target_turn_count=1,
        seeds=seeds,
        turn_results=turns,
    )


# ── _classify_verdict ────────────────────────────────────────────────────────


def test_classify_verdict_improvement_when_ci_above_zero():
    verdict, rationale = _classify_verdict(0.05, 0.20)
    assert verdict is Verdict.IMPROVEMENT
    assert "exceeds" in rationale.lower()


def test_classify_verdict_regression_when_ci_below_zero():
    verdict, rationale = _classify_verdict(-0.30, -0.05)
    assert verdict is Verdict.REGRESSION
    assert "trails" in rationale.lower()


def test_classify_verdict_equivalent_when_ci_includes_zero():
    verdict, rationale = _classify_verdict(-0.05, 0.05)
    assert verdict is Verdict.EQUIVALENT
    assert "no significant" in rationale.lower()


def test_classify_verdict_treats_exactly_zero_as_equivalent():
    """CI bound exactly at zero is borderline; the rule is "excludes zero"
    — equality counts as inclusion. Pin the convention."""
    verdict, _ = _classify_verdict(0.0, 0.10)
    assert verdict is Verdict.EQUIVALENT
    verdict2, _ = _classify_verdict(-0.10, 0.0)
    assert verdict2 is Verdict.EQUIVALENT


# ── _bootstrap_ci_on_difference ──────────────────────────────────────────────


def test_bootstrap_ci_includes_zero_when_strategies_equivalent():
    """Two identical rate distributions must produce a CI that straddles
    zero — there's no real difference to detect."""
    import random
    rng = random.Random(1)
    rates = [0.7, 0.6, 0.8, 0.9, 0.5, 0.7, 0.6, 0.7, 0.8, 0.6]
    low, high = _bootstrap_ci_on_difference(rates, list(rates), rng=rng)
    assert low <= 0 <= high


def test_bootstrap_ci_strongly_positive_when_b_clearly_better():
    """If B's rates are uniformly better than A's, the CI on (B - A) must
    sit entirely above zero."""
    import random
    rng = random.Random(2)
    a = [0.2, 0.3, 0.1, 0.2, 0.3, 0.2, 0.3, 0.1, 0.2, 0.3]
    b = [0.9, 0.8, 0.9, 1.0, 0.9, 0.8, 0.9, 1.0, 0.9, 0.8]
    low, high = _bootstrap_ci_on_difference(a, b, rng=rng)
    assert low > 0
    assert high > low


def test_bootstrap_ci_strongly_negative_when_b_clearly_worse():
    import random
    rng = random.Random(3)
    a = [0.9, 0.8, 0.9, 1.0, 0.9]
    b = [0.2, 0.3, 0.1, 0.2, 0.3]
    low, high = _bootstrap_ci_on_difference(a, b, rng=rng)
    assert high < 0


def test_bootstrap_ci_rejects_mismatched_lengths():
    import random
    with pytest.raises(ValueError, match="differ in length"):
        _bootstrap_ci_on_difference([0.5], [0.5, 0.5], rng=random.Random(0))


def test_bootstrap_ci_rejects_empty():
    import random
    with pytest.raises(ValueError, match="empty"):
        _bootstrap_ci_on_difference([], [], rng=random.Random(0))


def test_bootstrap_ci_is_deterministic_under_fixed_seed():
    """Same input + same seed → same CI bounds. Required for reproducible
    gate verdicts across runs."""
    import random
    rates = [0.5, 0.7, 0.4, 0.8]
    other = [0.6, 0.7, 0.5, 0.8]
    a = _bootstrap_ci_on_difference(rates, other, rng=random.Random(42))
    b = _bootstrap_ci_on_difference(rates, other, rng=random.Random(42))
    assert a == b


# ── compare_strategies (the public entry point) ──────────────────────────────


def test_compare_strategies_equivalent_when_pass_rates_match():
    """Two strategies with identical per-fixture pass rates → equivalent."""
    results_a = [
        _result(f"fx{i}", "full-history", per_seed_pass=[True, True, False, True, True])
        for i in range(10)
    ]
    results_b = [
        _result(f"fx{i}", "naive-truncate-8", per_seed_pass=[True, True, False, True, True])
        for i in range(10)
    ]
    decision = compare_strategies(results_a, results_b)
    assert decision.verdict is Verdict.EQUIVALENT
    assert decision.mean_pass_rate_a == decision.mean_pass_rate_b
    assert decision.fixture_count == 10
    assert decision.seed_count == 5


def test_compare_strategies_regression_when_b_clearly_worse():
    """Strategy A passes everything, B fails everything — verdict must
    be regression with negative mean_difference."""
    results_a = [
        _result(f"fx{i}", "full-history", per_seed_pass=[True] * 5)
        for i in range(10)
    ]
    results_b = [
        _result(f"fx{i}", "naive-truncate-8", per_seed_pass=[False] * 5)
        for i in range(10)
    ]
    decision = compare_strategies(results_a, results_b)
    assert decision.verdict is Verdict.REGRESSION
    assert decision.mean_difference < 0
    assert decision.ci_high < 0


def test_compare_strategies_improvement_when_b_clearly_better():
    results_a = [
        _result(f"fx{i}", "naive-truncate-8", per_seed_pass=[False] * 5)
        for i in range(10)
    ]
    results_b = [
        _result(f"fx{i}", "retrieval-substitution-v1", per_seed_pass=[True] * 5)
        for i in range(10)
    ]
    decision = compare_strategies(results_a, results_b)
    assert decision.verdict is Verdict.IMPROVEMENT
    assert decision.mean_difference > 0


def test_compare_strategies_insufficient_data_below_floor():
    """With fewer than the minimum fixtures, the gate must refuse to
    issue a verdict rather than fabricating one off insufficient data."""
    results_a = [_result(f"fx{i}", "A", per_seed_pass=[True] * 3) for i in range(2)]
    results_b = [_result(f"fx{i}", "B", per_seed_pass=[True] * 3) for i in range(2)]
    decision = compare_strategies(results_a, results_b, min_fixtures=5)
    assert decision.verdict is Verdict.INSUFFICIENT_DATA
    assert "insufficient" in decision.rationale.lower()


def test_compare_strategies_rejects_mismatched_fixture_lists():
    a = [_result("fx1", "A", per_seed_pass=[True])]
    b = [_result("fx2", "B", per_seed_pass=[True])]  # different fixture id
    with pytest.raises(ValueError, match="out of order"):
        compare_strategies(a, b)


def test_compare_strategies_rejects_empty_input():
    with pytest.raises(ValueError, match="empty"):
        compare_strategies([], [])


def test_compare_strategies_rejects_length_mismatch():
    a = [_result("fx1", "A", per_seed_pass=[True])]
    b = [_result(f"fx{i}", "B", per_seed_pass=[True]) for i in range(2)]
    with pytest.raises(ValueError, match="differ in length"):
        compare_strategies(a, b)


def test_compare_strategies_decision_is_deterministic():
    results_a = [_result(f"fx{i}", "A", per_seed_pass=[True, False, True]) for i in range(8)]
    results_b = [_result(f"fx{i}", "B", per_seed_pass=[False, True, False]) for i in range(8)]
    decision_1 = compare_strategies(results_a, results_b)
    decision_2 = compare_strategies(results_a, results_b)
    assert decision_1 == decision_2


def test_gate_decision_is_frozen():
    """GateDecision is a frozen dataclass — protects against mutation
    after baselining."""
    decision = compare_strategies(
        [_result(f"fx{i}", "A", per_seed_pass=[True]) for i in range(5)],
        [_result(f"fx{i}", "B", per_seed_pass=[True]) for i in range(5)],
    )
    with pytest.raises(Exception):
        decision.verdict = Verdict.IMPROVEMENT  # type: ignore[misc]


# ── adr_009_gate convenience ─────────────────────────────────────────────────


def test_adr_009_gate_without_retrieval_returns_naive_vs_full_only():
    full = [_result(f"fx{i}", "full-history", per_seed_pass=[True] * 5) for i in range(10)]
    naive = [_result(f"fx{i}", "naive-truncate-8", per_seed_pass=[True] * 5) for i in range(10)]
    out = adr_009_gate(full, naive)
    assert "naive_vs_full_history" in out
    assert "retrieval_vs_naive" not in out


def test_adr_009_gate_with_all_three_strategies():
    full = [_result(f"fx{i}", "full-history", per_seed_pass=[True] * 5) for i in range(10)]
    naive = [_result(f"fx{i}", "naive-truncate-8", per_seed_pass=[False] * 5) for i in range(10)]
    retrieval = [
        _result(f"fx{i}", "retrieval-substitution-v1", per_seed_pass=[True] * 5)
        for i in range(10)
    ]
    out = adr_009_gate(full, naive, retrieval)
    assert "naive_vs_full_history" in out
    assert "retrieval_vs_naive" in out
    # naive is worse than full → regression
    assert out["naive_vs_full_history"].verdict is Verdict.REGRESSION
    # retrieval is better than naive → improvement
    assert out["retrieval_vs_naive"].verdict is Verdict.IMPROVEMENT


def test_adr_009_gate_named_scenario_naive_is_enough():
    """Decision-gate scenario A: naive matches full-history → ADR 009
    needs revisiting. The verdict must be EQUIVALENT for naive_vs_full,
    which is the signal "the cheaper baseline is good enough"."""
    full = [_result(f"fx{i}", "full-history", per_seed_pass=[True, True, False, True, True]) for i in range(10)]
    naive = [_result(f"fx{i}", "naive-truncate-8", per_seed_pass=[True, True, False, True, True]) for i in range(10)]
    out = adr_009_gate(full, naive)
    assert out["naive_vs_full_history"].verdict is Verdict.EQUIVALENT


def test_adr_009_gate_named_scenario_naive_breaks_compaction():
    """Decision-gate scenario B: naive truncation drops a lot — ADR 009's
    retrieval plan is justified. The verdict must be REGRESSION for
    naive_vs_full."""
    full = [_result(f"fx{i}", "full-history", per_seed_pass=[True] * 5) for i in range(10)]
    naive = [_result(f"fx{i}", "naive-truncate-8", per_seed_pass=[False] * 5) for i in range(10)]
    out = adr_009_gate(full, naive)
    assert out["naive_vs_full_history"].verdict is Verdict.REGRESSION
