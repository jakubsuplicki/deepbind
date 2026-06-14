"""Latency benchmark runner — orchestrate, aggregate, capture machine info (ADR 011).

The runner takes a list of scenarios and a list of models, runs each
combination N+1 times (1 warm-up discarded + N timed runs), and aggregates
TimedResponse records into per-(model, scenario) statistics. Sequential
execution: parallel runs would compete for memory/GPU and pollute the
numbers.

The aggregate exposes p50, p95, mean, and standard deviation per metric.
The gate logic consumes per-run lists for bootstrap CIs.

Machine info is captured once per run via :func:`capture_machine_info`.
This anchors the baseline JSON to a specific hardware + Ollama version
combination — comparing baselines across machines is meaningless, so the
machine identity must be in the file.
"""

from __future__ import annotations

import asyncio
import platform
import statistics
import subprocess
from dataclasses import dataclass, field
from typing import Optional, Sequence

import httpx

from .harness import (
    OllamaTimedClient,
    TimedResponse,
)
from .scenarios import Scenario


# ── Aggregation ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScenarioStats:
    """Aggregated metrics for one (model, scenario) pair across N timed runs.

    ``skip_reason`` is set when the cell wasn't actually measured (e.g. the
    model isn't pulled in Ollama). All metric fields are zero in that case;
    the floor test treats skipped cells as informational rather than as
    measurement failures.

    ``sample_response_text`` is the response text from the first non-errored
    run, truncated to 500 chars. Lets a future scorer or human spot
    thinking-leaks / wrong-shape outputs without re-running the model.
    """

    model_id: str
    scenario_name: str
    n_timed_runs: int
    n_errors: int
    ttft_ms_p50: float
    ttft_ms_p95: float
    ttft_ms_mean: float
    ttft_ms_stdev: float
    decode_tps_p50: float
    decode_tps_p95: float
    decode_tps_mean: float
    decode_tps_stdev: float
    total_ms_p50: float
    total_ms_p95: float
    total_ms_mean: float
    total_ms_stdev: float
    output_tokens_mean: float
    prompt_tokens_mean: float
    runs: tuple[TimedResponse, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
    skip_reason: Optional[str] = None
    sample_response_text: str = ""


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * pct))))
    return s[k]


def _safe_stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.stdev(values)


def skipped_stats(
    *,
    model_id: str,
    scenario_name: str,
    reason: str,
) -> ScenarioStats:
    """Build a sentinel ScenarioStats for a (model, scenario) cell that wasn't measured.

    Used when a model isn't pulled in Ollama. ``n_timed_runs = n_errors = 0``
    so the floor test treats the cell as informational, not a measurement
    failure. The skip reason is surfaced in the baseline JSON so the user
    sees "model wasn't pulled" rather than "5 mysterious errors."
    """
    return ScenarioStats(
        model_id=model_id,
        scenario_name=scenario_name,
        n_timed_runs=0,
        n_errors=0,
        ttft_ms_p50=0.0,
        ttft_ms_p95=0.0,
        ttft_ms_mean=0.0,
        ttft_ms_stdev=0.0,
        decode_tps_p50=0.0,
        decode_tps_p95=0.0,
        decode_tps_mean=0.0,
        decode_tps_stdev=0.0,
        total_ms_p50=0.0,
        total_ms_p95=0.0,
        total_ms_mean=0.0,
        total_ms_stdev=0.0,
        output_tokens_mean=0.0,
        prompt_tokens_mean=0.0,
        skip_reason=reason,
    )


