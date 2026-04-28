"""Bootstrap-CI regression gate for ingest baselines (ADR 013).

Reuses the paired-bootstrap CI from ``tests.eval.latency.gate`` — same
statistical floor, same Verdict enum, same Direction enum. Only the
``DEFAULT_METRICS`` table differs: ingest stages are all
LOWER_IS_BETTER (no decode_tps analog), and the metrics include the
per-stage durations plus the total.

Caller responsibility: pair up cells across two baselines by
``scenario_name``, pull each metric series into paired float lists,
hand them to :func:`compare_metric`. Same shape as the latency
``compare_runs``.
"""

from __future__ import annotations

from typing import Sequence

# Re-export the gate primitives — single source of truth lives in latency.gate
from ..latency.gate import (  # noqa: F401 — re-exported by design
    Direction,
    GateDecision,
    Verdict,
    compare_metric,
)


# ── Ingest-specific metric table ───────────────────────────────────────────


DEFAULT_METRICS: dict[str, Direction] = {
    "total_ms_p50": Direction.LOWER_IS_BETTER,
    "total_ms_p95": Direction.LOWER_IS_BETTER,
    "total_ms_mean": Direction.LOWER_IS_BETTER,
}
"""Per-scenario aggregate metrics — apply across every paired scenario.

Stage-level metrics are extracted ad-hoc by the caller (see
:func:`compare_stage_metric`) because the metric name varies with stage."""


def compare_runs(
    pairs: Sequence[tuple[dict, dict]],
    *,
    metrics: dict[str, Direction] = DEFAULT_METRICS,
    confidence: float = 0.95,
) -> dict[str, GateDecision]:
    """Apply ``compare_metric`` across the standard scenario-aggregate metrics.

    ``pairs`` is a list of ``(stats_a, stats_b)`` tuples, where each
    side is the dict-form of a :class:`runner.ScenarioStats` matched by
    ``scenario_name`` across the two baselines. Returns one
    :class:`GateDecision` per metric.
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


def compare_stage_metric(
    pairs: Sequence[tuple[dict, dict]],
    *,
    stage_name: str,
    metric: str = "p50_ms",
    direction: Direction = Direction.LOWER_IS_BETTER,
    confidence: float = 0.95,
) -> GateDecision:
    """Compare a single stage's metric across paired scenario rows.

    Each pair element exposes a ``stage_stats`` list of dicts; this
    helper picks the matching stage by ``name`` and pulls
    ``metric`` (default p50_ms). Cells where the stage isn't present
    are dropped from the comparison (different scenarios may include
    different stages).
    """
    a_vals: list[float] = []
    b_vals: list[float] = []
    for sa, sb in pairs:
        a_stage = next(
            (s for s in sa.get("stage_stats", []) if s.get("name") == stage_name),
            None,
        )
        b_stage = next(
            (s for s in sb.get("stage_stats", []) if s.get("name") == stage_name),
            None,
        )
        if a_stage is None or b_stage is None:
            continue
        a_vals.append(float(a_stage.get(metric, 0.0)))
        b_vals.append(float(b_stage.get(metric, 0.0)))

    return compare_metric(
        metric_name=f"{stage_name}.{metric}",
        direction=direction,
        a=a_vals,
        b=b_vals,
        confidence=confidence,
    )
