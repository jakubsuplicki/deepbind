"""Install-time chat-model self-test (ADR 012).

Three probes against each candidate model in the user-pickable catalog:

1. **Correctness** — does the model honor ``think: false`` and produce a clean
   answer to "say hi in one word"? Regex-checks the response against a panel
   of thinking-prose patterns. Catches the Qwen3-30B-A3B + Ollama 0.18.0
   leak ([ADR 010 Issue 4](docs/architecture/decisions/010-conversation-replay-eval-harness.md))
   and any future model+runtime combinations with the same shape.

2. **Hardware fit** — does ``effective_footprint_bytes(entry, default_ctx)``
   sit comfortably under available RAM (default ≤ 80%)? Reuses the
   existing helpers in ``ollama_service``; same predicate the future
   memory-pressure auto-downgrade uses.

3. **Speed** — does the model achieve usable TPS on warm-short and
   chat-realistic-shallow? Reuses the latency harness's scenarios and
   streaming client so the runtime probe and the dev benchmark stay in
   lockstep — different invocation surface, identical measurement code.

The orchestrator iterates candidates from largest-capability-first, runs
hardware-fit cheaply, then correctness, then speed (most expensive last).
First passing candidate wins. Result is persisted to ``app/config.json``
under ``chat_model_probe`` for the chat router and onboarding to consume.

Determinism: probes use the same ``temperature: 0`` and fixed seed as the
latency harness. Same machine + same Ollama version + same models produces
the same recommendation. The user can override unconditionally via
``user_override`` in the persisted record.
"""

from __future__ import annotations

import asyncio
import logging
import platform as _platform_module
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Optional

from services._config_io import locked_config_update
from services.ollama_service import (
    DEFAULT_OLLAMA_BASE_URL,
    ModelCatalogEntry,
    effective_footprint_bytes,
    get_catalog,
    probe_hardware,
)
from tests.eval.latency.harness import OllamaTimedClient
from tests.eval.latency.scenarios import chat_realistic, warm_short

log = logging.getLogger(__name__)


# ── Tunables (calibrated against the 2026-04-28 M5 Pro 24 GB findings) ─────


CORRECTNESS_PROMPT = "Say hi in one word."
"""Smallest prompt that distinguishes clean output from chain-of-thought leak.
A clean model produces 1–3 tokens ("Hi.", "Hello!"). A leaking model fills
the budget with internal monologue ("Okay, the user asked me to say...")."""

CORRECTNESS_NUM_PREDICT = 8
"""Token cap for the correctness probe. Tight enough that a leaking model
hasn't reached the answer yet; generous enough that a clean model finishes
without truncation."""

THINKING_PROSE_PATTERNS: tuple[str, ...] = (
    r"^(Okay|Hmm|Wait|Let me|First|The user|So[, ])",
    r"\bthink (about|through)\b",
    r"\b(let me|i need to|i'll) (consider|figure|work)\b",
)
"""Regex patterns that match leaked chain-of-thought prose. Compiled at
match time with re.IGNORECASE. The opening-word panel catches the bulk
of Qwen3 thinking-mode openings ("Okay, the user asked..."); the verb
patterns catch reasoning that leaks past the opening word."""

HARDWARE_FIT_RAM_FRACTION = 0.80
"""Maximum fraction of available RAM the model footprint may occupy at
default context. Leaves headroom for OS overhead, the user's other apps,
and KV cache growth during long conversations. Same predicate the future
memory-pressure auto-downgrade uses."""

HARDWARE_FIT_DEFAULT_CTX = 16_384
"""Context length used for the hardware-fit calculation. Matches the
latency harness's default num_ctx so dev benchmarks and runtime probes
agree on what "fits" means."""

WARM_SHORT_PASS_MS = 1_500.0
"""Maximum total wall-clock for warm-short to be considered usable.
Calibrated against Cmd-Tab-to-ChatGPT (~2-3s) — anything under 1.5s
is competitive with the shadow-IT incumbent on this scenario."""

