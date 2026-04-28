"""ProfilePack — per-vertical model stack manifest (ADR 005).

Scaffold scope (2026-04-28). Per ADR 005 + ADR 004's "Buildable today" /
"Blocked by upstream ADRs" split, this module defines the **schema** and a
**scaffold catalog of three profiles** used by the InferenceRouter today; the
remaining six profiles in the ADR's table are deferred until someone close
to each vertical signs off on the specialist defaults, tool defaults, and
ingest defaults (per ADR 005 §"Open follow-ups" #1).

What's in this scaffold:
- `ProfilePack` Pydantic schema matching ADR 005 §"Schema"
- `SlotSpec` / `SlotLadder` shapes for each stack slot
- 3 starter profiles: `generic-knowledge-worker`, `developer-devops`,
  `patent-prosecutor` — enough variety to validate the schema (no coder for
  patent, coder ladder for developer, smallest install for generic)
- `get_active_profile()` reads from `app/config.json:active_profile_id`,
  falling back to `generic-knowledge-worker` when unset

What's deferred:
- Tauri-side onboarding picker UI
- Profile change (delta downloads / 30-day GC of dropped models)
- The other six profiles in ADR 005's catalog table
- License's `allowed_profiles` enforcement (the field is read but not yet
  cross-checked with the loaded license — wires in once the Tauri-side license
  loader from ADR 006 lands)
- `ingest_defaults` / `ui_strings` / `tools` defaults — placeholders today

The schema lands now; the empty fields are explicit (not silently absent) so
future fills are localized edits rather than cross-cutting refactors.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, Field

from services._config_io import locked_config_update

logger = logging.getLogger(__name__)


# Slot model_ids that are intentionally name-only placeholders today — the
# corresponding catalog entry / slot consumer hasn't been wired yet, but the
# profile schema needs to reference *something* for the slot. The router's
# `_dispatch_from_profile` falls back to qwen3-8b when it encounters one of
# these. Exposed so `validate_profile_catalog()` doesn't flag them as typos.
KNOWN_PLACEHOLDER_MODEL_IDS: Set[str] = {
    "qwen3-embedding-0.6b",  # embedding slot consumer not yet wired
    "kokoro-82m",            # TTS slot consumer not yet wired
    "granite-vision-3-2b",   # vision slot consumer not yet wired
}


class SlotSpec(BaseModel):
    """A single model slot in a profile stack.

    `model_id` references `ModelCatalogEntry.id` in `services/ollama_service.py`.
    `quant` and `pinned_sha` are placeholders for ADR 003's pinning mechanism;
    today they're informational. `expected_footprint_at_default_ctx` is a
    pre-computed budget hint surfaced in onboarding ("this profile needs
    ~12 GB unified memory at default context").
    """
    model_id: str
    quant: Optional[str] = None
    pinned_sha: Optional[str] = None
    expected_footprint_at_default_ctx_gb: Optional[float] = None


class SlotLadder(BaseModel):
    """A preferred model + downgrade ladder for a slot class.

    Per ADR 004 §"Per-request flow", `dispatch()` tries `preferred` first,
    then walks the ladder (smaller models that still satisfy the slot's
    purpose) under memory pressure. The router refuses with an explicit
    error if nothing in the ladder fits.
    """
    preferred: SlotSpec
    downgrade_ladder: List[SlotSpec] = Field(default_factory=list)


class ProfileStack(BaseModel):
    """The stack of slots for a profile. `embeddings` and `plumbing` are
    always-resident; `conversational` and `reasoning` are required; the
    rest are optional and `None` means the slot is not provisioned.
    """
    embeddings: SlotSpec
    plumbing: SlotSpec
    conversational: SlotLadder
    reasoning: SlotSpec
    coder: Optional[SlotLadder] = None
    vision: Optional[SlotSpec] = None
    long_context: Optional[SlotSpec] = None
    tts: Optional[SlotSpec] = None


class ProfilePack(BaseModel):
    """A profile manifest per ADR 005 §"Schema"."""
    id: str
    display_name: str
    version: int = 1
    stack: ProfileStack
    specialists: List[str] = Field(default_factory=list)
    tools: Dict[str, bool] = Field(default_factory=dict)
    ingest_defaults: Dict[str, object] = Field(default_factory=dict)
    ui_strings: Dict[str, str] = Field(default_factory=dict)
    context_recent_n: int = 4  # default per ADR 009
    # Per ADR 004 §"Runtime UI panel" the only legal values are these three.
    # Constraining via Literal so a typo in a profile definition (e.g.
    # "balenced") fails at construction rather than at runtime when the perf
    # toggle reads it.
    context_perf_mode_default: Literal["balanced", "quality", "lightweight"] = "balanced"


# ── Initial profile catalog (scaffold) ───────────────────────────────────────

# All three reference catalog model IDs from `services/ollama_service.py`. The
# `embeddings` slot references `qwen3-embedding-0.6b` and `tts` references
# `kokoro-82m` per ADR 005's "common to every profile" line — but those models
# aren't in the catalog yet (no embedding/TTS slot consumers wired). Marked
# clearly: until the embedding and TTS surfaces land, those slots are name-only
# placeholders the router does not dispatch to.

_PLACEHOLDER_EMBEDDING = SlotSpec(
    model_id="qwen3-embedding-0.6b",  # NOT in catalog yet — placeholder pending embedding slot consumer
    expected_footprint_at_default_ctx_gb=0.6,
)

_PLACEHOLDER_TTS = SlotSpec(
    model_id="kokoro-82m",  # NOT in catalog yet — placeholder pending TTS slot consumer
    expected_footprint_at_default_ctx_gb=0.2,
)

_GENERIC_KNOWLEDGE_WORKER = ProfilePack(
    id="generic-knowledge-worker",
    display_name="Generic knowledge worker",
    stack=ProfileStack(
        embeddings=_PLACEHOLDER_EMBEDDING,
        plumbing=SlotSpec(model_id="granite-4-h-micro"),
        conversational=SlotLadder(
            preferred=SlotSpec(model_id="qwen3-8b"),
            downgrade_ladder=[
                SlotSpec(model_id="qwen3-4b"),
                SlotSpec(model_id="qwen3-1.7b"),
            ],
        ),
        reasoning=SlotSpec(model_id="gemma4-e4b"),
        coder=None,
        vision=None,
        long_context=SlotSpec(model_id="ministral-3-8b"),
        tts=_PLACEHOLDER_TTS,
    ),
    specialists=[],
    tools={},
)

_DEVELOPER_DEVOPS = ProfilePack(
    id="developer-devops",
    display_name="Developer / DevOps",
    stack=ProfileStack(
        embeddings=_PLACEHOLDER_EMBEDDING,
        plumbing=SlotSpec(model_id="granite-4-h-micro"),
        conversational=SlotLadder(
            preferred=SlotSpec(model_id="qwen3-8b"),
            downgrade_ladder=[
                SlotSpec(model_id="qwen3-4b"),
            ],
        ),
        reasoning=SlotSpec(model_id="gemma4-e4b"),
        coder=SlotLadder(
            preferred=SlotSpec(model_id="devstral-small-2-24b"),
            downgrade_ladder=[
                SlotSpec(model_id="qwen3-8b"),  # generalist fallback when coder slot evicted
            ],
        ),
        vision=None,
        long_context=SlotSpec(model_id="ministral-3-8b"),
        tts=_PLACEHOLDER_TTS,
    ),
    specialists=["developer"],
    tools={"jira_ingest": True, "mcp": True},
)

_PATENT_PROSECUTOR = ProfilePack(
    id="patent-prosecutor",
    display_name="Patent prosecution",
    stack=ProfileStack(
        embeddings=_PLACEHOLDER_EMBEDDING,
        plumbing=SlotSpec(model_id="granite-4-h-micro"),
        conversational=SlotLadder(
            preferred=SlotSpec(model_id="qwen3-8b"),
            downgrade_ladder=[
                SlotSpec(model_id="qwen3-4b"),
            ],
        ),
        reasoning=SlotSpec(model_id="gemma4-e4b"),
        # Patent prosecutor profile explicitly drops the coder slot — that's
        # the whole point of profile-driven stacks per ADR 005.
        coder=None,
        # Vision slot required for figures; placeholder until the vision model
        # catalog entry + slot consumer land.
        vision=SlotSpec(model_id="granite-vision-3-2b"),  # NOT in catalog yet — placeholder
        long_context=SlotSpec(model_id="ministral-3-8b"),
        tts=_PLACEHOLDER_TTS,
    ),
    specialists=["patent-prosecution"],
    tools={"web_search": True},
)

PROFILE_CATALOG: Dict[str, ProfilePack] = {
    p.id: p for p in (_GENERIC_KNOWLEDGE_WORKER, _DEVELOPER_DEVOPS, _PATENT_PROSECUTOR)
}

DEFAULT_PROFILE_ID = "generic-knowledge-worker"


def get_profile_by_id(profile_id: str) -> Optional[ProfilePack]:
    return PROFILE_CATALOG.get(profile_id)


def list_profiles() -> List[ProfilePack]:
    return list(PROFILE_CATALOG.values())


# ── Active profile resolution (mtime-cached) ─────────────────────────────────

# Cached state: (config_path_str, mtime, resolved_profile). The cache is
# keyed on the absolute config path so different test workspaces don't share
# cache entries with each other. Invalidated automatically when mtime changes
# (covers external edits) and explicitly by `set_active_profile()`.
_active_profile_cache: Optional[Tuple[str, float, ProfilePack]] = None


def _resolve_active_profile_from(config_path: Path) -> ProfilePack:
    """Read and parse the active profile from a config file. Returns the
    default profile on any failure (malformed JSON, unknown id, IO error).

    A malformed config falling back silently is a real debugging hazard
    ("why is my profile not loading?"), so we log at warning level — operator
    log scrapes will surface this even without a debug build.
    """
    try:
        with open(config_path) as f:
            config = json.load(f)
        profile_id = config.get("active_profile_id")
        if profile_id and profile_id in PROFILE_CATALOG:
            return PROFILE_CATALOG[profile_id]
        if profile_id:
            logger.warning(
                "active_profile_id %r is not in the profile catalog; "
                "falling back to %s", profile_id, DEFAULT_PROFILE_ID,
            )
    except (json.JSONDecodeError, IOError, OSError) as exc:
        logger.warning("Failed to read active_profile_id from %s: %s", config_path, exc)
    return PROFILE_CATALOG[DEFAULT_PROFILE_ID]


def get_active_profile() -> ProfilePack:
    """Read the active profile, with mtime-based caching.

    The router's `dispatch()` calls this on every WS message. Without caching,
    that's a `config.json` read per chat turn. The cache is invalidated when:
    (a) the config file's mtime changes (external edits), or (b)
    `set_active_profile()` runs (explicit invalidation).

    **Multi-worker note:** the cache is process-local. A `uvicorn --workers N`
    deployment has N independent caches; a `set_active_profile()` call in one
    worker only invalidates that worker's cache. Other workers pick up the new
    profile when their next `get_active_profile()` sees a changed mtime —
    eventual consistency, not strong. Today's deployment target per ADR 003 is
    a single-worker desktop binary, where this is moot.

    Falls back to DEFAULT_PROFILE_ID if no profile is set or the configured
    one is not in the catalog (defensive: a stale config from a removed
    profile shouldn't crash the dispatcher).
    """
    global _active_profile_cache

    try:
        from config import get_settings
    except ImportError:
        return PROFILE_CATALOG[DEFAULT_PROFILE_ID]

    try:
        settings = get_settings()
        config_path = settings.workspace_path / "app" / "config.json"
        if not config_path.exists():
            return PROFILE_CATALOG[DEFAULT_PROFILE_ID]

        mtime = config_path.stat().st_mtime
        cache_key = str(config_path)
        if _active_profile_cache is not None:
            cached_path, cached_mtime, cached_profile = _active_profile_cache
            if cached_path == cache_key and cached_mtime == mtime:
                return cached_profile

        profile = _resolve_active_profile_from(config_path)
        _active_profile_cache = (cache_key, mtime, profile)
        return profile
    except OSError as exc:
        # stat() can fail if the file vanished between exists() and stat();
        # fall back rather than crash the dispatcher.
        logger.warning("Failed to stat %s: %s", config_path, exc)
        return PROFILE_CATALOG[DEFAULT_PROFILE_ID]


def invalidate_active_profile_cache() -> None:
    """Drop the active-profile cache. Called by `set_active_profile()` after
    a successful write; tests use it directly when manipulating config files
    that bypass `set_active_profile()`."""
    global _active_profile_cache
    _active_profile_cache = None


def set_active_profile(profile_id: str) -> None:
    """Persist the active profile id to `app/config.json` atomically.

    Uses `locked_config_update` from `services._config_io` so concurrent
    writers (e.g. `set_active_local_model` running at the same time) don't
    drop each other's updates via a read-modify-write race. The write is
    skipped if the dict didn't actually change (keeps the mtime cache warm).

    Raises ValueError if the profile_id is not in the catalog (callers must
    validate against `list_profiles()` before calling this). Invalidates the
    active-profile cache so the next `get_active_profile()` re-reads.
    """
    if profile_id not in PROFILE_CATALOG:
        raise ValueError(f"Unknown profile_id: {profile_id}")
    from config import get_settings

    settings = get_settings()
    config_path = settings.workspace_path / "app" / "config.json"
    with locked_config_update(config_path) as config:
        config["active_profile_id"] = profile_id
    invalidate_active_profile_cache()


# ── Profile validation ───────────────────────────────────────────────────────


def _walk_slot_specs(stack: ProfileStack) -> List[SlotSpec]:
    """Collect every SlotSpec in a stack — direct slots + ladder rungs."""
    specs: List[SlotSpec] = [stack.embeddings, stack.plumbing]
    specs.append(stack.conversational.preferred)
    specs.extend(stack.conversational.downgrade_ladder)
    specs.append(stack.reasoning)
    if stack.coder is not None:
        specs.append(stack.coder.preferred)
        specs.extend(stack.coder.downgrade_ladder)
    if stack.vision is not None:
        specs.append(stack.vision)
    if stack.long_context is not None:
        specs.append(stack.long_context)
    if stack.tts is not None:
        specs.append(stack.tts)
    return specs


def validate_profile_catalog() -> Dict[str, List[str]]:
    """Validate that every profile's stack references either a catalog entry
    or a known placeholder. Returns a dict of `profile_id → list of unresolved
    model_ids`. An empty dict means everything resolves cleanly.

    Run this from tests so a typo in a profile definition (`devstal` for
    `devstral`) fails CI rather than silently triggering the runtime
    fall-back-to-qwen3-8b warning. The runtime fallback is meant for the
    *known* placeholders; real typos should be caught earlier.
    """
    from services.ollama_service import get_model_by_id

    results: Dict[str, List[str]] = {}
    for profile in PROFILE_CATALOG.values():
        unresolved: List[str] = []
        for spec in _walk_slot_specs(profile.stack):
            if spec.model_id in KNOWN_PLACEHOLDER_MODEL_IDS:
                continue
            if get_model_by_id(spec.model_id) is None:
                unresolved.append(spec.model_id)
        if unresolved:
            results[profile.id] = unresolved
    return results