def aggregate(
    runs: Sequence[TimedResponse],
    *,
    model_id: str,
    scenario_name: str,
) -> ScenarioStats:
    """Compute p50/p95/mean/stdev across timed runs.

    Errored runs (``error`` set) are excluded from metric aggregation but
    counted in ``n_errors`` and surfaced in ``errors``. If every run errored,
    metrics report 0.0 — the JSON consumer reads ``n_errors`` to know it's
    not a real measurement.
    """
    ok = [r for r in runs if r.error is None]
    err = [r.error for r in runs if r.error is not None]
    sample_text = ok[0].response_text[:500] if ok else ""

    ttfts = [r.ttft_ms for r in ok]
    tpss = [r.decode_tps for r in ok]
    totals = [r.total_ms for r in ok]
    output_tokens = [float(r.output_tokens) for r in ok]
    prompt_tokens = [float(r.prompt_tokens) for r in ok]

    return ScenarioStats(
        model_id=model_id,
        scenario_name=scenario_name,
        n_timed_runs=len(ok),
        n_errors=len(err),
        ttft_ms_p50=_percentile(ttfts, 0.5),
        ttft_ms_p95=_percentile(ttfts, 0.95),
        ttft_ms_mean=statistics.mean(ttfts) if ttfts else 0.0,
        ttft_ms_stdev=_safe_stdev(ttfts),
        decode_tps_p50=_percentile(tpss, 0.5),
        decode_tps_p95=_percentile(tpss, 0.95),
        decode_tps_mean=statistics.mean(tpss) if tpss else 0.0,
        decode_tps_stdev=_safe_stdev(tpss),
        total_ms_p50=_percentile(totals, 0.5),
        total_ms_p95=_percentile(totals, 0.95),
        total_ms_mean=statistics.mean(totals) if totals else 0.0,
        total_ms_stdev=_safe_stdev(totals),
        output_tokens_mean=statistics.mean(output_tokens) if output_tokens else 0.0,
        prompt_tokens_mean=statistics.mean(prompt_tokens) if prompt_tokens else 0.0,
        runs=tuple(runs),
        errors=tuple(err),
        sample_response_text=sample_text,
    )


# ── Machine info ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MachineInfo:
    """Identifying details of the machine the baseline was captured on.

    Stored verbatim in the baseline JSON. Two baselines are only comparable
    when their MachineInfo matches in (platform, model_label, ram_gb,
    ollama_version). The harness does not enforce this — the gate caller
    decides whether to compare.
    """

    platform: str
    model_label: str
    ram_gb: Optional[int]
    ollama_version: Optional[str]
    knob_stack: tuple[str, ...]