REALISTIC_TPS_PASS = 8.0
"""Minimum sustained decode TPS on chat-realistic-shallow. Below this,
realistic conversations feel slow regardless of TTFT. Calibrated against
the 2026-04-28 finding that 5.5 TPS produced a 14.6s p95 (broken UX)
and 14.0 TPS produced 1.8s p95 (snappy)."""


# ── Result types ────────────────────────────────────────────────────────────


class ProbeVerdict(str, Enum):
    PASS = "pass"
    FAIL_HARDWARE_FIT = "fail_hardware_fit"
    FAIL_CORRECTNESS = "fail_correctness"
    FAIL_SPEED = "fail_speed"
    FAIL_UNREACHABLE = "fail_unreachable"  # Ollama down / model not pulled


@dataclass(frozen=True)
class ProbeEvidence:
    """Diagnostic evidence for one candidate's probe outcome.

    Stable JSON shape so the persisted record is auditable — a buyer can
    open ``app/config.json`` and see exactly why a given model was picked
    or rejected.
    """

    model: str
    verdict: str  # ProbeVerdict.value
    correctness_response: Optional[str] = None
    hardware_fit_bytes: Optional[int] = None
    available_ram_bytes: Optional[int] = None
    warm_short_total_ms: Optional[float] = None
    realistic_tps: Optional[float] = None
    error_message: Optional[str] = None


@dataclass(frozen=True)
class ProbeResult:
    """Full output of one ``recommend_chat_model()`` invocation."""

    schema_version: int
    timestamp_utc: str
    ollama_version: Optional[str]
    platform: str
    ram_gb: Optional[int]
    recommended_model: Optional[str]
    safe_fallback_used: bool
    candidates_evaluated: tuple[ProbeEvidence, ...] = field(default_factory=tuple)
    user_override: Optional[str] = None


# ── Probe 1 — Correctness ──────────────────────────────────────────────────


def _matches_thinking_prose(text: str) -> bool:
    """True if the response looks like leaked chain-of-thought prose."""
    if not text or not text.strip():
        # Empty response is suspicious but not specifically "thinking-leak"
        return False
    for pattern in THINKING_PROSE_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return True
    return False


async def probe_correctness(
    model: str,
    *,
    client: OllamaTimedClient,
) -> tuple[bool, str]:
    """Send the canonical correctness prompt; return (passed, response_text).

    Passing means: the response does not match any thinking-prose pattern
    AND is non-empty. The 8-token cap is intentional — a clean model
    produces a complete answer well within 8 tokens; a leaking model
    hasn't reached the answer yet.
    """
    response = await client.call(
        model=model,
        system_prompt=(
            "You are a helpful assistant. Answer the user's question "
            "directly and concisely."
        ),
        user_message=CORRECTNESS_PROMPT,
        max_output_tokens=CORRECTNESS_NUM_PREDICT,
        seed=1,
        scenario_name="probe-correctness",
    )
    if response.error is not None:
        return False, response.error
    text = response.response_text
    if not text.strip():
        return False, text
    if _matches_thinking_prose(text):
        return False, text
    return True, text


# ── Probe 2 — Hardware fit ─────────────────────────────────────────────────


def _available_ram_bytes() -> Optional[int]:
    """Return total system RAM in bytes, or None if unavailable.

    Uses ``probe_hardware()`` which already detects RAM via psutil. We use
    *total* RAM as the budget reference because the buyer expects to be
    able to run the model alongside their other apps without swapping —
    free-RAM-at-this-instant would let an idle moment pick a model that
    OOMs the moment they open Slack.
    """
    try:
        hw = probe_hardware()
    except Exception as exc:  # noqa: BLE001 — hardware probe failures are diagnostic
        log.warning("hardware probe failed: %s", exc)
        return None
    if hw.total_ram_gb is None or hw.total_ram_gb <= 0:
        return None
    return int(hw.total_ram_gb * (1024**3))


