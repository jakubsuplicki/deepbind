"""Memory-pressure monitor + downgrade ladder picker (ADR 005 §C triggers 1–2).

Two synchronous helpers and one ladder-walking picker. The chat router calls
them at turn-start (the pre-flight check, §C trigger 2) and on inference-time
OOM (§C trigger 1) to decide which model to dispatch to.

Design note — *why synchronous, not a watcher*. ADR 005 §149 originally
described this module as "watches free RAM, emits a `pressure` event when
crossing the 80% headroom threshold." A continuously-running watcher with
event subscribers buys nothing for the two triggers it serves: trigger 1
(OOM during inference) is reactive on the Ollama error path, not on a free-
RAM threshold; trigger 2 (pre-flight) only matters at turn-start. Both want
a fast synchronous "does this still fit right now?" predicate, not an event
firehose. Synchronous wins on simplicity (no task lifecycle, no subscriber
plumbing, no race between watcher tick and turn dispatch). If a future
trigger needs a true event stream we can promote then.

The 80% headroom threshold is read as: pass if `effective_footprint_bytes`
≤ 80% of currently free RAM. The remaining 20% absorbs OS overhead, browser
tabs, in-turn KV cache growth as the model decodes, and any other process
that allocates after the predicate runs but before Ollama actually loads.
ADR §C uses the wording "80% headroom threshold" — practical reading is
"the model takes at most 80% of free RAM." The conservative direction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from services.ollama_service import (
    MODEL_CATALOG,
    ModelCatalogEntry,
    downgrade_ladder_for,
    effective_footprint_bytes,
    tier_for_hardware,
    probe_hardware,
)

logger = logging.getLogger(__name__)

# Default headroom: model footprint must fit in this fraction of free RAM.
# 0.80 → 20% buffer for OS overhead + in-turn KV growth. ADR 005 §C wording
# "80% headroom threshold."
DEFAULT_HEADROOM_FRACTION = 0.80


def current_free_ram_bytes() -> int:
    """Snapshot of currently available RAM, in bytes.

    Pure psutil wrapper. Falls back to 0 (forces "no model fits" behaviour) if
    psutil is unavailable — that pessimistic default is correct: without a
    measurement we can't safely claim the model fits, and the floor-refusal
    path (caller surfaces 503) is the right outcome.
    """
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:  # noqa: BLE001 — env-dependent; never let this raise into chat
        logger.warning("memory_pressure: psutil unavailable; reporting 0 free bytes")
        return 0


def check_can_run(
    entry: ModelCatalogEntry,
    ctx_len_tokens: int,
    *,
    free_ram_bytes: Optional[int] = None,
    headroom_fraction: float = DEFAULT_HEADROOM_FRACTION,
) -> bool:
    """Will `entry` at `ctx_len_tokens` fit in current free RAM with headroom?

    Pass condition: `effective_footprint_bytes(entry, ctx_len_tokens)` ≤
    `free_ram_bytes × headroom_fraction`.

    Pass `free_ram_bytes` explicitly when the caller already has a snapshot
    (avoids re-reading psutil per ladder step inside `pick_runnable_model`).
    """
    free = current_free_ram_bytes() if free_ram_bytes is None else free_ram_bytes
    footprint = effective_footprint_bytes(entry, ctx_len_tokens)
    return footprint <= int(free * headroom_fraction)


@dataclass
class MemoryPressureSwap:
    """Outcome of the ladder walk.

    `chosen` is the runnable entry to dispatch to (None if even the floor
    won't fit — caller must surface a `503 insufficient resources`).
    `reason` is None on no-op (chosen == requested), else a short human-
    readable string for the warning toast. `trail` is the per-step audit
    (model_id, status) for telemetry: `status` is one of `runnable` |
    `over_footprint` | `not_installed`. The first entry in `trail` is
    always the originally-requested model.
    """
    chosen: Optional[ModelCatalogEntry]
    requested: ModelCatalogEntry
    reason: Optional[str]
    trail: List[Tuple[str, str]]

    @property
    def did_swap(self) -> bool:
        return self.chosen is not None and self.chosen.id != self.requested.id


def pick_runnable_model(
    requested: ModelCatalogEntry,
    *,
    tier: str,
    ctx_len_tokens: int,
    installed_ollama_tags: Iterable[str],
    free_ram_bytes: Optional[int] = None,
    headroom_fraction: float = DEFAULT_HEADROOM_FRACTION,
) -> MemoryPressureSwap:
    """Walk the §C ladder from `requested` toward the floor; pick the first
    entry that fits *and* is installed. Returns a `MemoryPressureSwap`.

    Ladder semantics:
      - The ladder is `downgrade_ladder_for(tier)` (top → floor by ladder
        position). We start at the requested model and walk *toward* the
        floor — never sideways or upward. If `requested` already fits, we
        return it untouched (`did_swap=False`).
      - "Installed" filter applied per §C "next-smaller *already-installed*
        model" — we never return an entry whose Ollama tag isn't on disk.
        On Tier A, the §B fallback pull is what populates this; if the
        fallback didn't land, the ladder may dead-end above the floor.
      - Floor refusal: when no installed entry below the requested model
        fits, returns `chosen=None`. Caller surfaces 503.

    Caller passes `free_ram_bytes` if they want a single snapshot for the
    whole walk (avoids per-step psutil churn). Default reads psutil once
    at the top of the call.
    """
    free = current_free_ram_bytes() if free_ram_bytes is None else free_ram_bytes
    threshold = int(free * headroom_fraction)
    installed = set(installed_ollama_tags)

    ladder = downgrade_ladder_for(tier, include_opt_in=True)
    # Drop everything strictly above the requested model's ladder position —
    # we never *upgrade* under pressure.
    if tier in requested.ladder_positions:
        cap = requested.ladder_positions[tier]
        ladder = [e for e in ladder if e.ladder_positions.get(tier, -1) <= cap]
    else:
        # Requested model isn't on this tier's ladder — caller passed an
        # incoherent (model, tier) pair. Fall back to ladder-as-is, starting
        # from whatever's at the top.
        logger.info(
            "memory_pressure: requested model %s has no ladder position on tier %s; "
            "walking full ladder",
            requested.id, tier,
        )

    trail: List[Tuple[str, str]] = []
    chosen: Optional[ModelCatalogEntry] = None
    swap_reason: Optional[str] = None

    for entry in ladder:
        footprint = effective_footprint_bytes(entry, ctx_len_tokens)
        if entry.ollama_model not in installed:
            trail.append((entry.id, "not_installed"))
            continue
        if footprint > threshold:
            trail.append((entry.id, "over_footprint"))
            continue
        trail.append((entry.id, "runnable"))
        chosen = entry
        break

    if chosen is None:
        swap_reason = (
            f"No installed model fits in {free // (1024**3)} GB free RAM at "
            f"{ctx_len_tokens} ctx (headroom {int(headroom_fraction*100)}%)"
        )
    elif chosen.id != requested.id:
        swap_reason = (
            f"{requested.label or requested.id} doesn't fit "
            f"({effective_footprint_bytes(requested, ctx_len_tokens) // (1024**3)} GB > "
            f"{threshold // (1024**3)} GB headroom) — switched to {chosen.label or chosen.id}"
        )

    return MemoryPressureSwap(
        chosen=chosen,
        requested=requested,
        reason=swap_reason,
        trail=trail,
    )


# ── Convenience: derive the user's tier without forcing every caller to import
#    probe_hardware + tier_for_hardware ─────────────────────────────────────


def current_tier() -> str:
    """Probe hardware and map to the ADR 005 tier. Cached-by-caller is fine —
    hardware doesn't change mid-process."""
    return tier_for_hardware(probe_hardware())


def floor_entry_for_tier(
    tier: str,
    *,
    installed_ollama_tags: Iterable[str],
) -> Optional[ModelCatalogEntry]:
    """Smallest installed entry on the tier's ladder — the §C "floor" the
    lightweight-mode toggle (trigger 3) pins to.

    Walks `downgrade_ladder_for(tier)` (top → floor) and returns the *last*
    entry whose Ollama tag is on disk. Lightweight mode picks this so chat
    "just works" without negotiating with the OS for memory; if the user
    only has one model installed and it's already at the bottom of the
    ladder, this returns that same entry and the caller can no-op.

    Returns None when no installed entry sits on the ladder (e.g. user has
    a custom-pulled model that isn't catalogued, or has nothing installed).
    Caller treats None as "lightweight mode can't pin anywhere; fall back
    to the regular pre-flight path."
    """
    installed = set(installed_ollama_tags)
    ladder = downgrade_ladder_for(tier, include_opt_in=True)
    floor: Optional[ModelCatalogEntry] = None
    for entry in ladder:
        if entry.ollama_model in installed:
            floor = entry  # keep walking; want the *smallest* (last) installed
    return floor


# ── OOM-error pattern matching for trigger 1 ────────────────────────────────


_OOM_HINTS: Sequence[str] = (
    "out of memory",
    "out-of-memory",
    "oom",
    "memory exhausted",
    "cannot allocate",
    "metal: failed to allocate",
    "cuda out of memory",
    "ggml_metal_graph_compute",  # Apple Metal OOM signature in Ollama logs
)


def looks_like_oom(error_message: str) -> bool:
    """Crude error-string match for the OOM auto-downgrade trigger (§C #1).

    Ollama returns OOM as plain-text strings via `/api/chat`'s error stream.
    No structured code; matching the message is the only handle we have.
    Better than nothing — false negatives mean the user sees the raw error
    and can re-try, false positives waste one ladder step then re-runs.
    """
    if not error_message:
        return False
    needle = error_message.lower()
    return any(hint in needle for hint in _OOM_HINTS)


# ── Catalog reverse-lookup for chat-router integration ──────────────────────


def find_entry_by_litellm_or_ollama(model: str) -> Optional[ModelCatalogEntry]:
    """Map a chat-router-shaped model identifier back to its catalog entry.

    Handles both forms: `ollama_chat/qwen3:8b` (LiteLLM-prefixed, the form
    `_make_llm` receives) and `qwen3:8b` (raw Ollama tag, what the catalog
    stores). Returns None on no-match — caller treats that as "non-Ollama
    model, skip the pressure check."
    """
    if not model:
        return None
    tag = model.replace("ollama_chat/", "") if model.startswith("ollama_chat/") else model
    for entry in MODEL_CATALOG:
        if entry.ollama_model == tag:
            return entry
    return None
