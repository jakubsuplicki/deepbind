"""Statistical decision gate for the conversation-replay eval (ADR 010).

Replaces the original "fixed-threshold" gate language ("if naive scores
within 5 points of full-history → amend ADR 009") with a bootstrap
confidence-interval rule. With N=10 fixtures the original threshold is
*one fixture flipping*; that's not enough signal to make an architectural
decision off. The bootstrap CI computes a 95% interval on the difference
between two strategies' per-seed pass rates, and the gate fires only
when the interval excludes zero (or whatever lower bound the caller
specifies).

What goes in: two ``MultiSeedFixtureResult`` lists (one per strategy),
covering the same fixtures and the same number of seeds.

What comes out: a ``GateDecision`` with the verdict and the supporting
numbers. The caller (CLI script in chunk 5) prints the result; the
human reads the numbers; the architectural call lands in an ADR
amendment if the gate fires.

Implementation note: the bootstrap is over per-seed pass rates per
fixture. Each "trial" is one seed's clean-pass rate on the joined-set
of (fixture, strategy). We resample fixtures with replacement to
produce the bootstrap distribution of mean pass-rate differences. This
is the standard pattern for "is the difference between two configs real
or noise" and runs in milliseconds on 10 fixtures × 5 seeds.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .runner import FixtureResult, MultiSeedFixtureResult


# ── Public types ─────────────────────────────────────────────────────────────


class Verdict(str, Enum):
    """Outcome of a strategy comparison gate.

    String values for stable JSON output in the CLI's gate-report file.
    """

    REGRESSION = "regression"
    """Strategy B is *worse* than strategy A by a CI-significant margin."""

    EQUIVALENT = "equivalent"
    """The 95% CI on the difference includes zero — no significant
    difference between the strategies. The cheaper one wins on parsimony."""

    IMPROVEMENT = "improvement"
    """Strategy B is *better* than strategy A by a CI-significant margin."""

    INSUFFICIENT_DATA = "insufficient_data"
    """Not enough fixtures or seeds to compute a meaningful CI."""


@dataclass(frozen=True)
class GateDecision:
    """The full record of a strategy-comparison gate run.

    Frozen so callers can stash it in a baseline file without later
    accidental mutation. JSON-serializable shape for the CLI.
    """

    strategy_a: str
    strategy_b: str
    fixture_count: int
    seed_count: int
    mean_pass_rate_a: float
    mean_pass_rate_b: float
    mean_difference: float  # B - A
    ci_low: float
    ci_high: float
    confidence: float  # e.g. 0.95
    verdict: Verdict
    rationale: str  # one-line human-readable explanation


# ── Implementation ───────────────────────────────────────────────────────────


def _per_fixture_mean_pass_rate(result: FixtureResult) -> float:
    """Mean clean-pass rate across all (seed, target_turn) cells for one
    fixture under one strategy. The bootstrap resamples over these
    fixture-level means."""
    return result.clean_pass_rate


def _bootstrap_ci_on_difference(
    rates_a: Sequence[float],
    rates_b: Sequence[float],
    *,
    iterations: int = 2000,
    confidence: float = 0.95,
    rng: random.Random,
) -> tuple[float, float]:
    """Paired-fixture bootstrap CI on (mean(b) - mean(a)).

    ``rates_a[i]`` and ``rates_b[i]`` must correspond to the same fixture
    so the resampling preserves the pairing — fixture-level variance is
    the dominant source of noise; we don't want to scramble it. Returns
    (low, high) bounds at the given confidence level.
    """
    if len(rates_a) != len(rates_b):
        raise ValueError(
            f"rates lists differ in length: {len(rates_a)} vs {len(rates_b)}"
        )
    if len(rates_a) == 0:
        raise ValueError("cannot bootstrap on empty rate lists")

    n = len(rates_a)
    diffs = []
    for _ in range(iterations):
        # Resample fixture indices with replacement; pairing preserved
        sample_idx = [rng.randrange(n) for _ in range(n)]
        sampled_a = [rates_a[i] for i in sample_idx]
        sampled_b = [rates_b[i] for i in sample_idx]
        diffs.append(statistics.mean(sampled_b) - statistics.mean(sampled_a))
    diffs.sort()
    alpha = (1 - confidence) / 2
    low_idx = int(alpha * iterations)
    high_idx = int((1 - alpha) * iterations) - 1
    return diffs[low_idx], diffs[high_idx]


def _classify_verdict(ci_low: float, ci_high: float) -> tuple[Verdict, str]:
    """Apply the standard CI-based decision rule.

    - CI entirely above zero → improvement
    - CI entirely below zero → regression
    - CI straddles zero → equivalent (no significant difference)
    """
    if ci_low > 0:
        return (
            Verdict.IMPROVEMENT,
            f"strategy B mean exceeds A by [{ci_low:+.3f}, {ci_high:+.3f}] at 95% CI; CI excludes zero.",
        )
    if ci_high < 0:
        return (
            Verdict.REGRESSION,
            f"strategy B mean trails A by [{ci_low:+.3f}, {ci_high:+.3f}] at 95% CI; CI excludes zero.",
        )
    return (
        Verdict.EQUIVALENT,
        f"95% CI on the difference is [{ci_low:+.3f}, {ci_high:+.3f}]; CI includes zero — no significant effect.",
    )


def compare_strategies(
    results_a: Sequence[FixtureResult],
    results_b: Sequence[FixtureResult],
    *,
    confidence: float = 0.95,
    iterations: int = 2000,
    rng_seed: int = 17,
    min_fixtures: int = 5,
    min_seeds_per_fixture: int = 1,
) -> GateDecision:
    """Compute the bootstrap-CI gate verdict for strategy A vs strategy B.

    ``results_a`` and ``results_b`` must cover the same fixtures in the
    same order — both are typically produced by running the same fixture
    list under each strategy. The bootstrap is paired by fixture index.

    ``rng_seed`` is fixed so two runs against the same input produce the
    same verdict — the eval is otherwise deterministic and we don't want
    the gate logic to introduce its own non-determinism.

    Returns ``Verdict.INSUFFICIENT_DATA`` (rather than raising) when the
    fixture count or seeds-per-fixture is below the configured floor;
    that's a "more data needed" signal, not a hard failure.
    """
    if len(results_a) != len(results_b):
        raise ValueError(
            f"strategy result lists differ in length: {len(results_a)} vs {len(results_b)}"
        )
    if not results_a:
        raise ValueError("cannot run gate on empty result lists")

    # Verify pairing by fixture id
    for a, b in zip(results_a, results_b):
        if a.fixture_id != b.fixture_id:
            raise ValueError(
                f"strategy result lists out of order: {a.fixture_id!r} vs {b.fixture_id!r}"
            )

    fixture_count = len(results_a)
    seed_count = len(results_a[0].seeds)
    rates_a = [_per_fixture_mean_pass_rate(r) for r in results_a]
    rates_b = [_per_fixture_mean_pass_rate(r) for r in results_b]
    mean_a = statistics.mean(rates_a)
    mean_b = statistics.mean(rates_b)

    if fixture_count < min_fixtures or seed_count < min_seeds_per_fixture:
        return GateDecision(
            strategy_a=results_a[0].strategy_name,
            strategy_b=results_b[0].strategy_name,
            fixture_count=fixture_count,
            seed_count=seed_count,
            mean_pass_rate_a=mean_a,
            mean_pass_rate_b=mean_b,
            mean_difference=mean_b - mean_a,
            ci_low=float("nan"),
            ci_high=float("nan"),
            confidence=confidence,
            verdict=Verdict.INSUFFICIENT_DATA,
            rationale=(
                f"insufficient data: have {fixture_count} fixtures × "
                f"{seed_count} seeds; need ≥{min_fixtures} fixtures × "
                f"≥{min_seeds_per_fixture} seeds for a CI-meaningful decision."
            ),
        )

    rng = random.Random(rng_seed)
    ci_low, ci_high = _bootstrap_ci_on_difference(
        rates_a,
        rates_b,
        iterations=iterations,
        confidence=confidence,
        rng=rng,
    )
    verdict, rationale = _classify_verdict(ci_low, ci_high)

    return GateDecision(
        strategy_a=results_a[0].strategy_name,
        strategy_b=results_b[0].strategy_name,
        fixture_count=fixture_count,
        seed_count=seed_count,
        mean_pass_rate_a=mean_a,
        mean_pass_rate_b=mean_b,
        mean_difference=mean_b - mean_a,
        ci_low=ci_low,
        ci_high=ci_high,
        confidence=confidence,
        verdict=verdict,
        rationale=rationale,
    )


# ── ADR-009 specific gate ────────────────────────────────────────────────────


def adr_009_gate(
    full_history: Sequence[FixtureResult],
    naive_truncate: Sequence[FixtureResult],
    retrieval_substitution: Sequence[FixtureResult] | None = None,
    *,
    confidence: float = 0.95,
) -> dict:
    """Apply ADR 010's named decision gate against ADR 009's stance.

    The gate has two questions:

    1. **Does naive recent-N truncation match full-history?** If yes
       (``equivalent`` verdict), ADR 009's retrieval-first stance must
       be revisited — naive is the cheaper viable strategy.
    2. **If retrieval-substitution results are provided, does retrieval
       beat naive truncation?** If yes (``improvement``), ADR 009 stands.
       If equivalent or regression, retrieval-substitution is not
       carrying its complexity.

    Returns a dict with each sub-question's GateDecision (frozen
    dataclasses are JSON-friendly via dataclasses.asdict at the call
    site). The caller decides what to do with it; this function does
    not amend ADRs.
    """
    naive_vs_full = compare_strategies(
        full_history,
        naive_truncate,
        confidence=confidence,
    )
    out = {
        "naive_vs_full_history": naive_vs_full,
    }
    if retrieval_substitution is not None:
        retrieval_vs_naive = compare_strategies(
            naive_truncate,
            retrieval_substitution,
            confidence=confidence,
        )
        out["retrieval_vs_naive"] = retrieval_vs_naive

    return out
