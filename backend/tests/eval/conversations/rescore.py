"""Re-score an existing baseline JSON without re-running the model.

Purpose
=======

The first 30B run captured raw ``response_text`` containing Qwen3 chain-of-
thought prose (Ollama 0.18 with ``think: false`` strips the opening
``<think>`` tag but leaves the rest). The scorer's ``must_not_contain``
guards then matched against thinking text, producing false-positive
``CONFABULATION`` severities even on correct final answers (e.g. fixture
``code-domain-debugging`` where the model thinks "the user wants
finalize_invoice, not rollback_to_draft" — both names get scored).

The adapter now strips chain-of-thought before returning to the runner,
but that fixes only future runs. This module fixes existing baselines
in-place: load the JSON, strip ``</think>`` blocks from each
``response_text``, re-run the scorer against the matching fixture turn,
and write a fresh baseline JSON with corrected severities, aggregations,
and gate decisions.

Why a separate tool, not a re-run
=================================

A 30B grid takes ~45 min – 2 h. Re-scoring the same captured responses
takes <1 s and produces an identical answer to "what would the metrics
look like if we'd had the strip in place." This is the right way to
correct a scoring-layer bug — the model output is fixed, only our
interpretation of it changed.

Output shape matches ``run_eval.py``'s baseline format exactly so the
rescored file is a drop-in replacement for the original.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .chat_adapters import _strip_thinking
from .gate import adr_009_gate
from .runner import (
    FixtureResult,
    MultiSeedFixtureResult,
    TurnResult,
    load_fixture,
)
from .run_eval import build_output, discover_fixtures
from .scorer import score_turn


# ── Rescoring core ───────────────────────────────────────────────────────────


def _rescore_turn_result(
    turn_json: dict,
    fixture: dict,
) -> TurnResult:
    """Rebuild one ``TurnResult`` from JSON, with thinking stripped + re-scored.

    Looks up the matching ``assistant_target`` turn by ``turn_index`` in the
    fixture's turn list, strips thinking from the recorded ``response_text``,
    and runs the scorer against the cleaned text. The latency and seed
    are preserved from the original record (re-scoring doesn't replay).
    """
    turn_index = turn_json["turn_index"]
    target_turn = fixture["turns"][turn_index]
    if target_turn.get("role") != "assistant_target":
        raise ValueError(
            f"fixture {fixture['id']!r} turn {turn_index} is not assistant_target "
            f"(role={target_turn.get('role')!r}); JSON may be from a different fixture set"
        )

    cleaned_response = _strip_thinking(turn_json["response_text"])
    fresh_score = score_turn(target_turn, cleaned_response)

    return TurnResult(
        turn_index=turn_index,
        seed=turn_json["seed"],
        response_text=cleaned_response,
        score=fresh_score,
        latency_ms=turn_json["latency_ms"],
    )


def _rescore_fixture_result(
    fixture_json: dict,
    fixtures_by_id: dict[str, dict],
) -> MultiSeedFixtureResult:
    fixture_id = fixture_json["fixture_id"]
    if fixture_id not in fixtures_by_id:
        raise KeyError(
            f"fixture {fixture_id!r} referenced in baseline but not found in fixtures dir"
        )
    fixture = fixtures_by_id[fixture_id]

    turn_results = [
        _rescore_turn_result(t, fixture) for t in fixture_json["turn_results"]
    ]

    return MultiSeedFixtureResult(
        fixture_id=fixture_id,
        strategy_name=fixture_json["strategy_name"],
        chat_model_id=fixture_json["chat_model_id"],
        target_turn_count=fixture_json["target_turn_count"],
        seeds=list(fixture_json["seeds"]),
        turn_results=turn_results,
    )


def rescore_baseline(
    baseline_json: dict,
    fixtures_dir: Path,
) -> dict:
    """Take a baseline JSON dict + the fixtures dir, return a fresh baseline.

    The returned dict has the same shape as ``run_eval.build_output``, so
    callers can write it back over the original (or to a new path) and
    diff against the previous run to see what the strip changed.
    """
    fixture_paths = discover_fixtures(fixtures_dir)
    fixtures_by_id: dict[str, dict] = {}
    for path in fixture_paths:
        fx = load_fixture(path)
        fixtures_by_id[fx["id"]] = fx

    by_strategy_in = baseline_json["by_strategy"]
    rescored: dict[str, list[FixtureResult]] = {}
    for strategy_name, fx_list in by_strategy_in.items():
        rescored[strategy_name] = [
            _rescore_fixture_result(fx_json, fixtures_by_id) for fx_json in fx_list
        ]

    md = baseline_json["run_metadata"]
    out = build_output(
        results=rescored,
        chat_model_id=md["chat_model_id"],
        seeds=md["seeds"],
        fixture_ids=md["fixture_ids"],
        retrieval_enabled=md["retrieval_enabled"],
    )
    # Mark this baseline as a rescore so the artifact's lineage is unambiguous.
    out["run_metadata"]["rescored_from_timestamp_utc"] = md["timestamp_utc"]
    out["run_metadata"]["rescored_at_utc"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rescore",
        description=(
            "Re-score an existing conversation-eval baseline JSON with the "
            "current scorer + thinking-strip applied. Does not re-run the model."
        ),
    )
    p.add_argument("input", help="Path to the baseline JSON to re-score.")
    p.add_argument(
        "--out",
        default=None,
        help="Output path. Default: '<input>.rescored.json'.",
    )
    p.add_argument(
        "--fixtures-dir",
        default="tests/eval/conversations/fixtures",
        help="Path to the fixtures directory (must match the original run).",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    in_path = Path(args.input)
    out_path = Path(args.out) if args.out else in_path.with_suffix(".rescored.json")

    baseline_in = json.loads(in_path.read_text(encoding="utf-8"))
    rescored = rescore_baseline(baseline_in, Path(args.fixtures_dir))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(rescored, indent=2, sort_keys=True), encoding="utf-8"
    )

    print(f"rescored baseline written: {out_path}")
    print()
    print("delta vs original:")
    for strategy_name in sorted(rescored["overall"].keys()):
        before = baseline_in["overall"][strategy_name]
        after = rescored["overall"][strategy_name]
        d_pass = after["mean_clean_pass_rate"] - before["mean_clean_pass_rate"]
        d_conf = after["mean_confabulation_rate"] - before["mean_confabulation_rate"]
        print(
            f"  {strategy_name:<22} clean_pass {before['mean_clean_pass_rate']:.3f}"
            f" → {after['mean_clean_pass_rate']:.3f} ({d_pass:+.3f}) | "
            f"confab {before['mean_confabulation_rate']:.3f}"
            f" → {after['mean_confabulation_rate']:.3f} ({d_conf:+.3f})"
        )

    print()
    print("rescored gate decisions:")
    for label, decisions in sorted(rescored["gate_decisions"].items()):
        for sub_label, decision in sorted(decisions.items()):
            print(
                f"  {label} ({sub_label}): {decision['verdict']} "
                f"Δ={decision['mean_difference']:+.3f} "
                f"95% CI [{decision['ci_low']:+.3f}, {decision['ci_high']:+.3f}]"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