def probe_hardware_fit(
    entry: ModelCatalogEntry,
    *,
    available_ram_bytes: int,
    default_ctx_tokens: int = HARDWARE_FIT_DEFAULT_CTX,
    ram_fraction: float = HARDWARE_FIT_RAM_FRACTION,
) -> tuple[bool, int]:
    """Return (passed, footprint_bytes).

    Passes if ``effective_footprint_bytes(entry, default_ctx) ≤ ram_fraction × available_ram``.
    Reuses the existing ``effective_footprint_bytes()`` helper so the
    probe's "fits" predicate is byte-identical to the future
    memory-pressure auto-downgrade's.
    """
    footprint = effective_footprint_bytes(entry, default_ctx_tokens)
    threshold = int(ram_fraction * available_ram_bytes)
    return footprint <= threshold, footprint


# ── Probe 3 — Speed ────────────────────────────────────────────────────────


async def probe_speed(
    model: str,
    *,
    client: OllamaTimedClient,
) -> tuple[bool, float, float]:
    """Run warm-short + chat-realistic-shallow once each; return passing flag and metrics.

    Returns ``(passed, warm_short_total_ms, realistic_tps)``. One timed run
    per scenario — the goal is "is this usable" not "what's the precise
    p95." The dev benchmark grid does the latter.
    """
    warm = warm_short()
    realistic = chat_realistic()

    warm_response = await client.call(
        model=model,
        system_prompt=warm.system_prompt,
        user_message=warm.user_message,
        max_output_tokens=warm.max_output_tokens,
        seed=1,
        scenario_name=warm.name,
    )
    realistic_response = await client.call(
        model=model,
        system_prompt=realistic.system_prompt,
        user_message=realistic.user_message,
        max_output_tokens=realistic.max_output_tokens,
        seed=1,
        scenario_name=realistic.name,
    )

    if warm_response.error or realistic_response.error:
        return False, 0.0, 0.0

    warm_total = warm_response.total_ms
    realistic_tps = realistic_response.decode_tps
    passed = warm_total <= WARM_SHORT_PASS_MS and realistic_tps >= REALISTIC_TPS_PASS
    return passed, warm_total, realistic_tps


# ── Orchestrator ───────────────────────────────────────────────────────────


def _candidates_for_probe() -> list[ModelCatalogEntry]:
    """User-pickable catalog entries, ordered largest-capability-first.

    Largest-first because we want the most capable model that *passes* — a
    user with 64 GB shouldn't get the 8B fallback when 30B would work for
    them. Hardware-fit pre-filtering inside the orchestrator skips
    too-large candidates cheaply, so this ordering doesn't pay correctness
    or speed probe cost on infeasible candidates.
    """
    catalog = get_catalog()
    user_pickable = [e for e in catalog if not e.internal]
    # Sort by download size descending (proxy for capability). Ties broken
    # by id for determinism.
    return sorted(
        user_pickable,
        key=lambda e: (-e.download_size_gb, e.id),
    )


