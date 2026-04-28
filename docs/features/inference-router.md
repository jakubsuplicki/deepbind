---
title: Inference Router
status: scaffold
type: feature
sources:
	- backend/services/inference_router.py
	- backend/services/ollama_service.py
	- backend/routers/chat.py
	- backend/tests/test_inference_router.py
depends_on:
	- local-models
	- profiles
last_reviewed: 2026-04-28
last_updated: 2026-04-28
---

# Inference Router

Per-request model dispatch ([ADR 004](../architecture/decisions/004-inference-router-architecture.md)).

## Status: scaffold

This is the **router skeleton** that ADR 004 §"Buildable today" calls out as the
forward-compatible piece that lands before the upstream ADRs (003 Tauri,
008 conversation pinning, full ADR 005 multi-profile install) ship. Behavior on
today's single-model production install is unchanged at the user level — the
seam exists in the codebase so the multi-slot rollout composes into it rather
than having to rewrite the chat dispatch path.

| In scaffold (this module) | Deferred (per ADR 004 §"Blocked by upstream ADRs") |
|---|---|
| Rule-based classifier (chat / tool / code) | ML-based classifier upgrade |
| `dispatch()` returning `DispatchDecision` (provider/model/audit fields) | Walking the `SlotLadder.downgrade_ladder` under memory pressure |
| Profile-driven slot lookup (consumes [ProfilePack](profiles.md)) | `pinned_loadout` enforcement (ADR 008 owns the pin shape) |
| User-override short-circuit (legacy single-model preservation) | Production-grade Apple Silicon footprint signals (ADR 003) |
| WS `done` event audit payload (`route` field) | Runtime UI panel consuming the audit payload |

## How It Works

### Per-request flow

```
                    ┌────────────────────────────────┐
                    │ chat handler receives WS msg   │
                    └────────────────┬───────────────┘
                                     ▼
                    ┌────────────────────────────────┐
                    │ classify(messages, tools)      │
                    │   → "chat" | "tool" | "code"   │
                    └────────────────┬───────────────┘
                                     ▼
                    ┌────────────────────────────────┐
                    │ get_router().dispatch(...)     │
                    │   ↓                            │
                    │ if user picked a model         │
                    │   → identity-on-override       │
                    │ else                           │
                    │   → ProfilePack slot lookup    │
                    └────────────────┬───────────────┘
                                     ▼
                    ┌────────────────────────────────┐
                    │ DispatchDecision(provider,     │
                    │   model, slot_class,           │
                    │   request_class, model_id,     │
                    │   reason)                      │
                    └────────────────┬───────────────┘
                                     ├──→ _llm_from_decision()
                                     │     → constructs LLMService / ClaudeService
                                     │
                                     └──→ done_fields["route"] = decision.to_audit_dict()
                                           → WS `done` event audit trail
```

### `classify(messages, tools) → str`

Rule-based per ADR 004 §"Per-request flow":

- `tools` non-empty → `"tool"` (dominates content inspection — a tool-call
  request that happens to contain a code fence is still primarily a tool
  request, and the audit trail should reflect that)
