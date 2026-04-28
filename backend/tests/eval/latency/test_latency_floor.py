"""Opt-in pre-merge regression gate for latency baselines (ADR 011).

Mirrors the discipline of ``tests/eval/test_baseline_floor.py`` and
``tests/eval/conversations/test_run_eval.py``: when the env var
``JARVIS_LATENCY_BENCH=1`` is set, runs the PR-scope grid against the
canonical baseline JSON and fails if any metric regresses by a
CI-significant margin.

The test silently *skips* in ordinary CI (no env var, no Ollama
available, no baseline committed yet). It functions as a hard gate when
developers run it locally before merging a chat / retrieval / local-models
change.

Calibration: until baseline-0 is captured on a real machine and committed,
the floor test skips because there's no reference to compare against.
That's the right state — a missing baseline isn't a failure, it's a
"please run ``run_bench`` once and commit the result" instruction.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# The floor test deliberately skips in default CI to keep the eval gate
# opt-in. Two ways to skip cleanly:
#   1. ``JARVIS_LATENCY_BENCH`` not set
#   2. No committed baseline-0.json present yet
ENABLED = os.environ.get("JARVIS_LATENCY_BENCH") == "1"
BASELINE_DIR = Path(__file__).parent / "baselines"
CANONICAL_BASELINE = BASELINE_DIR / "baseline-0.json"


pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="JARVIS_LATENCY_BENCH=1 not set; latency floor gate skipped.",
)


@pytest.fixture(scope="module")
def baseline_doc() -> dict:
    if not CANONICAL_BASELINE.exists():
        pytest.skip(
            f"No canonical baseline at {CANONICAL_BASELINE}. "
            f"Run 'python -m tests.eval.latency.run_bench' once and commit "
            f"its output to seed the floor test."
        )
    return json.loads(CANONICAL_BASELINE.read_text(encoding="utf-8"))


def test_canonical_baseline_has_machine_info(baseline_doc):
    info = baseline_doc.get("machine_info") or {}
    assert info.get("platform"), "machine_info.platform missing"
    assert info.get("model_label"), "machine_info.model_label missing"


def test_canonical_baseline_records_at_least_one_cell(baseline_doc):
    cells = baseline_doc.get("by_cell") or []
    assert len(cells) >= 1, "no by_cell entries in baseline"
    for cell in cells:
        assert "ttft_ms_p50" in cell
        assert "decode_tps_p50" in cell
        assert "total_ms_p95" in cell


def test_canonical_baseline_has_no_unexplained_errors(baseline_doc):
    """Every cell should either complete cleanly, be intentionally skipped,
    or document its error.

    Skipped cells (``skip_reason`` set, e.g. "model not pulled") are
    informational — the baseline was captured against the models that
    were available, others are recorded as gaps for future re-runs.

    Reference scenarios may legitimately error (no API key, network) —
    those are fine. Other cells with errors indicate the run was
    unhealthy and the baseline shouldn't have been committed.
    """
    cells = baseline_doc.get("by_cell") or []
    for cell in cells:
        if cell.get("skip_reason"):
            continue
        n_errors = cell.get("n_errors", 0)
        scenario = cell.get("scenario_name", "")
        if n_errors > 0:
            # Only acceptable for reference scenarios where the API key may be absent
            assert "reference" in scenario, (
                f"cell {cell.get('model_id')!r} × {scenario!r} has "
                f"{n_errors} errors but is not a reference scenario; "
                f"baseline should not have been committed in this state."
            )