async def iter_probe_events(
    *,
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    timeout_per_probe_s: float = 60.0,
    candidates: Optional[list[ModelCatalogEntry]] = None,
    ollama_version: Optional[str] = None,
) -> AsyncIterator[dict]:
    """Yield probe progress events; the final event carries the full ``ProbeResult``.

    Event shapes (stable JSON; consumed by the SSE endpoint and by the
    blocking ``recommend_chat_model`` wrapper alike):

    - ``{"event": "started", "candidate_count": N, "available_ram_bytes": int|None}``
    - ``{"event": "candidate_start", "model": str, "index": int, "candidate_count": N}``
    - ``{"event": "candidate_evidence", "evidence": ProbeEvidence-as-dict}``
    - ``{"event": "complete", "result": ProbeResult-as-dict}``

    Generating events instead of returning a single value lets the install-
    time UI stream "1/4: probing qwen3:30b..." progress, while the
    non-streaming caller still gets the final aggregate by draining the
    generator.
    """
    candidate_list = candidates if candidates is not None else _candidates_for_probe()
    available_ram = _available_ram_bytes()
    hw = probe_hardware()
    client = OllamaTimedClient(base_url=base_url)

    yield {
        "event": "started",
        "candidate_count": len(candidate_list),
        "available_ram_bytes": available_ram,
    }

    evidence: list[ProbeEvidence] = []
    recommended: Optional[str] = None

    for index, entry in enumerate(candidate_list):
        yield {
            "event": "candidate_start",
            "model": entry.ollama_model,
            "index": index,
            "candidate_count": len(candidate_list),
        }
        # Probe 2 first — cheapest. If we can't fit the weights, no point
        # paying correctness or speed probe cost.
        if available_ram is not None:
            fits, footprint = probe_hardware_fit(
                entry, available_ram_bytes=available_ram
            )
            if not fits:
                ev = ProbeEvidence(
                    model=entry.ollama_model,
                    verdict=ProbeVerdict.FAIL_HARDWARE_FIT.value,
                    hardware_fit_bytes=footprint,
                    available_ram_bytes=available_ram,
                )
                evidence.append(ev)
                yield {"event": "candidate_evidence", "evidence": asdict(ev)}
                continue
        else:
            # No RAM detection — skip hardware-fit prefiltering, proceed to correctness.
            footprint = effective_footprint_bytes(entry, HARDWARE_FIT_DEFAULT_CTX)

        # Probe 1 — correctness
        try:
            correct, evidence_text = await asyncio.wait_for(
                probe_correctness(entry.ollama_model, client=client),
                timeout=timeout_per_probe_s,
            )
        except asyncio.TimeoutError:
            ev = ProbeEvidence(
                model=entry.ollama_model,
                verdict=ProbeVerdict.FAIL_UNREACHABLE.value,
                error_message="correctness probe timed out",
            )
            evidence.append(ev)
            yield {"event": "candidate_evidence", "evidence": asdict(ev)}
            continue
        if not correct:
            ev = ProbeEvidence(
                model=entry.ollama_model,
                verdict=ProbeVerdict.FAIL_CORRECTNESS.value,
                correctness_response=evidence_text[:500],
            )
            evidence.append(ev)
            yield {"event": "candidate_evidence", "evidence": asdict(ev)}
            continue

        # Probe 3 — speed
        try:
            fast, warm_ms, realistic_tps = await asyncio.wait_for(
                probe_speed(entry.ollama_model, client=client),
                timeout=timeout_per_probe_s * 2,  # speed probe runs two scenarios
            )
        except asyncio.TimeoutError:
            ev = ProbeEvidence(
                model=entry.ollama_model,
                verdict=ProbeVerdict.FAIL_UNREACHABLE.value,
                correctness_response=evidence_text[:500],
                error_message="speed probe timed out",
            )
            evidence.append(ev)
            yield {"event": "candidate_evidence", "evidence": asdict(ev)}
            continue
        if not fast:
            ev = ProbeEvidence(
                model=entry.ollama_model,
                verdict=ProbeVerdict.FAIL_SPEED.value,
                correctness_response=evidence_text[:500],
                warm_short_total_ms=warm_ms,
                realistic_tps=realistic_tps,
            )
            evidence.append(ev)
            yield {"event": "candidate_evidence", "evidence": asdict(ev)}
            continue

        # All three probes passed
        ev = ProbeEvidence(
            model=entry.ollama_model,
            verdict=ProbeVerdict.PASS.value,
            correctness_response=evidence_text[:500],
            hardware_fit_bytes=footprint,
            available_ram_bytes=available_ram,
            warm_short_total_ms=warm_ms,
            realistic_tps=realistic_tps,
        )
        evidence.append(ev)
        yield {"event": "candidate_evidence", "evidence": asdict(ev)}
        recommended = entry.ollama_model
        break  # first passing candidate wins

    result = ProbeResult(
        schema_version=1,
        timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ollama_version=ollama_version,
        platform=_platform_string(hw),
        ram_gb=int(hw.total_ram_gb) if hw.total_ram_gb else None,
        recommended_model=recommended,
        safe_fallback_used=recommended is None,
        candidates_evaluated=tuple(evidence),
        user_override=None,
    )
    yield {
        "event": "complete",
        "result": _result_as_dict(result),
    }


