"""Conversation-eval CLI — capture baselines and run the gate (ADR 010).

Usage from the project's backend dir::

    .venv/bin/python -m tests.eval.conversations.run_eval \\
        --strategies full-history,naive-truncate-4,naive-truncate-8,naive-truncate-12 \\
        --provider ollama \\
        --model qwen3:30b-a3b \\
        --seeds 1,2,3 \\
        --fixtures-dir tests/eval/conversations/fixtures \\
        --out tests/eval/conversations/baselines/run.json

What it does
============

1. Loads every ``*.json`` fixture (excluding ``*.tools.json``) from the
   fixtures directory.
2. For each fixture × strategy × seed, calls ``run_fixture`` with the
   chat callable produced by ``make_chat_factory``. Multi-seed runs
   produce variance estimates that the gate logic consumes.
3. Aggregates per-strategy results across the full fixture set.
4. Computes ADR 010's named gate decisions (naive-truncate vs
   full-history; optionally retrieval-substitution vs naive).
5. Writes a stable-key JSON baseline. ``git diff`` of the baseline
   file across runs is the regression review.

What it does NOT do
===================

- Talk to a real LLM in tests — the test harness uses a stub chat. The
  CLI uses the real chat callable when invoked from the terminal.
- Decide policy. The CLI prints the gate verdict; the human reads it
  and amends ADRs.
- Run retrieval-substitution-vN strategies. Those are conditional on
  the gate decision and have not landed yet (per ADR 010 ordering).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from services.chat import ContextStrategy, FullHistoryStrategy

from .chat_adapters import make_chat_factory
from .gate import GateDecision, adr_009_gate
from .runner import (
    ChatFactory,
    FixtureResult,
    MultiSeedFixtureResult,
    load_fixture,
    run_fixture_multi_seed,
)
from .scorer import Severity
from .strategies import NaiveTruncateStrategy, RetrievalSubstitutionV1Strategy


# ── Strategy parsing ─────────────────────────────────────────────────────────


_RETRIEVAL_SUB_PREFIX = "retrieval-substitution-v1-n"


def parse_strategy(name: str) -> ContextStrategy:
    """Map a CLI strategy name to a strategy instance.

    Supported:

    - ``full-history`` — production default, identity over history.
    - ``naive-truncate-N`` — keep only the last N user turns. Cheapest
      compaction baseline.
    - ``retrieval-substitution-v1-nN-kK`` — ADR 009's retrieval-first
      strategy. Truncates to the last N user turns, then re-introduces
      the top-K most relevant dropped turns by keyword overlap with the
      latest user turn. The strategy retrieval-substitution must beat
      naive-truncate at the same N to justify its complexity.

    Returns a fresh instance per call (strategies are stateless across
    calls; making them per-run avoids any accidental state sharing).
    """
    name = name.strip()
    if name == "full-history":
        return FullHistoryStrategy()
    if name.startswith("naive-truncate-"):
        suffix = name[len("naive-truncate-") :]
        try:
            n = int(suffix)
        except ValueError as exc:
            raise ValueError(f"invalid naive-truncate suffix: {name!r}") from exc
        return NaiveTruncateStrategy(recent_n=n)
    if name.startswith(_RETRIEVAL_SUB_PREFIX):
        # Format: retrieval-substitution-v1-n<int>-k<int>
        suffix = name[len(_RETRIEVAL_SUB_PREFIX) :]
        try:
            n_part, k_part = suffix.split("-k", 1)
            n = int(n_part)
            k = int(k_part)
        except ValueError as exc:
            raise ValueError(
                f"invalid retrieval-substitution suffix: {name!r} "
                f"(expected 'retrieval-substitution-v1-n<int>-k<int>')"
            ) from exc
        return RetrievalSubstitutionV1Strategy(recent_n=n, top_k=k)
    raise ValueError(
        f"unknown strategy {name!r}. "
        f"Supported: 'full-history', 'naive-truncate-<int>', "
        f"'retrieval-substitution-v1-n<int>-k<int>'."
    )


def parse_strategies(spec: str) -> list[ContextStrategy]:
    """Parse a comma-separated list of strategy names."""
    items = [s for s in spec.split(",") if s.strip()]
    if not items:
        raise ValueError("--strategies must be non-empty")
    return [parse_strategy(s) for s in items]


def parse_seeds(spec: str) -> list[int]:
    """Parse a comma-separated list of integer seeds."""
    items = [s.strip() for s in spec.split(",") if s.strip()]
    if not items:
        raise ValueError("--seeds must be non-empty")
    seeds = []
    for s in items:
        try:
            seeds.append(int(s))
        except ValueError as exc:
            raise ValueError(f"invalid seed: {s!r}") from exc
    if len(seeds) != len(set(seeds)):
        raise ValueError(f"seeds must be unique, got {seeds}")
    return seeds


# ── Fixture loading ──────────────────────────────────────────────────────────


def discover_fixtures(fixtures_dir: Path) -> list[Path]:
    """List all fixture JSON paths in ``fixtures_dir``, sorted by name.

    Excludes ``*.tools.json`` sidecar files. Sorted output ensures the
    baseline JSON's per-fixture ordering is stable across runs (clean
    git diffs).
    """
    out = []
    for path in sorted(fixtures_dir.glob("*.json")):
        if path.name.endswith(".tools.json"):
            continue
        out.append(path)
    if not out:
        raise FileNotFoundError(f"no fixtures found under {fixtures_dir}")
    return out


# ── Run orchestration ────────────────────────────────────────────────────────


async def run_all_strategies(
    fixture_paths: Sequence[Path],
    strategies: Sequence[ContextStrategy],
    *,
    chat_factory: ChatFactory,
    seeds: Sequence[int],
    chat_model_id: str,
    retrieval_enabled: bool = False,
    workspace_path: Optional[Path] = None,
) -> dict[str, list[FixtureResult]]:
    """Run every (strategy, fixture, seed) combination.

    Returns a mapping ``strategy_name → [FixtureResult, ...]`` with one
    entry per fixture per strategy. Each FixtureResult contains
    seeds × target_turns of TurnResults. The gate logic consumes
    paired-by-fixture lists from this dict.
    """
    fixtures = [load_fixture(p) for p in fixture_paths]
    results: dict[str, list[FixtureResult]] = {}
    for strategy in strategies:
        per_fixture: list[FixtureResult] = []
        for fx in fixtures:
            print(
                f"  running {strategy.name} × {fx['id']} × {len(seeds)} seeds...",
                flush=True,
            )
            r: MultiSeedFixtureResult = await run_fixture_multi_seed(
                fx,
                strategy=strategy,
                chat_factory=chat_factory,
                seeds=list(seeds),
                chat_model_id=chat_model_id,
                retrieval_enabled=retrieval_enabled,
                workspace_path=workspace_path,
            )
            per_fixture.append(r)
        results[strategy.name] = per_fixture
    return results


# ── JSON output ──────────────────────────────────────────────────────────────


def _serialize_turn_result(r) -> dict:
    return {
        "turn_index": r.turn_index,
        "seed": r.seed,
        "response_text": r.response_text,
        "severity": r.score.severity.value,
        "passed": r.score.passed,
        "facts_passed": r.score.facts_passed,
        "facts_failed": r.score.facts_failed,
        "guards_triggered": r.score.guards_triggered,
        "latency_ms": round(r.latency_ms, 2),
    }


def _serialize_fixture_result(r: FixtureResult) -> dict:
    return {
        "fixture_id": r.fixture_id,
        "strategy_name": r.strategy_name,
        "chat_model_id": r.chat_model_id,
        "target_turn_count": r.target_turn_count,
        "seeds": list(r.seeds),
        "clean_pass_rate": round(r.clean_pass_rate, 4),
        "confabulation_rate": round(r.confabulation_rate, 4),
        "stdev_clean_pass_rate": round(r.stdev_clean_pass_rate, 4),
        "severity_distribution": {
            k: round(v, 4) for k, v in sorted(r.severity_distribution.items())
        },
        "p50_latency_ms": round(r.p50_latency_ms, 2),
        "p95_latency_ms": round(r.p95_latency_ms, 2),
        "per_seed_clean_pass_rates": {
            str(seed): round(rate, 4)
            for seed, rate in sorted(r.per_seed_clean_pass_rates().items())
        },
        "turn_results": [_serialize_turn_result(t) for t in r.turn_results],
    }


def _serialize_gate_decision(d: GateDecision) -> dict:
    return {
        **{k: v for k, v in dataclasses.asdict(d).items() if k != "verdict"},
        "verdict": d.verdict.value,
    }


def build_output(
    *,
    results: dict[str, list[FixtureResult]],
    chat_model_id: str,
    seeds: Sequence[int],
    fixture_ids: Sequence[str],
    retrieval_enabled: bool,
) -> dict:
    """Build the full baseline JSON document.

    Keys are sorted at every level for stable git diffs across runs.
    Aggregates (overall pass rates per strategy) come first; per-fixture
    detail follows.
    """
    overall = {
        strategy_name: {
            "mean_clean_pass_rate": round(
                sum(r.clean_pass_rate for r in fx_results) / len(fx_results), 4
            ),
            "mean_confabulation_rate": round(
                sum(r.confabulation_rate for r in fx_results) / len(fx_results), 4
            ),
        }
        for strategy_name, fx_results in sorted(results.items())
    }

    # Compute the ADR-009 gate. Two families of comparisons are produced
    # whenever the input strategies support them:
    #
    # 1. ``full-history vs naive-truncate-N`` for every N present. Tests
    #    whether naive truncation matches full-history; if "equivalent"
    #    at N=16 with adequate fixture power, ADR 009's retrieval-first
    #    stance is in question.
    # 2. ``naive-truncate-N vs retrieval-substitution-v1-nN-kK`` for every
    #    matched N (and matched K = whatever's in the run). Tests whether
    #    retrieval-substitution actually earns its complexity over the
    #    cheap baseline at the same window size — the load-bearing test
    #    of ADR 009's preferred strategy.
    gate_decisions: dict[str, dict] = {}
    full_history_results = results.get("full-history")
    naive_keys = sorted(
        k for k in results.keys() if k.startswith("naive-truncate-")
    )
    retrieval_keys = sorted(
        k for k in results.keys() if k.startswith("retrieval-substitution-v1-")
    )

    if full_history_results and naive_keys:
        for naive_key in naive_keys:
            decision_dict = adr_009_gate(
                full_history_results,
                results[naive_key],
            )
            gate_decisions[f"full-history__vs__{naive_key}"] = {
                k: _serialize_gate_decision(v) for k, v in decision_dict.items()
            }

    # Pair each retrieval-substitution-v1 result with its same-N naive
    # variant. The naming convention is stable
    # ("retrieval-substitution-v1-n<N>-k<K>" vs "naive-truncate-<N>")
    # so we can extract N from the retrieval key.
    for retrieval_key in retrieval_keys:
        # Format: retrieval-substitution-v1-n<N>-k<K>
        try:
            after_n = retrieval_key.split("-n", 1)[1]
            n_str = after_n.split("-k", 1)[0]
            int(n_str)
        except (IndexError, ValueError):
            continue
        naive_match = f"naive-truncate-{n_str}"
        if naive_match not in results:
            continue
        decision_dict = adr_009_gate(
            results[naive_match],
            results[retrieval_key],
        )
        # ``adr_009_gate`` labels the comparison as "naive_vs_full_history"
        # internally (its parameter names are general; the label is from
        # the original ADR-009 framing). For retrieval-vs-naive output we
        # rename the sub-key so the JSON makes sense.
        renamed = {
            "retrieval_vs_naive": _serialize_gate_decision(
                next(iter(decision_dict.values()))
            )
        }
        gate_decisions[f"{naive_match}__vs__{retrieval_key}"] = renamed

    return {
        "run_metadata": {
            "timestamp_utc": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "chat_model_id": chat_model_id,
            "seeds": list(seeds),
            "retrieval_enabled": retrieval_enabled,
            "strategies": sorted(results.keys()),
            "fixture_ids": sorted(fixture_ids),
            "fixture_count": len(fixture_ids),
            "seed_count": len(seeds),
        },
        "overall": overall,
        "gate_decisions": gate_decisions,
        "by_strategy": {
            strategy_name: [
                _serialize_fixture_result(r)
                for r in sorted(fx_results, key=lambda x: x.fixture_id)
            ]
            for strategy_name, fx_results in sorted(results.items())
        },
    }


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_eval",
        description=(
            "Run the conversation-replay eval harness end-to-end and "
            "write a baseline JSON for the ADR-010 decision gate."
        ),
    )
    p.add_argument(
        "--strategies",
        default=(
            "full-history,"
            "naive-truncate-4,naive-truncate-8,naive-truncate-12,naive-truncate-16,"
            "retrieval-substitution-v1-n8-k3,retrieval-substitution-v1-n4-k3"
        ),
        help=(
            "Comma-separated strategy names. Default sweeps full-history, "
            "naive-truncate at 4/8/12/16, and retrieval-substitution-v1 at "
            "the two most-pressured naive-N values (4 and 8) with top_k=3. "
            "The retrieval-vs-naive comparison at matched N is the gate "
            "that ADR 010 fires on."
        ),
    )
    p.add_argument(
        "--provider",
        default="ollama",
        help="Chat provider: 'ollama' (default) or 'anthropic'.",
    )
    p.add_argument(
        "--model",
        default=None,
        help=(
            "Override the provider's default model. For Ollama, defaults "
            "to 'qwen3:30b-a3b' (ADR 008's pinned chat slot)."
        ),
    )
    p.add_argument(
        "--base-url",
        default=None,
        help="Override the provider's base URL (Ollama only).",
    )
    p.add_argument(
        "--api-key",
        default=None,
        help="API key (Anthropic only; otherwise reads ANTHROPIC_API_KEY).",
    )
    p.add_argument(
        "--seeds",
        default="1,2,3",
        help="Comma-separated seeds (default: '1,2,3').",
    )
    p.add_argument(
        "--fixtures-dir",
        default="tests/eval/conversations/fixtures",
        help="Path to the fixtures directory.",
    )
    p.add_argument(
        "--out",
        default=None,
        help=(
            "Output path for the baseline JSON. Defaults to "
            "'tests/eval/conversations/baselines/run-<timestamp>.json'."
        ),
    )
    p.add_argument(
        "--retrieval",
        action="store_true",
        help="Enable production-retrieval wiring (mirrors shipped chat).",
    )
    p.add_argument(
        "--workspace-path",
        default=None,
        help="Pin the retrieval workspace (used only with --retrieval).",
    )
    p.add_argument(
        "--num-ctx",
        type=int,
        default=None,
        help=(
            "Override the Ollama num_ctx context window. Default in "
            "OllamaChat is 16384 — large enough for the 50-turn marathon "
            "fixture. Increase only if you see context-overflow errors."
        ),
    )
    return p


async def _run_async(args: argparse.Namespace) -> int:
    fixtures_dir = Path(args.fixtures_dir)
    fixture_paths = discover_fixtures(fixtures_dir)
    fixture_ids = [load_fixture(p)["id"] for p in fixture_paths]

    strategies = parse_strategies(args.strategies)
    seeds = parse_seeds(args.seeds)

    factory = make_chat_factory(
        args.provider,
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
    )
    # Probe one chat instance to get its model_id for the run metadata,
    # and apply the optional num_ctx override (Ollama only).
    sample_chat = factory(seeds[0])
    if args.num_ctx is not None and hasattr(sample_chat, "num_ctx"):
        # Wrap factory to inject num_ctx into every produced adapter
        original_factory = factory
        def _ctx_factory(seed: int):
            chat = original_factory(seed)
            chat.num_ctx = args.num_ctx
            return chat
        factory = _ctx_factory
        sample_chat = factory(seeds[0])
    chat_model_id = sample_chat.model_id

    workspace_path = (
        Path(args.workspace_path) if args.workspace_path else None
    )

    print(
        f"running {len(strategies)} strategies × {len(fixture_paths)} fixtures × "
        f"{len(seeds)} seeds against {chat_model_id}",
        flush=True,
    )

    results = await run_all_strategies(
        fixture_paths,
        strategies,
        chat_factory=factory,
        seeds=seeds,
        chat_model_id=chat_model_id,
        retrieval_enabled=args.retrieval,
        workspace_path=workspace_path,
    )

    output = build_output(
        results=results,
        chat_model_id=chat_model_id,
        seeds=seeds,
        fixture_ids=fixture_ids,
        retrieval_enabled=args.retrieval,
    )

    if args.out:
        out_path = Path(args.out)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = (
            Path("tests/eval/conversations/baselines") / f"run-{ts}.json"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nbaseline written: {out_path}")

    # Echo the gate decisions to the terminal — the most important signal
    if output["gate_decisions"]:
        print("\ngate decisions:")
        for label, decisions in sorted(output["gate_decisions"].items()):
            print(f"  {label}:")
            for sub_label, decision in sorted(decisions.items()):
                print(
                    f"    {sub_label}: {decision['verdict']} "
                    f"(Δ={decision['mean_difference']:+.3f}, "
                    f"95% CI [{decision['ci_low']:+.3f}, {decision['ci_high']:+.3f}])"
                )
                print(f"      → {decision['rationale']}")

    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    sys.exit(main())
