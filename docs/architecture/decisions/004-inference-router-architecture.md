# ADR 004 — Multi-model `InferenceRouter` with dynamic load/unload

**Status:** Accepted
**Date:** 2026-04-27 (initial), amended 2026-04-28 (implementation phasing)
**Related:** [ADR 002](002-pure-local-product-shape.md) · [ADR 005](005-profile-driven-model-stacks.md) · [ADR 008](008-conversation-pinned-chat-model.md) · [ADR 009](009-context-overflow-compaction.md) · [`docs/research/models/model-research-4.md`](../../research/models/model-research-4.md)

## Context

The current codebase treats local-LLM dispatch as a static install-time pick: hardware probe → score models → user picks one → that model serves every chat request until manually changed. Single active model at [`ollama_service.py:838`](../../../backend/services/ollama_service.py#L838), single-LLM dispatch at [`chat.py:256`](../../../backend/routers/chat.py#L256).

[`docs/research/models/model-research-4.md`](../../research/models/model-research-4.md) concluded that a single-model stack is the wrong shape. Different request classes have different optima:

- **Embeddings** — small, always-resident, latency-critical, no overlap with chat attention state.
- **Plumbing / always-on classification** — small, fast, runs in parallel with the chat model.
- **Conversational + tool dispatch** — primary chat brain.
- **Reasoning / duel mode** — distinct fine-tune or mode toggle.
- **Coding** — vertical-specific, not all profiles need it ([ADR 005](005-profile-driven-model-stacks.md)).
- **Vision** — on-demand for documents and figures.
- **TTS** — output-side, tiny, irrelevant to chat attention state.

A 24 GB Mac cannot host all of these simultaneously at full quant. The dispatcher must load on demand, unload on idle, downgrade under memory pressure, and tell the user honestly what trade-offs it is making.

The current architecture cannot represent this. Single-active-model state at the config layer; single-model dispatch at the request layer; single fixed `keep_alive` value at the runtime layer; no live RAM/VRAM signals; no Ollama `/api/ps` integration; no per-request classification; no downgrade ladder; no UI surface for any of the above.

## Decision drivers

1. **A 24 GB Mac is the floor.** The dispatcher must produce usable behavior under genuine memory pressure, not assume workstation hardware.
2. **The user must always understand what is happening.** "Why is this slow right now?" must be answerable from a single visible UI panel. Hidden swap is the worst-of-both-worlds outcome.
3. **Stateless slots route freely; conversational state pins per conversation.** This is enforced in [ADR 008](008-conversation-pinned-chat-model.md). The router enforces both behaviors; this is not a contradiction but two distinct policies.
4. **Profile is the source of truth for which slots exist.** A patent-prosecutor profile has no coder slot. The dispatcher must not synthesize coder routing decisions for a profile that doesn't include one.
5. **KV cache footprint scales with context length, not just weights.** A long Jira ingest can balloon a chat-model footprint mid-session. Footprint accounting must use current context, not weights-only.
6. **The dispatcher's behavior must be auditable.** Per-turn `model_id` recorded in the session row ([ADR 008](008-conversation-pinned-chat-model.md)). Every swap event surfaced in UI and logged.

## Decision

### Architecture

Replace the single-model dispatch path with an `InferenceRouter` service. The router holds:

- A **resident set** — models intentionally kept loaded (always-on slots: embeddings, plumbing).
- A **slot table** — per-class preferred model + per-class downgrade ladder, derived from the active `ProfilePack` ([ADR 005](005-profile-driven-model-stacks.md)).
- A **load probe** — `probe_runtime_load()` returning `{free_ram, free_vram, swap_used, page_outs, recent_oom, loaded[]}`, polled at 1–2s cadence.
- A **footprint table** — per model: `weights_bytes`, `bytes_per_kv_token`, `attention_arch` (`transformer` | `mamba` | `swa`). Effective footprint at request time = `weights + bytes_per_kv_token × ctx_len_now`.
- A **`keep_alive` policy table** — per slot: `forever` (embeddings, plumbing), `minutes` (chat, reasoning), `immediate-on-idle` (coder, vision unless active session).

### Per-request flow

```
route(request) -> Response:
  request.class       <- classify(request)        # chat | tool | reasoning | code | embed | vision
  pool                <- profile.slots[request.class]
  if pool is None:
      return refuse_with_reason("profile does not include this class")
  model               <- pool.preferred
  if not resident(model):
      if can_load(model, footprint_at(model, request.ctx_len_now)):
          load(model, keep_alive_policy[request.class])
      else:
          model       <- downgrade(pool, until_fits)
          if model is None:
              return fail_explicit("memory full — free resources or simplify request")
  return await model.generate(request)
```

The classifier starts coarse (rule-based on request shape: tool-call request → tool class, contains code fences → code class, etc.) and is upgradeable to a small classifier model later. Routing decisions are deterministic and explainable — the runtime UI panel shows *why* a request was routed where.

### Conversation pinning ([ADR 008](008-conversation-pinned-chat-model.md))

The chat slot is pinned per conversation. The router does not auto-swap the chat slot mid-conversation. Stateless slots (embeddings, plumbing, vision, code-pass, TTS) route freely per request. This ADR defers the full swap policy to ADR 008 but enforces the contract here: the router accepts a per-conversation `pinned_loadout` snapshot that overrides general routing for the duration of the conversation.

### KV-aware footprint accounting

`ModelCatalogEntry` gains `bytes_per_kv_token` (per attention architecture; see [`model-research-1.md`](../../research/models/model-research-1.md) §"KV-cache discipline" for reference numbers). `score_model()` and the dispatcher's `can_load()` use `weights + bytes_per_kv_token × ctx_len_now`, not `download_size_gb` alone.

For Mamba-hybrid architectures (Granite 4 H-Tiny / H-Small) and sliding-window-attention models (Gemma 4) the per-token cost is materially lower; the footprint formula handles this with the `attention_arch` field.

### Live signals (the foundation step)

`probe_runtime_load()` consolidates:
- **Free RAM** — `psutil.virtual_memory().available` (currently `probe_hardware()` reads `.total` only at [`ollama_service.py:375`](../../../backend/services/ollama_service.py#L375); needs extension).
- **Free VRAM** — NVML on NVIDIA, `ioreg` + `vm_stat` heuristics on Apple Silicon (unified memory).
- **Swap usage + page-out rate** — `psutil.swap_memory()` plus per-OS pressure indicators.
- **Loaded model set** — Ollama `GET /api/ps` (resident model name, size, expires_at). **Not currently called anywhere in the codebase.**

These signals stream over a single SSE channel from backend to frontend. Both the dispatcher and the runtime UI subscribe to the same source — no per-component polling.

### Runtime UI panel

The panel is a feature, not chrome. It surfaces:

- **RAM bar** — used by Jarvis / used by other apps / free, color-coded.
- **Currently loaded models** — name, slot, last-used, manual unload button.
- **Per-slot preference dropdown** — "for chat, prefer [Qwen3-30B-A3B ▼]" with alternatives + footprints visible.
- **Performance mode toggle** — `Quality` (load biggest fitting, swap aggressively), `Balanced` (default), `Lightweight` (stay small, never swap).
- **Low-memory banner** — surfaces when free RAM drops below threshold; provides specific next action ("free other apps" / "downgrade chat slot" / "end this conversation and start fresh on a smaller model").
- **Per-conversation loadout view** — what's pinned for the active conversation.

### Apple Silicon unload-then-load discipline

Unified memory does not release synchronously on `keep_alive: 0`. The dispatcher inserts an explicit barrier between unload and the next load: poll `/api/ps` until the target model is gone, with bounded retry and backoff. `vm_stat` purge events are a confirming signal but not the only signal.

### Tool-call format standardization

Per [`model-research-3.md`](../../research/models/model-research-3.md) §6: Qwen3 Hermes-style tool-call format is canonical. Granite, Devstral, and any other family are adapted at the edges (input adapter on prefill, output adapter on tool-use parsing). Conversation history is stored in canonical format. The current `tool_mode` classification at [`ollama_service.py:318`](../../../backend/services/ollama_service.py#L318) (`native` / `json_fallback` / `limited`) is the right shape; it hardens into:

- **`native_qwen3`** — speaks the canonical format directly.
- **`adapted`** — speaks via adapter (Granite 4, Devstral 2).
- **`excluded_from_tools`** — model is not used for tool dispatch (Phi-4, very small Qwen3 variants).

## Alternatives considered

### A. Static install-time picker (current architecture, kept)
The user picks one model that handles every request type. Wrong because the optimum varies by request class; a model good at tool-calling is not optimal at embeddings, and forcing one model to do both means accepting the worse trade in both. **Rejected as the v1 design.**

### B. Provider-pattern abstraction (`InferenceProvider`)
What the earlier draft of [`SELF-CONTAINED-APP-REVIEW.md`](../../SELF-CONTAINED-APP-REVIEW.md) proposed — single interface, swap implementations. Too narrow. Doesn't represent multi-slot loadout, doesn't represent per-class routing. **Rejected** — the right abstraction is `InferenceRouter`, not a one-model interface.

### C. Single MoE model (e.g., Qwen3-Next-80B-A3B everywhere)
A 3B-active MoE has small-model decode speed and big-model capability. Tempting as a single-model solution. Fails the 24 GB hardware floor (~45 GB Q4) and conflates request classes the same way option A does. **Rejected as a complete answer**, but adopted as the Tier B/C primary in the recommended stack ([`model-research-4.md`](../../research/models/model-research-4.md) §"Tier B").

### D. Cross-model KV cache reuse
Some inference frameworks claim cross-model KV reuse. Fragile; produces wrong-shaped attention silently. **Rejected.** Cross-model swap re-prefills from canonical conversation history.

### E. Dispatcher-side ML classifier as a hard requirement
Could ship a small classifier model to route requests. Adds another always-resident slot. Rule-based classifier covers the obvious cases (code blocks, tool requests, length thresholds). **Deferred** — start rule-based, add ML if accuracy demands it.

## Consequences

### Positive
- Each request class runs on a model optimized for it.
- The user can see what's happening at any moment. "Why is this slow?" is answerable in one glance.
- Hardware heterogeneity becomes a feature ("Jarvis runs on whatever Mac you have, picks the best model that fits, tells you when bigger hardware would help") rather than a "minimum RAM" wall.
- Footprint accounting is honest — KV-cache growth doesn't OOM mid-Jira-ingest invisibly.
- The router contract is the canonical place where profile, conversation pinning, and OOM behavior compose. One layer to test, one layer to audit.

### Negative
- Engineering depth. The router replaces a working single-model code path with a richer surface; multi-slot config schema migration is non-trivial.
- More state to surface in UI. The runtime panel is itself a meaningful component.
- Per-platform live-signal probing (NVML / `vm_stat` / Windows perf counters) requires per-OS validation. Half-built on one OS produces guessed dispatcher behavior on the others.
- The current [`local-models`](../../features/local-models.md) feature's static catalog and single-active-model semantics are partially obsoleted; the catalog data structure survives but the single-active-model surface goes away.

### What this changes about existing code
- New `backend/services/inference_router.py` (or similar).
- New `backend/services/runtime_load_probe.py` extending `probe_hardware()` with live signals.
- Multi-slot config schema replacing single `local_model.active` in `app/config.json`. Migration: existing single value fills the `chat` slot; other slots null until profile materializes them.
- [`chat.py:256`](../../../backend/routers/chat.py#L256) `_make_llm()` becomes a thin wrapper around `router.route()`.
- [`ollama_service.py:736`](../../../backend/services/ollama_service.py#L736) single-fixed `keep_alive` value replaced by a per-slot policy table.
- [`ModelCatalogEntry`](../../../backend/services/ollama_service.py#L113) gains `bytes_per_kv_token`, `effective_context_tokens`, `attention_arch`, `slot_class` fields.
- New `frontend/app/components/RuntimeModelPanel.vue` — RAM bar + loaded models + per-slot preferences + perf-mode toggle.
- The existing `tool_mode` classification hardens into `native_qwen3` / `adapted` / `excluded_from_tools`.

## Implementation phasing — what's feasible now vs blocked (2026-04-28)

The full design above describes the steady-state target. Several pieces depend on other ADRs that haven't shipped yet; this section is the honest sequencing call so a partial build doesn't pretend to be more than it is.

### Status (2026-04-28)

- **Catalog correctness pass: landed.** The factual errors flagged in [SELF-CONTAINED-APP-REVIEW.md §3](../../SELF-CONTAINED-APP-REVIEW.md#3-model-selection--licensing-posture) are corrected in [`ollama_service.py`](../../../backend/services/ollama_service.py): Qwen3 native context normalised to 32K (the 40K and 256K values were incorrect — they conflated YaRN-extended ranges and the `-Instruct-2507` long-context sibling), and Devstral Small 2 native normalised to 256K (384K was the RoPE-extended Ollama tag). The nonexistent "Gemma 4 27B" entry was renamed to its actual variant "Gemma 4 26B-A4B"; the missing models (Qwen3-14B, Qwen3-30B-A3B-Instruct-2507, Qwen3-4B-Instruct-2507, Granite 4.0 H-Micro/H-Tiny/H-Small) are now present in the catalog universe. **Per the code review on this work**, all entries with unverified Ollama registry tags carry `internal=True` so the user picker doesn't expose them — promotion to user-pickable requires verifying the tag against `ollama.com/library/<name>` and flipping the flag. The user-pickable surface is the 6 original entries with verified tags; the internal universe (7 entries) is reserved for the future router via `build_catalog(include_internal=True)`. Each internal entry carries a `TODO: verify Ollama tag` comment marking the verification gap.
- **Tool-format hardening: landed.** `tool_mode` taxonomy renamed `native` → `native_qwen3`, `json_fallback` → `adapted`, `limited` → `excluded_from_tools`. The new names match the dispatcher's intent (`native_qwen3` is the format the adapter standardises on; `adapted` is JSON-mode prompting via LiteLLM; `excluded_from_tools` is the policy decision the router takes for sub-2 GB models, not a model property). Frontend badge logic updated; backend tests, integration tests, and `done` event payloads now use the new names.
- **`probe_runtime_load()` + `GET /api/local/runtime/load`: landed (Python scaffold).** Pure-Python via `psutil.virtual_memory()` / `psutil.swap_memory()`, NVIDIA via `nvidia-smi` for Linux/Windows VRAM-used, and Ollama's `GET /api/ps` for the loaded-models list. Apple Silicon reports `gpu_vendor="apple"` with `gpu_vram_total_gb`/`gpu_vram_used_gb` as None — callers infer the unified-memory budget from `available_ram_gb`. When Ollama is unreachable the system signals still populate; only `loaded_models` and `ollama_reachable` reflect the gap. Per §"Blocked by upstream ADRs" below, the macOS branch graduates to a Tauri-side native helper after ADR 003 lands; today's psutil version is a working scaffold consumers can wire to.
- **`bytes_per_kv_token`, `attention_arch`, `slot_class` fields: landed.** All three fields are present on `ModelCatalogEntry` and populated for every catalog entry from `model-research-1.md` §"KV-cache discipline" reference numbers. Granite hybrid-mamba models report ~256–1024 bytes/token (state-space cache is fixed-size); Gemma SWA models ~1024–1536 bytes/token (sliding window only); transformer Qwen3/Devstral land at 2048–5120 bytes/token. `effective_footprint_bytes(entry, ctx_len_now)` is the predicate the dispatcher's `can_load(model)` will read once the production-grade memory-pressure signals land (ADR 003 gate).
- **InferenceRouter skeleton: landed.** New [`backend/services/inference_router.py`](../../../backend/services/inference_router.py) implements the rule-based classifier (`chat | tool | code`), `DispatchDecision` audit-trail dataclass, and `dispatch()` with profile-driven slot lookup. Wired into [`routers/chat.py`](../../../backend/routers/chat.py): `_make_llm()` routes through `get_router().dispatch()`, and `_handle_message` emits `decision.to_audit_dict()` in the WS `done` event's new `route` field for the future runtime UI panel. Behavior preservation: legacy single-model installs short-circuit via `_resolve_local_override` (user-picked model → identity dispatch). Profile-driven path activates when no model override is set. Downgrade-ladder walking and `pinned_loadout` enforcement remain deferred per the §"Blocked by upstream ADRs" list above. 46 tests in [`test_inference_router.py`](../../../backend/tests/test_inference_router.py) cover classifier corners, cloud/ollama dispatch, profile fallback, footprint accounting, keep_alive policy.
- **Per-slot `keep_alive` policy table: landed.** `KEEP_ALIVE_BY_SLOT` in [`ollama_service.py`](../../../backend/services/ollama_service.py) maps slot class to Ollama keep_alive semantics: `embedding`/`plumbing` → `-1` (forever-resident), `conversational`/`reasoning`/`long_context`/`best_local` → `30m` (today's behavior preservation), `code`/`vision` → `5m` (on-demand eviction). `warm_up_model()` consumes the policy via the catalog entry's `slot_class` — the consumer is wired, not orphaned. `keep_alive_for_slot(slot_class)` is the lookup helper.
- **ProfilePack scaffold: landed (3 of 9 profiles).** New [`backend/services/profile_service.py`](../../../backend/services/profile_service.py) implements ADR 005's schema (`ProfilePack` / `ProfileStack` / `SlotSpec` / `SlotLadder`) and a 3-profile starter catalog (`generic-knowledge-worker` default, `developer-devops`, `patent-prosecutor`) — enough variety to validate the schema on real shapes (no coder for patent, coder ladder for developer, smallest install for generic). Remaining 6 profiles in ADR 005's catalog table deferred until domain validation per ADR 005 §"Open follow-ups" #1. The router consumes `get_active_profile()` lazily on each dispatch so config edits take effect without a backend restart.

### Buildable today (no upstream dependency) — all landed 2026-04-28

These all landed in the 2026-04-28 chunk, in a behavior-preserving way — single-model production behavior is unchanged at the user level, but the codebase has moved to the right shape:

- **`InferenceRouter` skeleton.** ✓ Landed. Classifier (rule-based: tool-call → tool, code fence → code, default → chat), per-class slot table, `dispatch(request) → DispatchDecision` with profile-driven lookup. `_make_llm()` is a thin wrapper around `router.dispatch()` per the design. Behavior on legacy single-model installs is identical.
- **`probe_runtime_load()` via pure Python.** ✓ Landed. `psutil.virtual_memory()`, `psutil.swap_memory()`, Ollama's `GET /api/ps` over HTTP. Degraded vs the eventual native version, but consumers can wire to it.
- **`ModelCatalogEntry` field additions.** ✓ Landed. `bytes_per_kv_token`, `attention_arch`, `slot_class` populated for every catalog entry from `model-research-1.md` §"KV-cache discipline". `effective_footprint_bytes()` is the consumer.
- **Per-slot `keep_alive` policy table.** ✓ Landed. `KEEP_ALIVE_BY_SLOT` replaces the fixed value; `warm_up_model()` is the consumer.
- **Tool-format hardening.** ✓ Landed. `tool_mode` renamed `native_qwen3` / `adapted` / `excluded_from_tools`.

### Blocked by upstream ADRs

These are part of the design but cannot be built honestly until prerequisites ship:

- **Multi-profile slot tables.** Reference ADR 005's `ProfilePack`. Until ADR 005 is implemented, the router has exactly one profile (the active install) and the slot table is effectively static. Building "profile-driven slot tables" without ADR 005 means inventing the profile shape twice — once here, once in ADR 005 — and then resolving the conflict. Sequence: ADR 005 first, then this piece.
- **Conversation pinning enforcement.** ADR 008 defines the pinning contract; ADR 004 enforces it. If ADR 008's per-conversation `pinned_loadout` snapshot doesn't exist yet, the enforcement is a no-op (single chat slot is never swapped anyway). Defer the `accepts pinned_loadout` interface until ADR 008 lands the snapshot shape.
- **Production-grade Apple Silicon signals.** `vm_stat` parsing + page-out heuristics work in Python but are noticeably fragile across macOS versions (the M5 + macOS 26 combo we already hit on Ollama is a reminder that platform-quirks bite). The robust answer is a small Tauri-side native helper exposing memory pressure via the OS APIs. **Sequence: ADR 003 (Tauri packaging) first, then graduate the probe.** The Python version is a working scaffold in the meantime, not the shipping form.
- **Runtime UI panel.** Frontend can prototype the Vue component now (the SSE backend is buildable), but the *final* form depends on whether some panels move into native Tauri windows / menubar items. Same gating: prototype now, finalize after ADR 003.

### Useful sequencing call

The cleanest order top-to-bottom:

1. ADR 003 (Tauri packaging) — unblocks license, native signals, and finalizes the panel surface.
2. ADR 005 (profile-driven model stacks) — produces the `ProfilePack` shape the router consumes.
3. ADR 008 (conversation pinning) — produces the `pinned_loadout` shape the router enforces.
4. **This ADR's full implementation** — composes all three above into the router contract.

What can land out of order without harm:

- The `InferenceRouter` skeleton + Python `probe_runtime_load()` + catalog field additions + `keep_alive` policy table — all of these are forward-compatible with the upstream ADRs and don't lock anything in.

What should NOT land out of order:

- Anything that bakes assumptions about the `ProfilePack` schema (creates ADR-005 churn).
- Anything that ships native-signal probing as production-grade before Tauri lands (creates a "we shipped fragile platform code we have to maintain forever" liability).
- The runtime UI panel as a final user-facing surface (creates a redesign cost when Tauri lands).

### Interaction with active ADR-010 eval run

The router work (skeleton + probe + catalog fields + keep_alive table) touches files the conversation-replay eval does not import: `routers/chat.py`'s `_make_llm`, `services/ollama_service.py`, and a new `services/inference_router.py`. The eval harness's `OllamaChat` adapter talks to Ollama directly and never goes through `_make_llm`. Concurrent work is safe at the source-file level; only commit timing needs care so a mid-run commit doesn't capture eval files in an inconsistent state.

## Open follow-ups (non-blocking)

1. **Live-load probe** is the foundation. Until it produces real signals on Mac and Windows, every later piece of the dispatcher is built against guessed numbers. Build first; verify both platforms before extending.
2. **Multi-slot config schema migration ADR.** Forward-compatible with `ProfilePack`. Ideally one-shot lossless migration of existing single-active-model installs.
3. **Request classifier accuracy** — measurable via the existing [`eval-baseline`](../../concepts/eval-baseline.md) harness once routing is wired. Rule-based first; ML upgrade only if measured accuracy demands it.
4. **Tool-format adapter quality across families** — research-3 §6 said standardize on Qwen3, but adapter quality is unmeasured per family. Add per-family adapter tests as Granite / Devstral land in profiles.
5. **Embedding-model context budget** — embeddings have their own context budget (Qwen3-Embedding-0.6B caps at 32K input). Ingest pipelines must respect it; surface to the runtime panel if exceeded.