def _result_as_dict(result: ProbeResult) -> dict:
    """JSON-shape projection of a ``ProbeResult``.

    Same fields persistence writes to ``app/config.json`` so SSE events and
    the persisted record are byte-identical — a frontend can persist the
    final-event payload directly without re-fetching.
    """
    return {
        "schema_version": result.schema_version,
        "timestamp_utc": result.timestamp_utc,
        "ollama_version": result.ollama_version,
        "platform": result.platform,
        "ram_gb": result.ram_gb,
        "recommended_model": result.recommended_model,
        "safe_fallback_used": result.safe_fallback_used,
        "candidates_evaluated": [asdict(e) for e in result.candidates_evaluated],
        "user_override": result.user_override,
    }


async def recommend_chat_model(
    *,
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    timeout_per_probe_s: float = 60.0,
    candidates: Optional[list[ModelCatalogEntry]] = None,
    ollama_version: Optional[str] = None,
) -> ProbeResult:
    """Drain ``iter_probe_events`` and return the final ``ProbeResult``.

    Test-friendly synchronous-style wrapper for callers that don't need
    progress streaming.
    """
    final: Optional[ProbeResult] = None
    async for ev in iter_probe_events(
        base_url=base_url,
        timeout_per_probe_s=timeout_per_probe_s,
        candidates=candidates,
        ollama_version=ollama_version,
    ):
        if ev.get("event") == "complete":
            payload = ev["result"]
            final = ProbeResult(
                schema_version=payload["schema_version"],
                timestamp_utc=payload["timestamp_utc"],
                ollama_version=payload["ollama_version"],
                platform=payload["platform"],
                ram_gb=payload["ram_gb"],
                recommended_model=payload["recommended_model"],
                safe_fallback_used=payload["safe_fallback_used"],
                candidates_evaluated=tuple(
                    ProbeEvidence(**e) for e in payload["candidates_evaluated"]
                ),
                user_override=payload["user_override"],
            )
    assert final is not None, "iter_probe_events must yield a complete event"
    return final


# ── Re-run trigger detection ───────────────────────────────────────────────


@dataclass(frozen=True)
class CurrentEnvironment:
    """The pieces of the runtime environment a re-run trigger can detect.

    ``platform`` includes macOS major version because the Qwen3-30B-A3B
    correctness leak (ADR 010 Issue 4) was observed only on macOS 26 — same
    arch and chip on macOS 14 produced clean output. We persist the major
    version so a major-OS bump triggers a fresh probe automatically.

    ``catalog_models`` is the sorted tuple of ``ollama_model`` strings for
    user-pickable entries; new entries trigger re-run so the user sees the
    new model in the recommendation set on next launch instead of waiting
    for an explicit re-test.
    """

    ollama_version: Optional[str]
    platform: str
    catalog_models: tuple[str, ...]


def _platform_string(hw=None) -> str:
    """``{os}-{arch}`` with macOS major version appended on darwin.

    Pre-existing persisted records used the simpler ``{os}-{arch}`` form;
    those records will trip ``platform_changed`` on first read after this
    change ships, which is the desired behaviour — the probe re-runs once,
    and from then on the format is stable.
    """
    if hw is None:
        hw = probe_hardware()
    base = f"{hw.os}-{hw.arch}"
    if hw.os == "darwin":
        try:
            mac_ver = _platform_module.mac_ver()[0]
            major = mac_ver.split(".")[0] if mac_ver else ""
            if major:
                return f"{base}-macos{major}"
        except Exception:  # noqa: BLE001 — diagnostic only
            pass
    return base


