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
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

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


async def recommend_chat_model(
    *,
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    timeout_per_probe_s: float = 60.0,
    candidates: Optional[list[ModelCatalogEntry]] = None,
) -> ProbeResult:
    """Run the three probes against each candidate; return ranked recommendation.

    Hardware-fit pre-filtering runs first per candidate (no Ollama call).
    Correctness runs next (one Ollama call, ~5s). Speed runs last (two
    Ollama calls, ~10–20s). First fully-passing candidate wins.

    If *no* candidate passes, ``recommended_model`` is None and
    ``safe_fallback_used`` is True — the caller chooses how to handle the
    degenerate case (typically: fall back to the smallest catalog entry
    and surface a warning).
    """
    candidate_list = candidates if candidates is not None else _candidates_for_probe()
    available_ram = _available_ram_bytes()
    hw = probe_hardware()
    client = OllamaTimedClient(base_url=base_url)

    evidence: list[ProbeEvidence] = []
    recommended: Optional[str] = None

    for entry in candidate_list:
        # Probe 2 first — cheapest. If we can't fit the weights, no point
        # paying correctness or speed probe cost.
        if available_ram is not None:
            fits, footprint = probe_hardware_fit(
                entry, available_ram_bytes=available_ram
            )
            if not fits:
                evidence.append(
                    ProbeEvidence(
                        model=entry.ollama_model,
                        verdict=ProbeVerdict.FAIL_HARDWARE_FIT.value,
                        hardware_fit_bytes=footprint,
                        available_ram_bytes=available_ram,
                    )
                )
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
            evidence.append(
                ProbeEvidence(
                    model=entry.ollama_model,
                    verdict=ProbeVerdict.FAIL_UNREACHABLE.value,
                    error_message="correctness probe timed out",
                )
            )
            continue
        if not correct:
            evidence.append(
                ProbeEvidence(
                    model=entry.ollama_model,
                    verdict=ProbeVerdict.FAIL_CORRECTNESS.value,
                    correctness_response=evidence_text[:500],
                )
            )
            continue

        # Probe 3 — speed
        try:
            fast, warm_ms, realistic_tps = await asyncio.wait_for(
                probe_speed(entry.ollama_model, client=client),
                timeout=timeout_per_probe_s * 2,  # speed probe runs two scenarios
            )
        except asyncio.TimeoutError:
            evidence.append(
                ProbeEvidence(
                    model=entry.ollama_model,
                    verdict=ProbeVerdict.FAIL_UNREACHABLE.value,
                    correctness_response=evidence_text[:500],
                    error_message="speed probe timed out",
                )
            )
            continue
        if not fast:
            evidence.append(
                ProbeEvidence(
                    model=entry.ollama_model,
                    verdict=ProbeVerdict.FAIL_SPEED.value,
                    correctness_response=evidence_text[:500],
                    warm_short_total_ms=warm_ms,
                    realistic_tps=realistic_tps,
                )
            )
            continue

        # All three probes passed
        evidence.append(
            ProbeEvidence(
                model=entry.ollama_model,
                verdict=ProbeVerdict.PASS.value,
                correctness_response=evidence_text[:500],
                hardware_fit_bytes=footprint,
                available_ram_bytes=available_ram,
                warm_short_total_ms=warm_ms,
                realistic_tps=realistic_tps,
            )
        )
        recommended = entry.ollama_model
        break  # first passing candidate wins

    return ProbeResult(
        schema_version=1,
        timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ollama_version=None,  # populated by callers that capture it; harness independence
        platform=f"{hw.os}-{hw.arch}",
        ram_gb=int(hw.total_ram_gb) if hw.total_ram_gb else None,
        recommended_model=recommended,
        safe_fallback_used=recommended is None,
        candidates_evaluated=tuple(evidence),
        user_override=None,
    )


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
