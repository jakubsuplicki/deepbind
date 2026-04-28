"""Latency benchmark CLI (ADR 011).

Run from the project's backend dir::

    .venv/bin/python -m tests.eval.latency.run_bench
    .venv/bin/python -m tests.eval.latency.run_bench --scope pr
    .venv/bin/python -m tests.eval.latency.run_bench \\
        --models qwen3:8b,qwen3:14b \\
        --seeds 1,2,3,4,5 \\
        --knob-stack flash_attention,kv_cache_q8 \\
        --out tests/eval/latency/baselines/baseline-1.json

Two scopes:

- ``--scope nightly`` (default) — full grid, all default scenarios × all
  default models. Wall-clock 45–90 min on M5 Pro 24 GB; the canonical
  baseline-capture run.
- ``--scope pr`` — subset for quick checks: only ``warm-short`` +
  ``chat-realistic-shallow`` × the canonical chat model. ~5–10 min.
  Used by the floor test and developers iterating on a knob.

Output is stable-key JSON, mirroring the conversations harness's
discipline. ``git diff baselines/`` is the regression review.
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

from .harness import OllamaTimedClient, AnthropicTimedClient
from .runner import GridResult, capture_machine_info, run_grid
from .scenarios import (
    Scenario,
    chat_realistic,
    default_scenarios,
    warm_short,
)


# ── Defaults ────────────────────────────────────────────────────────────────


# Per ADR 010 §"Issue 4" + ADR 012 §"Default model selection": qwen3:14b is the
# canonical chat model for benchmarking until the install-time self-test (ADR 012)
# lands and per-machine selection takes over. qwen3:30b-a3b is *excluded* from
# nightly because it leaks chain-of-thought on Ollama 0.18.0 — measuring it
# produces correct-but-misleading numbers since the production path will not run
# this combination on this Ollama version.
DEFAULT_MODELS_NIGHTLY = ("qwen3:8b", "qwen3:14b")
DEFAULT_MODELS_PR = ("qwen3:14b",)
DEFAULT_SEEDS = (1, 2, 3, 4, 5)


# ── Scope ───────────────────────────────────────────────────────────────────


def _scope_scenarios_models(
    scope: str,
    *,
    include_reference: bool,
) -> tuple[list[Scenario], tuple[str, ...]]:
    if scope == "pr":
        # PR mode: smallest viable set for fast iteration
        return [warm_short(), chat_realistic()], DEFAULT_MODELS_PR
    if scope == "nightly":
        return default_scenarios(include_reference=include_reference), DEFAULT_MODELS_NIGHTLY
    raise ValueError(f"unknown --scope value {scope!r}; expected 'nightly' or 'pr'")


# ── Serialization ───────────────────────────────────────────────────────────


def _round_dict(d: dict, places: int = 2) -> dict:
    return {
        k: (round(v, places) if isinstance(v, float) else v) for k, v in d.items()
    }


def grid_to_json(result: GridResult) -> dict:
    """Convert a GridResult to a stable-key JSON-serializable dict."""
    info = result.machine_info
    return {
        "schema_version": 1,
        "run_metadata": {
            "timestamp_utc": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "n_warmup_runs": result.n_warmup_runs,
            "n_timed_runs": result.n_timed_runs,
            "seeds": list(result.seeds),
        },
        "machine_info": {
            "model_label": info.model_label,
            "ollama_version": info.ollama_version,
            "platform": info.platform,
            "ram_gb": info.ram_gb,
            "knob_stack": list(info.knob_stack),
        },
        "by_cell": [
            _round_dict(
                {
                    "model_id": s.model_id,
                    "scenario_name": s.scenario_name,
                    "n_timed_runs": s.n_timed_runs,
                    "n_errors": s.n_errors,
                    "skip_reason": s.skip_reason,
                    "ttft_ms_p50": s.ttft_ms_p50,
                    "ttft_ms_p95": s.ttft_ms_p95,
                    "ttft_ms_mean": s.ttft_ms_mean,
                    "ttft_ms_stdev": s.ttft_ms_stdev,
                    "decode_tps_p50": s.decode_tps_p50,
                    "decode_tps_p95": s.decode_tps_p95,
                    "decode_tps_mean": s.decode_tps_mean,
                    "decode_tps_stdev": s.decode_tps_stdev,
                    "total_ms_p50": s.total_ms_p50,
                    "total_ms_p95": s.total_ms_p95,
                    "total_ms_mean": s.total_ms_mean,
                    "total_ms_stdev": s.total_ms_stdev,
                    "output_tokens_mean": s.output_tokens_mean,
                    "prompt_tokens_mean": s.prompt_tokens_mean,
                    "sample_response_text": s.sample_response_text,
                    "errors": list(s.errors),
                }
            )
            for s in sorted(result.stats, key=lambda x: (x.model_id, x.scenario_name))
        ],
    }


# ── Summary print ───────────────────────────────────────────────────────────


def _print_summary(result: GridResult) -> None:
    """One-line-per-cell terminal summary; useful for "is this baseline sane"."""
    info = result.machine_info
    print(f"\nmachine: {info.model_label} | {info.ram_gb} GB | "
          f"ollama {info.ollama_version or '?'} | "
          f"knobs: {','.join(info.knob_stack) or 'stock'}")
    print(f"timed runs per cell: {result.n_timed_runs}\n")
    print(f"  {'model':<24} {'scenario':<28} {'TTFT p50':>10} {'TPS p50':>9} {'total p95':>11} {'errors':>7}")
    for s in sorted(result.stats, key=lambda x: (x.model_id, x.scenario_name)):
        if s.skip_reason:
            print(
                f"  {s.model_id:<24} {s.scenario_name:<28} "
                f"{'SKIP':>9}    {'':>9} {'':>11} {'':>7}    ({s.skip_reason})"
            )
        else:
            print(
                f"  {s.model_id:<24} {s.scenario_name:<28} "
                f"{s.ttft_ms_p50:>9.0f}ms {s.decode_tps_p50:>8.1f} "
                f"{s.total_ms_p95:>10.0f}ms {s.n_errors:>7}"
            )


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_bench",
        description="Capture latency baseline JSON for the local-models stack (ADR 011).",
    )
    p.add_argument(
        "--scope",
        choices=("nightly", "pr"),
        default="nightly",
        help=(
            "Scope of the run. 'nightly' (default) runs the full grid; "
            "'pr' runs the subset for fast PR checks."
        ),
    )
    p.add_argument(
        "--models",
        default=None,
        help=(
            "Comma-separated Ollama model tags. Overrides the scope's default "
            "model list. Example: 'qwen3:8b,qwen3:14b'."
        ),
    )
    p.add_argument(
        "--seeds",
        default=",".join(str(s) for s in DEFAULT_SEEDS),
        help="Comma-separated integer seeds (timed runs). Default: '1,2,3,4,5'.",
    )
    p.add_argument(
        "--no-reference",
        action="store_true",
        help=(
            "Skip the Anthropic reference scenario. Default behavior is to "
            "include it; it skips silently when no API key is available."
        ),
    )
    p.add_argument(
        "--knob-stack",
        default="",
        help=(
            "Comma-separated list of optimization knobs enabled for this run "
            "(recorded in baseline machine_info). Default empty = stock Ollama."
        ),
    )
    p.add_argument(
        "--ollama-base-url",
        default="http://127.0.0.1:11434",
        help="Ollama HTTP base URL.",
    )
    p.add_argument(
        "--anthropic-model",
        default=None,
        help=(
            "Override the Anthropic reference model. Default is the realistic "
            "shadow-IT comparison (Claude Sonnet 4.x family)."
        ),
    )
    p.add_argument(
        "--out",
        default=None,
        help=(
            "Output baseline JSON path. Defaults to "
            "'tests/eval/latency/baselines/<machine_id>-<timestamp>.json'."
        ),
    )
    return p


def _parse_seeds(spec: str) -> tuple[int, ...]:
    items = [s.strip() for s in spec.split(",") if s.strip()]
    if not items:
        raise ValueError("--seeds must be non-empty")
    return tuple(int(s) for s in items)


def _default_out_path(machine_label: str) -> Path:
    safe = "".join(c if c.isalnum() else "-" for c in machine_label.lower()).strip("-")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("tests/eval/latency/baselines") / f"{safe}-{ts}.json"


async def _run_async(args: argparse.Namespace) -> int:
    knob_stack = [k.strip() for k in args.knob_stack.split(",") if k.strip()]
    info = capture_machine_info(knob_stack=knob_stack)

    scenarios, default_models = _scope_scenarios_models(
        args.scope, include_reference=not args.no_reference
    )
    if args.models:
        models = tuple(m.strip() for m in args.models.split(",") if m.strip())
    else:
        models = default_models

    seeds = _parse_seeds(args.seeds)

    ollama = OllamaTimedClient(base_url=args.ollama_base_url)
    anthropic = AnthropicTimedClient(
        model=args.anthropic_model
        or AnthropicTimedClient().model
    )

    print(
        f"running scope={args.scope} | "
        f"{len(scenarios)} scenarios × {len(models)} models × {len(seeds)} seeds"
    )

    result = await run_grid(
        scenarios=scenarios,
        models=models,
        seeds=seeds,
        ollama_client=ollama,
        anthropic_client=anthropic,
        machine_info=info,
        progress=print,
    )

    out_path = Path(args.out) if args.out else _default_out_path(info.model_label)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(grid_to_json(result), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    _print_summary(result)
    print(f"\nbaseline written: {out_path}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    sys.exit(main())
