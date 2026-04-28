"""Opt-in pre-merge regression gate for ingest baselines (ADR 013).

Mirrors :mod:`tests.eval.latency.test_latency_floor` discipline: when
``JARVIS_INGEST_BENCH=1`` is set, validates the canonical baseline file
has a sensible shape. Until baseline-0 is captured and committed, the
test skips silently — a missing baseline isn't a failure, it's a "run
``run_bench`` once and commit the result" instruction.

The actual regression gate (compare current run to baseline-0 under
bootstrap CI) lives in the knob-loop chunk that produces baseline-1 and
will reuse :func:`tests.eval.ingest.gate.compare_runs`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ENABLED = os.environ.get("JARVIS_INGEST_BENCH") == "1"
BASELINE_DIR = Path(__file__).parent / "baselines"
CANONICAL_BASELINE = BASELINE_DIR / "baseline-0.json"


pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="JARVIS_INGEST_BENCH=1 not set; ingest floor gate skipped.",
)


@pytest.fixture(scope="module")
def baseline_doc() -> dict:
    if not CANONICAL_BASELINE.exists():
        pytest.skip(
            f"No canonical baseline at {CANONICAL_BASELINE}. "
            f"Run 'python -m tests.eval.ingest.run_bench' once and commit "
            f"its output to seed the floor test."
        )
    return json.loads(CANONICAL_BASELINE.read_text(encoding="utf-8"))


def test_canonical_baseline_has_machine_info(baseline_doc):
    info = baseline_doc.get("machine_info") or {}
    assert info.get("platform"), "machine_info.platform missing"
    assert info.get("model_label"), "machine_info.model_label missing"


def test_canonical_baseline_has_fixture_path(baseline_doc):
    md = baseline_doc.get("run_metadata") or {}
    assert md.get("fixture_path"), "run_metadata.fixture_path missing"


def test_canonical_baseline_records_at_least_one_scenario(baseline_doc):
    scenarios = baseline_doc.get("by_scenario") or []
    assert len(scenarios) >= 1, "no by_scenario entries in baseline"
    for s in scenarios:
        assert "total_ms_p50" in s
        assert "total_ms_p95" in s
        assert "stage" in s


def test_canonical_baseline_has_no_unexplained_errors(baseline_doc):
    """Every scenario should either complete cleanly, be intentionally
    skipped (skip_reason set), or document its error state."""
    scenarios = baseline_doc.get("by_scenario") or []
    for s in scenarios:
        if s.get("skip_reason"):
            continue
        n_errors = s.get("n_errors", 0)
        assert n_errors == 0, (
            f"scenario {s.get('scenario_name')!r} has {n_errors} errors; "
            f"baseline should not have been committed in this state."
        )
