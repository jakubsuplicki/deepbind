"""Ingest benchmark CLI (ADR 013).

Run from the project's backend dir::

    .venv/bin/python -m tests.eval.ingest.run_bench
    .venv/bin/python -m tests.eval.ingest.run_bench --scope pr
    .venv/bin/python -m tests.eval.ingest.run_bench \\
        --fixture samples/911Report.pdf \\
        --seeds 1,2,3,4,5 \\
        --knob-stack batched_embeddings \\
        --out tests/eval/ingest/baselines/baseline-1.json

Two scopes:

- ``--scope nightly`` (default) — full grid: end-to-end + every
  isolated stage, 3 timed runs each. Wall-clock 5–15 min on M5 Pro
  24 GB depending on fixture size; the canonical baseline-capture run.
- ``--scope pr`` — subset for fast checks: end-to-end only, 3 runs.
  ~90s on the 911 Report; used by floor test and the knob loop when
  iterating on a stage.

Output is stable-key JSON, mirroring the latency / conversations
baselines. ``git diff baselines/`` is the regression review.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .runner import GridResult, capture_machine_info, run_grid
from .scenarios import (
    DEFAULT_FIXTURE_RELATIVE,
    IngestScenario,
    default_scenarios,
    end_to_end,
)


# ── Defaults ────────────────────────────────────────────────────────────────


DEFAULT_SEEDS = (1, 2, 3)


# ── Scope ───────────────────────────────────────────────────────────────────


def _scope_scenarios(
    scope: str,
    fixture: Path,
) -> list[IngestScenario]:
    if scope == "pr":
        # PR mode: end-to-end only, fast feedback for iterating on a knob
        return [end_to_end(fixture)]
    if scope == "nightly":
        return default_scenarios(fixture)
    raise ValueError(f"unknown --scope value {scope!r}; expected 'nightly' or 'pr'")


# ── Serialization ───────────────────────────────────────────────────────────


def _round_dict(d: dict, places: int = 2) -> dict:
    return {
        k: (round(v, places) if isinstance(v, float) else v) for k, v in d.items()
    }


def _stage_stats_to_dict(s) -> dict:
    return _round_dict(
        {
            "name": s.name,
            "p50_ms": s.p50_ms,
            "p95_ms": s.p95_ms,
            "mean_ms": s.mean_ms,
            "stdev_ms": s.stdev_ms,
            "units_median": {
                k: round(v, 2) if isinstance(v, float) else v
                for k, v in s.units_median.items()
            },
        }
    )


def grid_to_json(result: GridResult) -> dict:
    """Convert a GridResult to a stable-key JSON-serializable dict."""
    info = result.machine_info
    return {
        "schema_version": 1,
        "run_metadata": {
            "timestamp_utc": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "fixture_path": result.fixture_path,
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
        "by_scenario": [
            _round_dict(
                {
                    "scenario_name": s.scenario_name,
                    "fixture_path": s.fixture_path,
                    "stage": s.stage,
                    "n_timed_runs": s.n_timed_runs,
                    "n_errors": s.n_errors,
                    "skip_reason": s.skip_reason,
                    "total_ms_p50": s.total_ms_p50,
                    "total_ms_p95": s.total_ms_p95,
                    "total_ms_mean": s.total_ms_mean,
                    "total_ms_stdev": s.total_ms_stdev,
                    "stage_stats": [_stage_stats_to_dict(st) for st in s.stage_stats],
                    "errors": list(s.errors),
                }
            )
            for s in sorted(result.stats, key=lambda x: x.scenario_name)
        ],
    }


# ── Summary print ───────────────────────────────────────────────────────────


def _print_summary(result: GridResult) -> None:
    """One-line-per-cell terminal summary; useful for "is this baseline sane"."""
    info = result.machine_info
    print(
        f"\nmachine: {info.model_label} | {info.ram_gb} GB | "
        f"ollama {info.ollama_version or '?'} | "
        f"knobs: {','.join(info.knob_stack) or 'stock'}"
    )
    print(f"fixture: {result.fixture_path}")
    print(f"timed runs per scenario: {result.n_timed_runs}\n")
    print(f"  {'scenario':<40} {'total p50':>12} {'total p95':>12} {'errors':>7}")
    for s in sorted(result.stats, key=lambda x: x.scenario_name):
        if s.skip_reason:
            print(
                f"  {s.scenario_name:<40} {'SKIP':>12} {'':>12} {'':>7}    "
                f"({s.skip_reason})"
            )
        else:
            print(
                f"  {s.scenario_name:<40} "
                f"{s.total_ms_p50:>10.0f}ms {s.total_ms_p95:>10.0f}ms "
                f"{s.n_errors:>7}"
            )
            for st in s.stage_stats:
                if len(s.stage_stats) > 1:
                    print(
                        f"    └─ {st.name:<35} "
                        f"{st.p50_ms:>10.0f}ms {st.p95_ms:>10.0f}ms"
                    )


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_bench",
        description="Capture ingest-pipeline latency baseline JSON (ADR 013).",
    )
    p.add_argument(
        "--scope",
        choices=("nightly", "pr"),
        default="nightly",
        help=(
            "Scope of the run. 'nightly' (default) runs the full grid "
            "(end-to-end + every isolated stage); 'pr' runs end-to-end only."
        ),
    )
    p.add_argument(
        "--fixture",
        default=str(DEFAULT_FIXTURE_RELATIVE),
        help=(
            "Path to the fixture file (PDF). Resolved relative to the "
            "project root if not absolute. Default: 'samples/911Report.pdf'."
        ),
    )
    p.add_argument(
        "--seeds",
        default=",".join(str(s) for s in DEFAULT_SEEDS),
        help="Comma-separated integer seeds (timed runs). Default: '1,2,3'.",
    )
    p.add_argument(
        "--knob-stack",
        default="",
        help=(
            "Comma-separated optimization knobs enabled for this run "
            "(recorded in machine_info). Default empty = stock pipeline."
        ),
    )
    p.add_argument(
        "--out",
        default=None,
        help=(
            "Output baseline JSON path. Defaults to "
            "'tests/eval/ingest/baselines/<machine_id>-<timestamp>.json'."
        ),
    )
    return p


def _parse_seeds(spec: str) -> tuple[int, ...]:
    items = [s.strip() for s in spec.split(",") if s.strip()]
    if not items:
        raise ValueError("--seeds must be non-empty")
    return tuple(int(s) for s in items)


def _resolve_fixture(spec: str) -> Path:
    p = Path(spec)
    if p.is_absolute():
        return p
    # Resolve relative to project root: walk up from this file until
    # we find a directory containing 'samples' or 'backend' (project root)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "samples").is_dir() or (parent / "backend").is_dir():
            return parent / spec
    return Path.cwd() / spec


def _default_out_path(machine_label: str) -> Path:
    safe = "".join(c if c.isalnum() else "-" for c in machine_label.lower()).strip("-")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("tests/eval/ingest/baselines") / f"{safe}-{ts}.json"


async def _run_async(args: argparse.Namespace) -> int:
    knob_stack = [k.strip() for k in args.knob_stack.split(",") if k.strip()]
    info = capture_machine_info(knob_stack=knob_stack)

    fixture = _resolve_fixture(args.fixture)
    if not fixture.exists():
        print(
            f"warning: fixture not found at {fixture} — scenarios will record "
            f"a skip_reason and the baseline will still write."
        )

    scenarios = _scope_scenarios(args.scope, fixture)
    seeds = _parse_seeds(args.seeds)

    print(
        f"running scope={args.scope} | "
        f"{len(scenarios)} scenarios × {len(seeds)} seeds | fixture: {fixture.name}"
    )

    result = await run_grid(
        scenarios=scenarios,
        seeds=seeds,
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