def current_environment(
    *,
    ollama_version: Optional[str],
    catalog: Optional[list[ModelCatalogEntry]] = None,
) -> CurrentEnvironment:
    """Capture the current environment for re-run-trigger comparison.

    ``ollama_version`` should come from ``probe_runtime()``; the catalog
    defaults to the user-pickable subset of ``get_catalog()``.
    """
    if catalog is None:
        catalog = [e for e in get_catalog() if not e.internal]
    return CurrentEnvironment(
        ollama_version=ollama_version,
        platform=_platform_string(),
        catalog_models=tuple(sorted(e.ollama_model for e in catalog)),
    )


def needs_rerun(
    persisted: Optional[dict],
    current: CurrentEnvironment,
) -> tuple[bool, str]:
    """Return ``(needs_rerun, reason)``.

    The reason string is one of: ``no_prior_probe``, ``ollama_version_changed``,
    ``platform_changed``, ``catalog_added_models``, or ``fresh``.

    Catalog *removals* don't trigger a re-run — if a model the user had is
    no longer in the catalog the existing recommendation is still valid for
    the models that remain. Only *additions* warrant re-running, since a
    new model might be a better choice than the current pick.
    """
    if not persisted:
        return True, "no_prior_probe"
    if persisted.get("ollama_version") != current.ollama_version:
        return True, "ollama_version_changed"
    if persisted.get("platform") != current.platform:
        return True, "platform_changed"
    persisted_models = {
        e.get("model")
        for e in persisted.get("candidates_evaluated") or []
        if e.get("model")
    }
    added = set(current.catalog_models) - persisted_models
    if added:
        return True, "catalog_added_models"
    return False, "fresh"


# ── Persistence ────────────────────────────────────────────────────────────


PROBE_CONFIG_KEY = "chat_model_probe"


def persist_probe_result(
    result: ProbeResult,
    *,
    config_path: Path,
    user_override: Optional[str] = None,
) -> None:
    """Write the probe result to ``app/config.json`` under ``chat_model_probe``.

    Uses ``locked_config_update`` so concurrent writes don't lose data;
    skip-on-unchanged means re-running the probe with no change doesn't
    rewrite the file.
    """
    payload: dict = {
        "schema_version": result.schema_version,
        "timestamp_utc": result.timestamp_utc,
        "ollama_version": result.ollama_version,
        "platform": result.platform,
        "ram_gb": result.ram_gb,
        "recommended_model": result.recommended_model,
        "safe_fallback_used": result.safe_fallback_used,
        "candidates_evaluated": [asdict(e) for e in result.candidates_evaluated],
        "user_override": user_override,
    }
    with locked_config_update(config_path) as config:
        config[PROBE_CONFIG_KEY] = payload


def read_probe_result(config_path: Path) -> Optional[dict]:
    """Read the persisted probe result, or None if not yet captured."""
    import json

    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data.get(PROBE_CONFIG_KEY)


def set_user_override(
    config_path: Path,
    *,
    model: Optional[str],
) -> None:
    """Set or clear ``user_override`` in the persisted record.

    ``model=None`` clears the override (revert to recommendation). If no
    probe has run yet, the override is written into a stub record so the
    chat router still reads the user's choice — the ``recommended_model``
    field stays ``None`` until a real probe runs and overwrites it.
    """
    with locked_config_update(config_path) as config:
        record = config.get(PROBE_CONFIG_KEY) or {}
        if not record:
            record = {
                "schema_version": 1,
                "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ollama_version": None,
                "platform": _platform_string(),
                "ram_gb": None,
                "recommended_model": None,
                "safe_fallback_used": True,
                "candidates_evaluated": [],
            }
        record["user_override"] = model
        config[PROBE_CONFIG_KEY] = record


def effective_chat_model(config_path: Path) -> Optional[str]:
    """Return the chat model the chat router should use.

    ``user_override`` wins unconditionally. Otherwise ``recommended_model``.
    Returns None when no probe has run yet — the caller falls back to the
    catalog's smallest entry or the static ``DEFAULT_MODELS`` for the
    pre-probe era.
    """
    record = read_probe_result(config_path)
    if not record:
        return None
    return record.get("user_override") or record.get("recommended_model")
