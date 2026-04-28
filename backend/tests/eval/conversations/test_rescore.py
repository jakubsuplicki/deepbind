"""Tests for the rescore CLI (re-score an existing baseline without
re-running the model).

Covers the round trip: synthesize a minimal baseline-shaped JSON whose
``response_text`` contains Qwen3-style ``</think>`` chain-of-thought that
trips a fixture's ``must_not_contain`` guard, run it through ``rescore``,
and confirm the cleaned baseline reports CLEAN_PASS instead of
CONFABULATION. This pins the load-bearing behavior — the whole reason
the rescore exists is to undo guard false-positives produced by chain-
of-thought leakage.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.eval.conversations.rescore import rescore_baseline


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _baseline_skeleton(*, response_text: str, fixture_id: str = "long-conv-shallow") -> dict:
    """Build the smallest baseline JSON that exercises the rescore flow.

    Pinned to fixture #1 (long-conv-shallow) because its target turn at
    index 27 has both ``expected_facts`` (renewable/Poland/offshore-or-Baltic)
    and ``must_not_contain`` guards (solar, nuclear) — the exact shape
    where chain-of-thought false-positives showed up in the live run.
    """
    return {
        "run_metadata": {
            "chat_model_id": "ollama:qwen3:30b-a3b@seed=1",
            "fixture_count": 1,
            "fixture_ids": [fixture_id],
            "retrieval_enabled": False,
            "seed_count": 1,
            "seeds": [1],
            "strategies": ["full-history"],
            "timestamp_utc": "2026-04-27T15:43:15Z",
        },
        "overall": {
            "full-history": {
                "mean_clean_pass_rate": 0.0,
                "mean_confabulation_rate": 1.0,
            }
        },
        "gate_decisions": {},
        "by_strategy": {
            "full-history": [
                {
                    "fixture_id": fixture_id,
                    "strategy_name": "full-history",
                    "chat_model_id": "ollama:qwen3:30b-a3b@seed=1",
                    "target_turn_count": 1,
                    "seeds": [1],
                    "clean_pass_rate": 0.0,
                    "confabulation_rate": 1.0,
                    "stdev_clean_pass_rate": 0.0,
                    "severity_distribution": {
                        "clean_pass": 0.0,
                        "confabulation": 1.0,
                        "no_answer": 0.0,
                        "partial": 0.0,
                    },
                    "p50_latency_ms": 1000.0,
                    "p95_latency_ms": 1000.0,
                    "per_seed_clean_pass_rates": {"1": 0.0},
                    "turn_results": [
                        {
                            "turn_index": 27,
                            "seed": 1,
                            "response_text": response_text,
                            "severity": "confabulation",
                            "passed": False,
                            "facts_passed": [],
                            "facts_failed": [],
                            "guards_triggered": [],
                            "latency_ms": 1000.0,
                        }
                    ],
                }
            ]
        },
    }


def test_rescore_clears_false_positive_guard_on_thinking_text():
    """Chain-of-thought mentions a forbidden term ("nuclear" in the rejected-
    candidate list) but the final answer after </think> is correct. Pre-
    rescore the JSON records CONFABULATION; post-rescore it must record
    CLEAN_PASS, and the baseline's confab_rate must drop to zero."""
    response = (
        "Okay, the user's research was about renewable energy in Poland, "
        "specifically offshore wind in the Baltic — not solar or nuclear, "
        "those were rejected directions.\n</think>\n\n"
        "Your research is on offshore wind in the Baltic Sea, "
        "as part of a broader renewable energy study in Poland."
    )
    baseline = _baseline_skeleton(response_text=response)

    rescored = rescore_baseline(baseline, FIXTURES_DIR)

    fx_after = rescored["by_strategy"]["full-history"][0]
    assert fx_after["clean_pass_rate"] == 1.0
    assert fx_after["confabulation_rate"] == 0.0
    assert fx_after["turn_results"][0]["severity"] == "clean_pass"
    assert fx_after["turn_results"][0]["guards_triggered"] == []
    # Lineage marker so the rescored artifact is traceable
    assert "rescored_from_timestamp_utc" in rescored["run_metadata"]
    # Cleaned response_text actually got cleaned
    assert "</think>" not in fx_after["turn_results"][0]["response_text"]
    assert "rejected directions" not in fx_after["turn_results"][0]["response_text"]


def test_rescore_preserves_genuine_failure():
    """If the FINAL answer (after </think>) really violates a guard, rescore
    must still report CONFABULATION — the strip is a fix for false positives,
    not a way to launder real failures."""
    response = (
        "Okay, let me think.\n</think>\n\n"
        "Your research was about solar panels in Spain."  # wrong AND trips no_solar
    )
    baseline = _baseline_skeleton(response_text=response)

    rescored = rescore_baseline(baseline, FIXTURES_DIR)

    fx_after = rescored["by_strategy"]["full-history"][0]
    assert fx_after["confabulation_rate"] == 1.0
    assert fx_after["turn_results"][0]["severity"] == "confabulation"
    assert "no_solar" in fx_after["turn_results"][0]["guards_triggered"]


def test_rescore_keeps_response_text_when_no_thinking_tag():
    """Responses that never had a </think> tag (Anthropic, or already-cleaned
    text) must round-trip unchanged."""
    response = (
        "Your research is on offshore wind in the Baltic Sea, "
        "in the Poland renewable energy context."
    )
    baseline = _baseline_skeleton(response_text=response)

    rescored = rescore_baseline(baseline, FIXTURES_DIR)

    fx_after = rescored["by_strategy"]["full-history"][0]
    assert fx_after["turn_results"][0]["response_text"] == response
    assert fx_after["clean_pass_rate"] == 1.0


def test_rescore_raises_when_fixture_missing_from_dir():
    """If the baseline references a fixture id we can't load, fail loudly —
    silent skip would corrupt the aggregations."""
    baseline = _baseline_skeleton(
        response_text="anything",
        fixture_id="this-fixture-does-not-exist",
    )
    try:
        rescore_baseline(baseline, FIXTURES_DIR)
    except KeyError as exc:
        assert "this-fixture-does-not-exist" in str(exc)
    else:
        raise AssertionError("rescore_baseline should have raised KeyError")


def test_rescore_recomputes_gate_decisions_with_two_strategies():
    """When the input has both full-history and a naive-truncate variant,
    the rescore must regenerate the gate decision against the cleaned
    severities — not echo the original."""
    response_clean = (
        "Okay, thinking.\n</think>\n\n"
        "Renewable energy in Poland — offshore wind in the Baltic."
    )
    baseline = _baseline_skeleton(response_text=response_clean)

    # Add a second strategy mirroring the first, so adr_009_gate runs.
    baseline["by_strategy"]["naive-truncate-4"] = [
        {
            **baseline["by_strategy"]["full-history"][0],
            "strategy_name": "naive-truncate-4",
        }
    ]
    baseline["overall"]["naive-truncate-4"] = baseline["overall"]["full-history"]

    rescored = rescore_baseline(baseline, FIXTURES_DIR)

    assert "gate_decisions" in rescored
    assert "full-history__vs__naive-truncate-4" in rescored["gate_decisions"]
    decision = rescored["gate_decisions"][
        "full-history__vs__naive-truncate-4"
    ]["naive_vs_full_history"]
    # Both strategies pass cleanly → mean difference is zero, verdict equivalent
    assert decision["mean_difference"] == 0.0
    assert decision["verdict"] in ("equivalent", "insufficient_data")