def _detect_ollama_version() -> Optional[str]:
    """Return the Ollama version string or None if unavailable.

    Tries ``ollama --version`` via subprocess; ADR 010 pins 0.18.0 on Apple M5,
    and the recorded version helps explain "the baseline regressed when we
    bumped Ollama" later.
    """
    try:
        proc = subprocess.run(
            ["ollama", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    return out or None


def _detect_ram_gb() -> Optional[int]:
    """Return system RAM in GB. Falls back to None on platforms we don't probe.

    Avoids a hard psutil dependency in the runner module — psutil is
    already a project dep but the latency harness should run even if it
    isn't, e.g., in a slim CI environment that strips optional deps.
    """
    try:
        import psutil

        return int(round(psutil.virtual_memory().total / (1024**3)))
    except ImportError:
        return None


def _detect_apple_silicon_label() -> Optional[str]:
    """Return e.g. ``Apple M5 Pro`` on macOS arm64, None elsewhere."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return None
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    label = (proc.stdout or "").strip()
    return label or None


def capture_machine_info(knob_stack: Optional[Sequence[str]] = None) -> MachineInfo:
    """Snapshot the current machine for the baseline JSON.

    ``knob_stack`` is the list of optimization knobs enabled for this run
    (e.g. ``["flash_attention", "kv_cache_q8"]``). Defaults to empty, which
    represents the "stock Ollama" baseline-0.
    """
    apple = _detect_apple_silicon_label()
    label = apple or platform.platform()
    return MachineInfo(
        platform=f"{platform.system().lower()}-{platform.machine()}",
        model_label=label,
        ram_gb=_detect_ram_gb(),
        ollama_version=_detect_ollama_version(),
        knob_stack=tuple(sorted(set(knob_stack or ()))),
    )


# ── Grid orchestration ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class GridResult:
    """Full output of one ``run_grid`` invocation."""

    machine_info: MachineInfo
    seeds: tuple[int, ...]
    n_warmup_runs: int
    n_timed_runs: int
    stats: tuple[ScenarioStats, ...]


async def _probe_pulled_models(base_url: str) -> set[str]:
    """Return the set of Ollama model tags currently pulled.

    Used by ``run_grid`` to skip cells whose model isn't installed rather
    than failing 5 times per scenario with HTTP 404s. Returns the empty
    set on any HTTP / parsing failure — caller treats "couldn't probe" as
    "don't filter," falling back to the original behavior of attempting
    every model and recording errors.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
            if resp.status_code != 200:
                return set()
            data = resp.json()
            models = data.get("models") or []
            return {m.get("name") for m in models if m.get("name")}
    except (httpx.HTTPError, ValueError, KeyError):
        return set()


async def run_grid(
    *,
    scenarios: Sequence[Scenario],
    models: Sequence[str],
    seeds: Sequence[int] = (1, 2, 3, 4, 5),
    n_warmup_runs: int = 1,
    ollama_client: Optional[OllamaTimedClient] = None,
    machine_info: Optional[MachineInfo] = None,
    progress: Optional[callable] = None,  # type: ignore[type-arg]
    pulled_models: Optional[set[str]] = None,
) -> GridResult:
    """Run every (model × scenario) combo with warm-up + N timed runs.

    Sequential execution against the local Ollama stack (ADR 015: local-only
    build, no hosted-API comparison). Scenarios are run per (model × scenario);
    models not present in ``pulled_models`` (probed via ``GET /api/tags`` if
    not supplied) are recorded as skipped rather than retried 5x with HTTP 404.

    Failed cells surface as ``errors`` on the aggregated ScenarioStats —
    the grid as a whole completes even if one cell fails (a model OOM on
    one scenario shouldn't kill the whole run).

    ``progress`` is an optional callable invoked as ``progress(label)`` for
    each run; the CLI passes ``print``. Pass ``None`` in tests to silence.

    ``pulled_models``: pre-probed set of Ollama model tags currently
    pulled. When None, the function probes ``GET /api/tags`` itself.
    Tests pass an explicit set to avoid network IO.
    """
    ollama = ollama_client or OllamaTimedClient()
    info = machine_info or capture_machine_info()

    n_timed = len(seeds)
    results: list[ScenarioStats] = []

    ollama_scenarios = list(scenarios)

    if pulled_models is None:
        pulled_models = await _probe_pulled_models(ollama.base_url)

    # ── Per-model Ollama scenarios ──────────────────────────────────────
    for model in models:
        # Skip absent models cleanly — single sentinel cell per scenario,
        # not 5 hard errors per scenario.
        if pulled_models and model not in pulled_models:
            for scenario in ollama_scenarios:
                if progress is not None:
                    progress(f"  ollama:{model} × {scenario.name}: SKIP (model not pulled)")
                results.append(
                    skipped_stats(
                        model_id=f"ollama:{model}",
                        scenario_name=scenario.name,
                        reason=f"model {model!r} not pulled in Ollama",
                    )
                )
            continue

        for scenario in ollama_scenarios:
            label = f"ollama:{model} × {scenario.name}"
            if progress is not None:
                progress(f"  {label}: warming up...")
            for _ in range(n_warmup_runs):
                await ollama.call(
                    model=model,
                    system_prompt=scenario.system_prompt,
                    user_message=scenario.user_message,
                    max_output_tokens=scenario.max_output_tokens,
                    seed=seeds[0],
                    scenario_name=scenario.name,
                )

            timed: list[TimedResponse] = []
            for i, seed in enumerate(seeds, 1):
                if progress is not None:
                    progress(f"  {label}: timed run {i}/{n_timed}...")
                r = await ollama.call(
                    model=model,
                    system_prompt=scenario.system_prompt,
                    user_message=scenario.user_message,
                    max_output_tokens=scenario.max_output_tokens,
                    seed=seed,
                    scenario_name=scenario.name,
                )
                timed.append(r)

            results.append(
                aggregate(
                    timed,
                    model_id=f"ollama:{model}",
                    scenario_name=scenario.name,
                )
            )

    return GridResult(
        machine_info=info,
        seeds=tuple(seeds),
        n_warmup_runs=n_warmup_runs,
        n_timed_runs=n_timed,
        stats=tuple(results),
    )


def run_grid_sync(
    *,
    scenarios: Sequence[Scenario],
    models: Sequence[str],
    seeds: Sequence[int] = (1, 2, 3, 4, 5),
    n_warmup_runs: int = 1,
    ollama_client: Optional[OllamaTimedClient] = None,
    machine_info: Optional[MachineInfo] = None,
    pulled_models: Optional[set[str]] = None,
) -> GridResult:
    """Sync wrapper for tests / scripts that don't drive an event loop."""
    return asyncio.run(
        run_grid(
            scenarios=scenarios,
            models=models,
            seeds=seeds,
            n_warmup_runs=n_warmup_runs,
            ollama_client=ollama_client,
            machine_info=machine_info,
            pulled_models=pulled_models,
        )
    )