- Most recent user message contains a `` ``` `` code fence → `"code"`
- Otherwise → `"chat"`

Only the *most recent* user turn drives classification. Older turns can have
code without making the current request a code request, and assistant messages
are ignored entirely.

### `InferenceRouter.dispatch(provider, model_override, base_url, request_class) → DispatchDecision`

Decision tree:

1. **Cloud providers** (`anthropic` / `openai` / `google` / unknown) — identity
   dispatch. The router doesn't transform the model string for cloud requests;
   the provider's own SDK handles model selection. `model_id` is `None` in the
   audit because cloud models aren't in the local catalog.

2. **Ollama with user override** (`_resolve_local_override`) — try to resolve
   the override against the catalog by `litellm_model` first, then by `id`. If
   matched, the decision carries the catalog entry's `slot_class` and `id`.
   If not matched (custom Ollama tag the user set up locally), pass through
   with `model_id=None` and `slot_class="conversational"`.

3. **Ollama with no override** (`_dispatch_from_profile`) — read the active
   `ProfilePack` (defaults to `generic-knowledge-worker`), select the slot for
   the request class (`code` requests use the coder ladder if the profile has
   one; everything else uses the conversational ladder). The slot's preferred
   model is looked up in the catalog. If the profile references a model that
   isn't in the catalog (e.g. embedding placeholders), fall back to `qwen3-8b`
   and emit a warning rather than crashing — the dispatcher must always produce
   *something* the chat path can use.

### `DispatchDecision.to_audit_dict()`

Compact form for the WS `done` event:

```python
{
    "provider": "ollama",
    "model_id": "qwen3-8b",
    "request_class": "chat",
    "slot_class": "conversational",
    "reason": "user-selected catalog model qwen3-8b",
}
```

The `model` string and `base_url` are deliberately omitted — they don't belong
in a UI audit panel, and surfacing them risks leaking workspace-local URLs
into client telemetry.

## Wiring into chat.py

The chat handler calls `_route_request()` once at the top of `_handle_message`,
feeds the resulting decision to `_llm_from_decision()` to construct the LLM
service, and includes `decision.to_audit_dict()` in the `done_fields` payload
that closes out the WS message.

A back-compat `_make_llm(provider, model, api_key, base_url)` shim still exists
because tests and the per-connection `_get_llm` cache call it directly — it
internally routes through the same `get_router().dispatch()` so the constructed
model string is consistent across both paths.

## Behavior preservation

Today's single-model production install:

1. User picks `qwen3:8b` in the chat picker → `model = "ollama_chat/qwen3:8b"`
   flows through every chat WS message.
2. `_route_request` receives the model override → `_resolve_local_override`
   matches it in the catalog → `DispatchDecision(model="ollama_chat/qwen3:8b",
   model_id="qwen3-8b", slot_class="conversational")`.
3. `_llm_from_decision` constructs an `LLMService` with the same model string.

Net effect: the model string handed to LiteLLM is unchanged from pre-router
behavior. The router contributes the `route` audit payload in `done` events
and nothing else on the legacy path.

The profile-driven path activates only when there is no model override — i.e.
the post-ADR-005 multi-slot install path that doesn't ship until that ADR
implements the onboarding picker.

## Key Files

| File | Purpose |
|------|---------|
| [inference_router.py](../../backend/services/inference_router.py) | Router skeleton: classifier + dispatch + DispatchDecision |
| [ollama_service.py](../../backend/services/ollama_service.py) | Catalog with ADR-004 footprint fields (`bytes_per_kv_token`, `attention_arch`, `slot_class`); `effective_footprint_bytes()`; `KEEP_ALIVE_BY_SLOT` policy table |
| [chat.py](../../backend/routers/chat.py) | Wiring: `_route_request` + `_llm_from_decision` + `done` event audit payload |
| [test_inference_router.py](../../backend/tests/test_inference_router.py) | 46 tests: classifier corners, cloud/ollama dispatch, profile fallback, footprint accounting, keep_alive policy |

## Behavior under code-review hardening (2026-04-28)

- **Decision/cache consistency.** The per-WS-connection `_get_llm` cache is keyed on the routed (`provider`, `model`, `base_url`) from the `DispatchDecision`, not on the user-selected `model` string. So when profile-driven multi-slot installs ship and the *resolved* model differs from the user's input, the cache rebuilds rather than serving a stale instance — keeping the audit trail (the `route` field) and the actual generation consistent.
- **Privacy gate enforced inside `_route_request`.** Every entry point (chat handler, duel handler, `_make_llm` test shim) gets the gate consistently and only once per call. `PrivacyBlockedError` raises before LLM construction. **Boundary:** `InferenceRouter.dispatch()` itself is intentionally privacy-naive — see the module docstring on [`inference_router.py`](../../backend/services/inference_router.py) for the rationale. Future non-chat callers of `dispatch()` must either go through `_route_request()` or call `assert_provider_allowed()` themselves; the contract is documented at the module level so the next person to wire a new caller doesn't accidentally bypass the gate.
- **Duel routing.** The duel handler also routes via `_route_request` — same cache key, same audit shape. A duel and a chat turn in the same connection share the LLM instance when they resolve to the same model.
- **Classifier ignores always-on tools.** In this codebase `TOOLS` is always populated (specialist filter), so passing it to the classifier would dominate every request as `"tool"`. The chat handler passes `tools=None` to `_route_request` so content-based rules (code-fence detection) actually drive the decision. The classifier API still accepts a `tools` parameter for callers that have explicit per-request tool descriptors.

## Gotchas

- **The router does NOT walk the downgrade ladder yet.** `SlotLadder.downgrade_ladder`
  is in the schema and tested for shape, but `dispatch()` returns the slot's
  `preferred` unconditionally. Walking the ladder under memory pressure needs
  a `can_load(model, ctx_len_now)` predicate that respects platform-specific
  signals — and per ADR 004 §"Production-grade Apple Silicon signals", that
  predicate is gated on the Tauri-side native helper that ADR 003 unblocks.
  Holding the ladder in the schema today is correct (no migration cost when
  can_load lands); using it today would mean making decisions on `psutil`
  signals that are noisy on macOS.

- **Profile placeholder slots fall back, they don't crash.** The scaffold
  profiles reference `qwen3-embedding-0.6b` / `kokoro-82m` / `granite-vision-3-2b`
  for embedding/TTS/vision slots — none of those are in the catalog yet. If the
  router were ever asked to dispatch to one (it isn't today; only the
  conversational and coder slots are read), it would log a warning and fall
  back to `qwen3-8b`. The right time to fail-fast on missing slot models is
  when the slot has a real consumer; until then, fall-back-with-warning is
  more useful than crash-on-startup.

- **Singleton router caches the active profile per instance.** `get_router()`
  returns a process-wide singleton; the singleton resolves the active profile
  on each `dispatch()` call (not at construction), so config edits to
  `active_profile_id` take effect on the next request without a backend
  restart. Tests that need an explicit profile construct their own
  `InferenceRouter(profile=...)` — `reset_router_for_tests()` is the hook to
  drop the singleton if a test needs to verify the singleton-vs-test-instance
  boundary.

- **Audit fields are part of the wire contract now.** The `done` event payload
  gained a `route: { provider, model_id, request_class, slot_class, reason }`
  object. Frontends that care about the routing decision read it; older
  frontends ignore unknown fields. If the field name changes, treat it as a
  wire-format change for the WS protocol.

## Related ADRs

- [ADR 004 — Multi-model InferenceRouter](../architecture/decisions/004-inference-router-architecture.md) — the architecture this scaffold implements one phase of.
- [ADR 005 — Profile-driven model stacks](../architecture/decisions/005-profile-driven-model-stacks.md) — the `ProfilePack` shape this router consumes.
- [ADR 008 — Conversation-pinned chat model](../architecture/decisions/008-conversation-pinned-chat-model.md) — the `pinned_loadout` enforcement contract this router will eventually honour.
- [ADR 003 — Desktop distribution: Tauri shell](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md) — gates production-grade memory-pressure signals.
