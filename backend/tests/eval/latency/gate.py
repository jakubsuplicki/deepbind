"""Bootstrap-CI regression gate for latency baselines (ADR 011).

Same statistical floor as ``conversations/gate.py``: paired bootstrap
confidence interval on the difference between two runs. Adapted to numeric
metrics (TTFT, total_ms, decode_tps) where lower-is-better differs by metric.

The conversations gate operates on per-fixture clean_pass_rate (higher
better). Latency operates on per-(model, scenario) metric values where:
- ``ttft_ms``, ``total_ms`` — lower is better; an improvement in B over A
  shows as a *negative* mean difference.
- ``decode_tps`` — higher is better; an improvement shows as positive.

The gate flips the verdict semantics based on the metric's polarity, so
"improvement" / "regression" / "equivalent" carry the same human meaning
regardless of metric direction.

Inputs are pre-extracted paired float lists. The caller (typically
``run_bench`` or a future ``compare_baselines.py`` script) is responsible
for matching scenarios across the two runs by ``(model_id, scenario_name)``.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class Verdict(str, Enum):
    """Same shape as ``conversations.gate.Verdict``."""

    REGRESSION = "regression"
    EQUIVALENT = "equivalent"
    IMPROVEMENT = "improvement"
    INSUFFICIENT_DATA = "insufficient_data"


class Direction(str, Enum):
    """Whether lower or higher values represent improvement.

    String values for clean JSON output. Stable across versions.
    """

    LOWER_IS_BETTER = "lower_is_better"
    HIGHER_IS_BETTER = "higher_is_better"


@dataclass(frozen=True)
class GateDecision:
    """Outcome of comparing two paired metric series."""

    metric_name: str
    direction: Direction
    n_pairs: int
    mean_a: float
    mean_b: float
    mean_difference: float  # B - A
    ci_low: float
    ci_high: float
    confidence: float
    verdict: Verdict
    rationale: str


def _bootstrap_ci(
    a: Sequence[float],
    b: Sequence[float],
    *,
    iterations: int,
    confidence: float,
    rng: random.Random,
) -> tuple[float, float]:
    """Paired-by-index bootstrap CI on (mean(b) - mean(a)).

    The pairing matters: a[i] and b[i] must correspond to the same
    (model_id, scenario_name) cell so resampling preserves cell-level
    variance. Without pairing the CI gets noisier and harder to interpret.
    """
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} vs {len(b)}")
    if not a:
        raise ValueError("cannot bootstrap on empty inputs")

    n = len(a)
    diffs: list[float] = []
    for _ in range(iterations):
        idx = [rng.randrange(n) for _ in range(n)]
        sa = [a[i] for i in idx]
        sb = [b[i] for i in idx]
        diffs.append(statistics.mean(sb) - statistics.mean(sa))
    diffs.sort()
    alpha = (1 - confidence) / 2
    low = diffs[int(alpha * iterations)]
    high = diffs[int((1 - alpha) * iterations) - 1]
    return low, high


def _classify(
    *,
    ci_low: float,
    ci_high: float,
    direction: Direction,
) -> tuple[Verdict, str]:
    """Map a CI on ``B - A`` to an improvement / regression / equivalent verdict.

    For ``LOWER_IS_BETTER``: improvement means B < A, so mean_diff < 0.
    For ``HIGHER_IS_BETTER``: improvement means B > A, so mean_diff > 0.
    The CI excluding zero in the right direction is the gate.
    """
    if ci_low <= 0 <= ci_high:
        return (
            Verdict.EQUIVALENT,
            f"95% CI on (B - A) is [{ci_low:+.3f}, {ci_high:+.3f}]; "
            f"includes zero — no significant effect.",
        )

    if direction is Direction.LOWER_IS_BETTER:
        if ci_high < 0:
            return (
                Verdict.IMPROVEMENT,
                f"B is faster than A by [{-ci_high:+.3f}, {-ci_low:+.3f}] units "
                f"(95% CI on -(B-A)); CI excludes zero.",
            )
        return (
            Verdict.REGRESSION,
            f"B is slower than A by [{ci_low:+.3f}, {ci_high:+.3f}] units (95% CI); "
            f"CI excludes zero.",
        )

    # HIGHER_IS_BETTER
    if ci_low > 0:
        return (
            Verdict.IMPROVEMENT,
            f"B exceeds A by [{ci_low:+.3f}, {ci_high:+.3f}] units (95% CI); "
            f"CI excludes zero.",
        )
    return (
        Verdict.REGRESSION,
        f"B trails A by [{ci_low:+.3f}, {ci_high:+.3f}] units (95% CI); "
        f"CI excludes zero.",
    )


def compare_metric(
    *,
    metric_name: str,
    direction: Direction,
    a: Sequence[float],
    b: Sequence[float],
    confidence: float = 0.95,
    iterations: int = 2000,
    rng_seed: int = 17,
    min_pairs: int = 3,
) -> GateDecision:
    """Compare two paired metric series under a bootstrap CI.

    ``a`` and ``b`` must be the same length and pair-aligned. Typical
    usage: extract per-cell ``ttft_ms_p50`` from baseline-N and baseline-(N+1),
    matched by ``(model_id, scenario_name)``.

    Returns ``Verdict.INSUFFICIENT_DATA`` when fewer than ``min_pairs``
    cells are available — that's a "more data needed" signal, not a hard
    failure. The default of 3 pairs is the lowest a paired bootstrap CI
    can produce a meaningful interval from.
    """
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} vs {len(b)}")
    n = len(a)

    mean_a = statistics.mean(a) if a else 0.0
    mean_b = statistics.mean(b) if b else 0.0

    if n < min_pairs:
        return GateDecision(
            metric_name=metric_name,
            direction=direction,
            n_pairs=n,
            mean_a=mean_a,
            mean_b=mean_b,
            mean_difference=mean_b - mean_a,
            ci_low=float("nan"),
            ci_high=float("nan"),
            confidence=confidence,
            verdict=Verdict.INSUFFICIENT_DATA,
            rationale=(
                f"insufficient data: have {n} pairs; need ≥{min_pairs} for a "
                f"CI-meaningful decision."
            ),
        )

    rng = random.Random(rng_seed)
    ci_low, ci_high = _bootstrap_ci(
        a, b, iterations=iterations, confidence=confidence, rng=rng
    )
    verdict, rationale = _classify(
        ci_low=ci_low, ci_high=ci_high, direction=direction
    )

    return GateDecision(
        metric_name=metric_name,
        direction=direction,
        n_pairs=n,
        mean_a=mean_a,
        mean_b=mean_b,
        mean_difference=mean_b - mean_a,
        ci_low=ci_low,
        ci_high=ci_high,
        confidence=confidence,
        verdict=verdict,
        rationale=rationale,
    )


# Convenience: standard metric comparisons applied to a paired-cells list.

DEFAULT_METRICS: dict[str, Direction] = {
    "ttft_ms_p50": Direction.LOWER_IS_BETTER,
    "ttft_ms_p95": Direction.LOWER_IS_BETTER,
    "total_ms_p50": Direction.LOWER_IS_BETTER,
    "total_ms_p95": Direction.LOWER_IS_BETTER,
    "decode_tps_p50": Direction.HIGHER_IS_BETTER,
    "decode_tps_p95": Direction.HIGHER_IS_BETTER,
}


def compare_runs(
    pairs: Sequence[tuple[dict, dict]],
    *,
    metrics: dict[str, Direction] = DEFAULT_METRICS,
    confidence: float = 0.95,
) -> dict[str, GateDecision]:
    """Apply ``compare_metric`` across every standard metric.

    ``pairs`` is a list of ``(stats_a, stats_b)`` tuples, where each side is
    the dict-form of a :class:`runner.ScenarioStats` (matched by
    ``model_id`` + ``scenario_name``). Returns one GateDecision per metric.
    """
    out: dict[str, GateDecision] = {}
    for metric_name, direction in metrics.items():
        a_vals = [float(sa.get(metric_name, 0.0)) for sa, _ in pairs]
        b_vals = [float(sb.get(metric_name, 0.0)) for _, sb in pairs]
        out[metric_name] = compare_metric(
            metric_name=metric_name,
            direction=direction,
            a=a_vals,
            b=b_vals,
            confidence=confidence,
        )
    return out
