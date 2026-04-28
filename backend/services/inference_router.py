"""InferenceRouter — per-request model dispatch (ADR 004).

Scaffold scope (2026-04-28). Per ADR 004 §"Buildable today", this is the
behavior-preserving router skeleton:

- Rule-based request classifier (chat | tool | code) — coarse on purpose,
  upgradeable to a small classifier model later.
- Per-class slot lookup against the active `ProfilePack` (ADR 005).
- `dispatch()` returns a `DispatchDecision` with the chosen provider/model
  plus an explainable `reason` and the audit fields (`request_class`,
  `slot_class`, `model_id`).
- `_make_llm()` in `routers/chat.py` calls `dispatch_chat()` and then constructs
  the LLM service from the decision — so today's single-model production
  behavior is preserved (the legacy "user picks one local model" install path
  short-circuits the profile lookup), but the seam exists for the multi-slot
  rollout to compose into.

What's deferred per ADR 004 §"Blocked by upstream ADRs":

- Per-conversation `pinned_loadout` enforcement (ADR 008 owns the pin shape).
- Memory-pressure-driven downgrade walking through `SlotLadder.downgrade_ladder`
  using `probe_runtime_load()` — the probe and the ladder both exist now, but
  composing them needs an explicit `can_load(model, ctx_len_now)` predicate
  that respects platform-specific signals (Apple Silicon unified memory needs
  the Tauri-side native helper to be reliable; ADR 003 is the gate). For today,
  `dispatch()` returns the slot's preferred without walking the ladder; the
  ladder is held in the schema so when can_load lands it's a one-liner change
  in `dispatch()`, not a schema migration.
- ML-based classifier upgrade (ADR 004 §E rejected as v1 hard requirement).

The audit-trail consumer of `request_class` and `slot_class` is the chat
WebSocket `done` event (`done_fields["route"]`, populated via
`DispatchDecision.to_audit_dict()`) in `routers/chat.py`. Without that consumer
the classifier output would be unused; with it, every routed request leaves a
traceable record of which slot served it and why.

**Privacy gate boundary.** `dispatch()` is intentionally privacy-naive — it
makes routing decisions without consulting `services.privacy`. The privacy
gate runs in `_route_request()` (in `routers/chat.py`), which is the canonical
entry point for any caller that may need to be blocked when offline mode is
on. New consumers of `get_router().dispatch()` MUST either go through
`_route_request()` or call `services.privacy.assert_provider_allowed()` first
themselves — `dispatch()` will happily route a blocked provider if you ask it
to. Coupling routing to privacy was considered and rejected: privacy is policy
that lives outside dispatch, and tying them would mean every future router
test or non-chat caller has to also stub the privacy module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from services.ollama_service import (
    DEFAULT_OLLAMA_BASE_URL,
    ModelCatalogEntry,
    get_model_by_id,
    get_model_by_litellm,
)
from services.profile_service import ProfilePack, get_active_profile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchDecision:
    """The result of routing a single request.

    The chat handler reads `provider`, `model`, `base_url` to construct the
    LLM service. `request_class`, `slot_class`, `model_id`, `reason` are the
    audit trail surfaced in the `done` WS event for the runtime UI panel.
    """
    provider: str
    model: str  # litellm-format string for ollama/cloud, or canonical for anthropic
    base_url: Optional[str]
    request_class: str  # classifier output: "chat" | "tool" | "code"
    slot_class: str     # which profile slot served the request
    model_id: Optional[str]  # catalog ID if local; None for cloud or unknown
    reason: str

    def to_audit_dict(self) -> Dict[str, Any]:
        """Compact form for the WS `done` event audit trail. Only the fields
        the runtime UI panel surfaces — keeps the wire payload small."""
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "request_class": self.request_class,
            "slot_class": self.slot_class,
            "reason": self.reason,
        }


# ── Rule-based classifier ────────────────────────────────────────────────────


def classify(messages: Optional[List[Dict[str, Any]]], tools: Optional[List[Any]]) -> str:
    """Classify a request by shape. Coarse rule-based per ADR 004 §"Per-request flow".

    Order matters: tool-bearing requests classify as "tool" before content
    inspection — a tool-call request that happens to contain a code fence is
    still primarily a tool request, and the audit trail should reflect that.

    **The chat handler in `routers/chat.py` deliberately passes `tools=None`**
    because in this codebase the tool list is always-on (the specialist filter
    always returns a populated list), so tool-availability carries no per-request
    signal. The `if tools` branch below is preserved for future call sites
    where tools are an explicit per-request decision (e.g. an embedding
    pipeline that selectively enables tool descriptors). ADR 004 §"Per-request
    flow" enumerates `tool` as a request class — keeping the seam matches the
    design contract even though the chat handler doesn't exercise it today.
    """
    if tools:
        return "tool"

    if messages:
        # Walk from most recent backward — the last user turn is the one being
        # responded to right now.
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content") or ""
            if isinstance(content, str) and "```" in content:
                return "code"
            break  # Only inspect the most recent user message

    return "chat"


# ── Router ───────────────────────────────────────────────────────────────────


class InferenceRouter:
    """Routes a request to a model based on the active `ProfilePack` and the
    request's class.

    For today's single-model install path, the user's selected local model
    overrides profile lookup — that's behavior preservation. Profile-driven
    selection only kicks in when the user has not picked a model. Once
    multi-slot installs ship (ADR 005), this short-circuit is removed.
    """

    def __init__(self, profile: Optional[ProfilePack] = None) -> None:
        self._profile_override = profile

    @property
    def profile(self) -> ProfilePack:
        """Resolves the active profile lazily so config edits take effect
        without restarting the process."""
        return self._profile_override or get_active_profile()

    def _slot_class_for_request(self, request_class: str) -> str:
        """Map classifier output to a stack slot.

        For v0 every request goes to the conversational slot regardless of
        classifier output — we don't have wired consumers for code/tool slots
        yet. The mapping exists so that when ADR 005's multi-slot installs
        land, this is a one-line change to read from `self.profile.stack`.
        """
        if request_class == "code" and self.profile.stack.coder is not None:
            return "code"
        return "conversational"

    def _resolve_local_override(
        self,
        model_override: Optional[str],
        base_url: Optional[str],
        request_class: str,
    ) -> DispatchDecision:
        """Legacy single-model path: user picked a specific Ollama model."""
        entry: Optional[ModelCatalogEntry] = None
        if model_override:
            entry = get_model_by_litellm(model_override) or get_model_by_id(model_override)

        if entry is not None:
            return DispatchDecision(
                provider="ollama",
                model=entry.litellm_model,
                base_url=base_url or DEFAULT_OLLAMA_BASE_URL,
                request_class=request_class,
                slot_class=entry.slot_class,
                model_id=entry.id,
                reason=f"user-selected catalog model {entry.id}",
            )

        if model_override:
            # Custom Ollama tag the user set up locally — pass through.
            return DispatchDecision(
                provider="ollama",
                model=model_override,
                base_url=base_url or DEFAULT_OLLAMA_BASE_URL,
                request_class=request_class,
                slot_class="conversational",
                model_id=None,
                reason="user-selected non-catalog Ollama model (passthrough)",
            )

        # No override — fall through to profile lookup.
        return self._dispatch_from_profile(base_url, request_class)

    def _dispatch_from_profile(
        self,
        base_url: Optional[str],
        request_class: str,
    ) -> DispatchDecision:
        """No user override: pick from the active profile's stack."""
        slot_class = self._slot_class_for_request(request_class)

        if slot_class == "code":
            # _slot_class_for_request only returns "code" when coder is not None.
            slot_spec = self.profile.stack.coder.preferred
        else:
            slot_spec = self.profile.stack.conversational.preferred

        entry = get_model_by_id(slot_spec.model_id)
        if entry is None:
            # Profile references a model not yet in the catalog (placeholder
            # entries in the scaffold profiles, e.g. embeddings). Fall back to
            # the safe default rather than crashing — the dispatcher should
            # always produce *something* the chat path can use.
            logger.warning(
                "Profile %s slot %s references unknown model_id %s; falling back to qwen3-8b",
                self.profile.id, slot_class, slot_spec.model_id,
            )
            fallback = get_model_by_id("qwen3-8b")
            if fallback is None:
                # Catalog is missing the safe default — this is a programming
                # error, not a runtime condition. Surface it.
                raise RuntimeError("catalog missing qwen3-8b safe default")
            return DispatchDecision(
                provider="ollama",
                model=fallback.litellm_model,
                base_url=base_url or DEFAULT_OLLAMA_BASE_URL,
                request_class=request_class,
                slot_class=slot_class,
                model_id=fallback.id,
                reason=f"profile {self.profile.id} slot {slot_class} model not in catalog; fallback to qwen3-8b",
            )

        return DispatchDecision(
            provider="ollama",
            model=entry.litellm_model,
            base_url=base_url or DEFAULT_OLLAMA_BASE_URL,
            request_class=request_class,
            slot_class=slot_class,
            model_id=entry.id,
            reason=f"profile {self.profile.id} {slot_class} slot",
        )

    def dispatch(
        self,
        provider: Optional[str],
        model_override: Optional[str],
        base_url: Optional[str],
        request_class: str,
    ) -> DispatchDecision:
        """Make a routing decision for a single request.

        `provider` is the user-selected (or system-default) provider. For
        cloud providers, dispatch is identity — we don't transform the model.
        For Ollama, the router resolves the catalog entry and falls back to
        the active profile when no model is specified.
        """
        effective_provider = provider or "anthropic"

        if effective_provider == "anthropic":
            return DispatchDecision(
                provider="anthropic",
                model=model_override or "claude-sonnet-4-20250514",
                base_url=None,
                request_class=request_class,
                slot_class="conversational",
                model_id=None,
                reason="anthropic primary cloud chat",
            )

        if effective_provider in ("openai", "google"):
            from services.llm_service import DEFAULT_MODELS
            return DispatchDecision(
                provider=effective_provider,
                model=model_override or DEFAULT_MODELS.get(effective_provider, "gpt-4o"),
                base_url=None,
                request_class=request_class,
                slot_class="conversational",
                model_id=None,
                reason=f"{effective_provider} cloud passthrough",
            )

        if effective_provider == "ollama":
            return self._resolve_local_override(model_override, base_url, request_class)

        # Unknown provider — pass through; LiteLLM will surface its own error
        # if the provider isn't supported.
        return DispatchDecision(
            provider=effective_provider,
            model=model_override or "",
            base_url=base_url,
            request_class=request_class,
            slot_class="conversational",
            model_id=None,
            reason=f"unknown provider {effective_provider} (passthrough)",
        )


_default_router: Optional[InferenceRouter] = None


def get_router() -> InferenceRouter:
    """Module-level singleton. Tests construct their own InferenceRouter
    with an explicit profile override; production reads the active profile
    from config via `get_active_profile()` which is mtime-cached so the
    file is only re-read when `app/config.json` actually changes."""
    global _default_router
    if _default_router is None:
        _default_router = InferenceRouter()
    return _default_router


def reset_router_for_tests() -> None:
    """Test hook: drop the singleton so a fresh profile-override router can
    be installed via `get_router()`."""
    global _default_router
    _default_router = None
