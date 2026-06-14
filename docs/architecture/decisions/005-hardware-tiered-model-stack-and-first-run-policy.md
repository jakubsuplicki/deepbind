# ADR 005 — Hardware-tiered model stack, first-run pull policy, and downgrade ladder

**Status:** Accepted
**Date:** 2026-04-30
**Related:** [ADR 002](002-pure-local-product-shape.md) · [ADR 003](003-desktop-distribution-tauri-and-sidecars.md) · [ADR 012](012-chat-model-self-test.md)

## Context

[ADR 003](003-desktop-distribution-tauri-and-sidecars.md) settles distribution shape (Tauri shell + PyInstaller backend + bundled Ollama sidecar — graduated through G4a). [ADR 012](012-chat-model-self-test.md) settles per-machine *selection* among already-installed models. Neither answers what is installed in the first place.

Three open questions blocked [G4b](../../features/desktop-shell-graduation.md#g4--bundled-ollama-sidecar-split-into-g4a--g4b) — first-run model-pull UX:

1. **Catalog.** Which models are user-pickable in v1, and under what license filter?
2. **First-run policy.** What does the bundled app pull on first launch? Probe-driven or static? Single model or multiple? Block UI or stream in the background?
3. **Memory-pressure response.** When free RAM drops below the active model's working set during a session, what happens? OOM crash, eject the chat model entirely, or graceful downgrade to a smaller already-installed model?

The model-research work (final, 2026-04) did the research — license filter, qualifying universe, hardware tiering, recommended primary stack — but was never crystallized as a load-bearing decision document. This ADR locks it in.

## Decision drivers

1. **License purity** — Apache 2.0 / plain MIT only. No Gemma Terms of Use flow-down, no DeepSeek custom license, no Z.ai (Entity-Listed since Jan 2025), no Llama community license. Compliance-focused operators in defense / privacy / regulated verticals do an *outbound license review* of every model in the bundle; one non-permissive entry blocks procurement. Per the model-research §"License filter (settled)."
2. **Self-contained from minute zero** — driver #4 of ADR 003. The product must work for some non-trivial value of "work" before a single byte of LLM weights downloads. This means *embedding + plumbing-classifier* are bundled (G2b done); chat is the exception that pulls on first run via signed manifest.
3. **Hardware heterogeneity is real** — the operator runs anything from a 16 GB compliance laptop to a dual-GPU workstation. A one-size-fits-all default either underutilizes the workstation or OOMs the laptop. Tiering is the architectural answer.
4. **First-run UX is the first compliance impression** — the operator's first session decides whether they trust the product. Crashing during onboarding because we picked too-big a default is irrecoverable. **Conservative-default + opt-in upgrade** beats **aspirational-default + crash-loop**.
5. **Memory pressure is normal, not exceptional** — chat sessions accumulate KV cache; vault indexing co-resident in the same process consumes RAM; the user has Slack open. Eventually the active model's working set won't fit. Graceful downgrade to a smaller already-installed model is the correct response — *not* OOM, *not* stalling indefinitely on swap. The fallback must already be on disk; lazy-pulling on OOM means crashing first.

## Decision

### A. Catalog (frozen for v1)

The full per-tier catalog is the **Recommended primary stack (final)** catalog. This ADR adopts that catalog verbatim as the user-pickable surface; entries that don't appear there are not user-pickable in v1.

**Always-resident (bundled into installer, audited at signing time):**

| Use case | Model | License | Quant | Disk | Status |
|---|---|---|---|---|---|
| Embeddings | Qwen3-Embedding-0.6B | Apache 2.0 | Q8 | ~600 MB | ✅ bundled in G2b (via fastembed) |
| NER (Polish) | spaCy `pl_core_news_sm` | MIT | — | ~25 MB | ✅ bundled in G2b |
| NER (English) | spaCy `en_core_web_sm` | MIT | — | ~15 MB | ✅ bundled in G2b |

**Pulled on first run (per the §B policy below):**

Tier A — 24 GB Apple Silicon, 16 GB unified, RTX 4060/4070 8 GB, 32 GB CPU-only:

| Use case | Model | Quant | RAM (est.) | Role |
|---|---|---|---|---|
| Conversational primary | Qwen3-8B | Q4_K_M | ~6 GB | first-run default |
| Conversational fallback | Qwen3-4B-Instruct-2507 | Q4_K_M | ~3 GB | downgrade ladder target |
| Conversational ceiling (opt-in) | Qwen3-30B-A3B-Instruct-2507 | Q4_K_M | ~20 GB | user picks from settings; tight on 24 GB |
| Reasoning (opt-in) | Qwen3-30B-A3B-Thinking-2507 | Q4_K_M | ~20 GB | duel mode, settings-only |
| Coding (opt-in) | Qwen3-Coder-30B-A3B-Instruct | Q4_K_M | ~20 GB | engineering vertical |

Tier B — 48–64 GB unified, RTX 4090 24 GB, dual 3090 48 GB. Same as A plus:

| Use case | Model | Quant | RAM (est.) | Role |
|---|---|---|---|---|
| Conversational primary | Qwen3-30B-A3B-Instruct-2507 | Q4_K_M | ~20 GB | first-run default for Tier B |
| Reasoning primary | Qwen3-30B-A3B-Thinking-2507 | Q4_K_M | ~20 GB | duel-mode opt-in |
| Plumbing upgrade (opt-in) | Granite 4.0 H-Tiny | Q5_K_M | ~6 GB | replaces bundled embedding/NER if user wants higher quality |
| Compliance reasoning (opt-in) | OLMo 3.1-32B-Think | Q4_K_M | ~18 GB | for compliance-sensitive deployments that want the most-permissive-license stack |

Tier C — 96+ GB unified, H100, dual A100, MI300X. Same as B plus:

| Use case | Model | Quant | RAM (est.) | Role |
|---|---|---|---|---|
| Conversational frontier | gpt-oss-120b | Native MXFP4 | ~63 GB | first-run default for Tier C (single H100) |
| Reasoning frontier | Qwen3-235B-A22B-Thinking-2507 | Q4_K_M | ~120–140 GB | dual-GPU / multi-GPU |
| Coding frontier | Qwen3-Coder-480B-A35B-Instruct-FP8 | FP8 | ~480 GB | multi-GPU, niche |

**Catalog discipline:** every entry is Apache 2.0 or plain MIT. Every entry has a *verified Ollama tag* before becoming user-pickable; entries with unverified tags carry `internal=True` in [`backend/services/ollama_service.py`](../../../backend/services/ollama_service.py) and are filtered from the picker until verification (existing convention). Adding or removing entries from the catalog is an ADR amendment, not a quiet code change.

### B. First-run pull policy (probe → pull primary → background-pull fallback)

On first launch (no `<app_data>/.first_run_complete` marker), the desktop app:

1. **Probes hardware** via the existing [`probe_runtime_load()`](../../../backend/services/ollama_service.py) — total RAM, GPU presence, accelerator class.
2. **Maps to a tier (A / B / C)** using a bright-line table baked into the catalog. Conservative on the boundary: 24 GB Apple Silicon is firmly Tier A; 32 GB unified is Tier A unless GPU >= 16 GB; 48 GB+ with discrete GPU is Tier B; 96 GB+ or H100-class is Tier C.
3. **Picks the tier's first-run primary** (the row labeled "first-run default" in the catalog above).
4. **Pulls the primary in the foreground**, blocking the chat-ready UI state. Progress UI uses the existing toast pattern from G5 (cold-start reindex pill — same shape, different label). Estimated time: ~3–5 min for Qwen3-8B on a 100 Mbit connection.
5. **Pulls the tier's downgrade-fallback in the background** — chat is ready as soon as the primary lands; the fallback completes silently while the user explores. Tier A fallback is Qwen3-4B (~3 GB).
6. **Writes `.first_run_complete`** when the primary lands. Subsequent launches skip the entire pipeline.
7. **Runs the chat-model-probe** ([ADR 012](012-chat-model-self-test.md)) once both pulls complete to confirm correctness/speed/fit on the user's exact (Ollama version × macOS version × hardware) tuple. The probe pins the active chat model.

**Pull mechanism:** uses Ollama's `/api/pull` (we already speak it via [`backend/services/ollama_service.py`](../../../backend/services/ollama_service.py)), but the *blob URL + SHA-256* are verified against our signed manifest, not Ollama's tag-mutable registry. Per [ADR 003 §"First-launch model fetch"](003-desktop-distribution-tauri-and-sidecars.md#first-launch-model-fetch-the-one-outbound-call-we-accept-at-install-time): Ollama tags drift; SHAs do not.

**Resumability:** half-pulled blobs on a flaky connection resume from the last block, not restart. Ollama's pull supports this natively; our manifest's SHA verification runs only on the *final* assembled blob, so partial-resume is safe.

**Skip / opt-out:** the first-run modal includes a "I'll pick my own model later" affordance. Choosing it skips the primary pull; the user lands in chat with no chat model installed and the UI surfaces a clear "no chat model — pick one in settings" empty state. Embeddings + NER still work (they're bundled). The marker file is *not* written; next launch re-prompts.

### C. Downgrade ladder

When chat-time RAM pressure makes the active model unrunnable, the runtime swaps to the next-smaller already-installed model on the tier's ladder rather than crashing.

**Ladder per tier (top → floor):**

- Tier A: Qwen3-30B-A3B (if user opted in) → Qwen3-8B → **Qwen3-4B-Instruct-2507** → floor (refuse, surface "free up RAM" UI). The -2507 variant (256K-native) is chosen over the base 32K Qwen3-4B per §A "Tier A — Conversational fallback Qwen3-4B-Instruct-2507 (downgrade ladder target)" — long-context capability matters more than throughput on the constrained-hardware floor where this fallback fires.
- Tier B: gpt-oss-120b (if user opted in) → Qwen3-30B-A3B → Qwen3-8B → Qwen3-4B → floor
- Tier C: gpt-oss-120b → Qwen3-30B-A3B → Qwen3-8B → Qwen3-4B → floor

**Triggers (in priority order):**

1. **OOM during inference** — Ollama returns an OOM error or the request fails with a memory exhaustion code. The router catches it, walks one step down the ladder, retries the same turn against the smaller model. The user sees a one-line toast "Switched to {smaller_model} — RAM pressure" and the response.
2. **Observed throughput vs probe baseline (advisory only — amended 2026-05-01).** Replaces the original pre-flight memory check, which has been removed. Each completed turn carries Ollama's authoritative `eval_count / eval_duration` from the `done` event through to the chat router as a `metrics` payload on the WS `done` event. The frontend's `useChatHealth` composable rolls a 5-turn window of observed `decode_tps` per model and compares it against that model's probe baseline (ADR 012's `realistic_tps`, captured per-machine at install). When the full window stays below 50% of baseline, a single soft-hint toast surfaces (cooldown 10 min/model): *"{model} is running at ~{n}% of expected speed. Try a smaller model or close other apps."* When the full window stays above 105% of baseline AND a heavier installed catalog rung also has a passing probe baseline, a complementary hint surfaces (cooldown 24h/model): *"You may have headroom for {heavier rung}."* Both toasts carry a "Re-test models" action that lands at the settings → chat-model-probe panel. **Strict non-goals:** never gates dispatch, never auto-swaps models, never refuses a turn. The OOM-retry (trigger 1) remains the safety net for actual memory exhaustion. Why this replaces the pre-flight check: the original gated dispatch on `psutil.virtual_memory().available × 0.8 ≥ effective_footprint`, but on macOS unified memory `psutil.available` excludes the reclaimable inactive/cached pool that Ollama mmap-loading uses — producing the very "Insufficient RAM" failure mode it was meant to prevent (empirical: a 24 GB Apple Silicon refused an 8B model in-app while a 30B loaded fine from the terminal). The observed-vs-baseline comparison sidesteps the prediction problem entirely — we measure what the model *actually does*, not what we think it can do.
3. **Explicit user toggle** — `Settings → Lightweight mode` pins the active model to the floor of the ladder regardless of pressure. Useful when the user has Slack + Spotify + four Chrome windows and wants chat to "just work" without negotiating with the OS for memory.

**Floor behavior:** when the smallest fallback won't fit either, the router refuses the turn with a `503` "insufficient resources" error. Frontend surfaces a recovery UI: list current memory consumers, offer to close other apps, link to Settings → Performance mode. Never a crash.

**Why the fallback must already be on disk:** if we lazy-pull on first OOM, the user sees a multi-minute hang at the worst possible moment — mid-conversation. The §B policy pre-pulls the tier's fallback exactly so this never happens.

### D. Bundled-vs-pulled boundary

| Asset | Bundled? | Why |
|---|---|---|
| Qwen3-Embedding-0.6B (ONNX, Q8) | ✅ Yes | Always-resident; semantic search must work pre-pull |
| spaCy `pl_core_news_sm` + `en_core_web_sm` | ✅ Yes | NER + entity extraction must work pre-pull |
| Granite 4.0 H-Micro (plumbing classifier) | ❌ No (deferred) | Originally listed in the model-research as bundled. Deferred to v1.1: it's not load-bearing for v1 (existing rule-based classifier is good enough), and bundling 1.8 GB of GGUF for a non-critical model is a bigger commitment than v1 needs. Revisit when classifier accuracy becomes a real bottleneck. |
| Any chat / reasoning / coding model | ❌ No | Pulled on first run (§B) per ADR 003 §"First-launch model fetch." Tier-dependent; user-controlled via opt-in for ceiling models. |
| TTS (Kokoro-82M) | ❌ Out of scope | Voice output is v1.1+ (per ADR 007 — voice input dropped). |

The bundled set is intentionally minimal. Every additional bundled byte is a license review the operator does before procurement; every additional bundled model is a notarization re-cert when it's bumped. v1 ships only the always-resident plumbing.

## Alternatives considered

- **Static default model regardless of hardware** (e.g. always pull Qwen3-4B). Avoids the probe complexity. Rejected: an operator on a 64 GB workstation would correctly conclude we shipped them a toy. Conversely, a static Qwen3-30B default on a 24 GB Mac OOMs in onboarding. The asymmetry argues for tiering even at the cost of probe code.
- **No first-run pull — start with no chat model, surface a picker.** Cleanest from a "what does the bundle do?" perspective. Rejected: the first-run experience becomes "click around for ten minutes choosing a model"; chat doesn't work for the duration. Onboarding momentum dies. Operators in compliance verticals are not patient with empty-state UX.
- **Bundle one chat model in the installer.** Eliminates first-run pull. Rejected for v1: the installer surface is part of what the operator license-reviews; bundling 5–80 GB of chat-model weights *into the signed installer* triggers a much bigger compliance review than fetching the same blob via signed manifest at first run. Also: the right model for one operator's hardware is the wrong model for another's. Tiered first-run pull side-steps both.
- **Lazy-pull the downgrade fallback on first OOM.** Avoids ~3 GB on first-run download. Rejected: see §C — first OOM is mid-session, lazy-pull means a multi-minute hang at the worst possible moment.
- **Cloud-provider fallback when local model OOMs.** Eliminates the floor-refusal failure mode. Rejected: contradicts ADR 002 ("pure local product shape") and the planned [ADR 014](014-desktop-bundle-excludes-cloud-providers.md) (cloud providers excluded from desktop bundle). The floor-refusal case is correct behavior for a privacy-positioned product.

## Consequences

### Positive

- One coherent decision-document for "which models, when, how" — replaces three different load-bearing assumptions scattered across the codebase + research notes.
- Hardware-aware first-run UX gives a sane default for every operator's machine.
- Downgrade ladder makes memory pressure a graceful degradation, not a failure mode. Compliance-focused operators explicitly ask "what happens when the laptop is constrained?" — this is the answer.
- Catalog discipline (ADR-amendment-required to add/remove entries) prevents catalog drift between code and docs.
- License purity (Apache 2.0 / plain MIT only) is structurally enforced — every entry passes review without exception.

### Negative

- First-run download is non-trivial (~5 GB on Tier A primary + ~3 GB Tier A fallback in background). Bandwidth-constrained users feel this. Mitigated by:
  - Background-streaming the fallback (chat works the moment the primary lands).
  - Resumable pulls (no restart on flaky connection).
  - "Skip — pick later" escape hatch if the user wants to defer entirely.
- Hardware probe heuristics need maintenance: as new hardware classes appear (M5 Max, RTX 5090, M-Ultra variants), the tier mapping table needs revision. Tracked as a v1.1 item.
- Tier C (gpt-oss-120b first-run default) requires a 100 GB+ free disk check; surfacing this as a clear pre-flight rather than a mid-pull failure is its own UX subtask.

### What this changes about existing code

- [`backend/services/ollama_service.py`](../../../backend/services/ollama_service.py) — `probe_runtime_load()` already exists; needs a `tier_for_hardware()` helper that returns A/B/C plus the tier's first-run-default model id.
- New service `backend/services/first_run_orchestrator.py` — state machine wraps the §B pipeline (probe → pull primary → pull fallback → run chat-model-probe → mark complete).
- New endpoints `POST /api/local/first-run/start` + `GET /api/local/first-run/status` — same shape as the existing `/api/memory/reindex/status` from G5.
- New service `backend/services/memory_pressure_monitor.py` — watches free RAM, emits a `pressure` event when crossing the 80% headroom threshold; the chat router subscribes and triggers ladder swap. Reuses `effective_footprint_bytes()` from ADR 012.
- Frontend modal in `OnboardingLocalFlow.vue` for the first-run pull pipeline.
- Settings → "Lightweight mode" toggle (§C trigger #3) wired into [`ollama_service.py`](../../../backend/services/ollama_service.py)'s active-model-pinning path.
- §C trigger 2 surface (amended 2026-05-01): `OllamaDispatcher` forwards Ollama's per-stage durations (`eval_duration`, `prompt_eval_duration`, `load_duration`, `total_duration`) on the `usage` `StreamEvent`; [`backend/routers/chat.py`](../../../backend/routers/chat.py) accumulates a per-turn metrics payload (`decode_tps`, `prefill_tps`, `ttft_ms`, `load_ms`, `total_ms`, `eval_count`, `prompt_eval_count`) and emits it on the `done` WS event. Frontend [`useChatHealth`](../../../frontend/app/composables/useChatHealth.ts) loads probe baselines from `GET /api/local/chat-model-probe`, rolls a per-model 5-turn window, and emits the soft-hint toast on sustained drift. [`ChatPanel.vue`](../../../frontend/app/components/ChatPanel.vue) renders a per-turn telemetry pill (`12.4 t/s · 0.85s`) tinted by current health status, plus a chat-side "Re-test models" advisory banner that surfaces only while the latest turn classifies as `slow`.

## Migration path (G4b sequencing)

The Ollama process plumbing (G4a) is done. G4b's chunks land in this order:

1. **Catalog freeze + tier mapping (G4b1)** ✅ landed 2026-04-30. [`backend/services/ollama_service.py`](../../../backend/services/ollama_service.py) gains three new `ModelCatalogEntry` fields — `license: Literal["Apache-2.0", "MIT", "non-permissive"]`, `ladder_positions: Dict[str, int]`, `first_run_default_tiers: List[str]` — and three module-level helpers: `tier_for_hardware()`, `first_run_default_for()`, `downgrade_ladder_for()`. Existing Qwen3 + Granite entries are annotated with their tier roles per §A; non-Apache/MIT entries (Gemma, Mistral) carry `license="non-permissive"` for filter-time enforcement (a separate cleanup chunk graduates the picker-side filter and removes the entries — gated on test rewrites). Two new entries land for tier completeness: `qwen3-30b-a3b-thinking-2507` (Tier B reasoning primary) and `gpt-oss-120b` (Tier C first-run primary), both `internal=True` until Ollama tags verify. Boundary tests in [`backend/tests/test_local_models.py`](../../../backend/tests/test_local_models.py) cover the §A tier table inflection points.

   **Amendment 2026-05-05 — catalog cleanup landed (closes the deferred follow-up).** The four entries that had been carrying `license="non-permissive"` as transitional metadata — `ministral-3-8b` + `devstral-small-2-24b` (Mistral Research License) and `gemma4-e4b` + `gemma4-26b-a4b` (Gemma Terms of Use) — are deleted from `MODEL_CATALOG`. The `license` field's `Literal` type narrows to `["Apache-2.0", "MIT"]`, which makes any future re-introduction a Pydantic `ValidationError` at construction time rather than a metadata-only flag. Catalog now 11 entries (was 15), all Apache-2.0; user-pickable subset shrank from 6 → 3 (qwen3-1.7b/4b/8b). The `code` preset is intentionally empty — devstral was the only entry with `preset="code"` and no Apache-2.0 / MIT replacement exists in the v1 catalog; coding workloads route to `qwen3-30b-a3b-instruct-2507` / `granite-4-h-small` via the `balanced` / `best-local` presets. Tests rewritten to a positive `test_no_non_permissive_entries` invariant + a `test_removed_non_permissive_ids_stay_removed` regression guard. Closes the commercial-licensing audit finding #6 and finding #7 (the latter was contingent on #6: with no non-permissive entries in the catalog, the tokenizer-fetch path in `services/token_counting.py` cannot resolve a non-permissive parent license).
2. **First-run orchestrator + endpoints (G4b2)** ✅ landed 2026-04-30. New service [`backend/services/first_run_orchestrator.py`](../../../backend/services/first_run_orchestrator.py) owns the §B state machine: probing → pulling_primary → (marker write at `<workspace>/app/.first_run_complete`) → pulling_fallback → running_probe → complete. Mirrors the reindex-supervisor singleton shape (module-level state + `asyncio.Lock` + single `asyncio.Task` + lifespan cancel). Two divergences from that pattern: the marker is written *mid*-pipeline (as soon as the foreground primary lands the user can chat — fallback + probe continue in the background); fallback-pull and probe failures are non-fatal (recorded as `fallback_failed` / `probe_failed` flags but state still advances to `complete`). Skip path (per §B "Skip / opt-out") writes no marker. New endpoints in [`backend/routers/local_models.py`](../../../backend/routers/local_models.py): `POST /api/local/first-run/start` (idempotent — `started`/`already_running`/`already_complete`/`skipped`) and `GET /api/local/first-run/status` (mirrors `/api/memory/reindex/status` shape from G5 — frontend polls 1×/s). To consume Ollama's pull stream without parsing SSE, [`ollama_service.py`](../../../backend/services/ollama_service.py) factored out `pull_model_events()` (raw-dict async generator); `pull_model_stream()` is now a thin SSE wrapper over it. Coverage in [`backend/tests/test_first_run_orchestrator.py`](../../../backend/tests/test_first_run_orchestrator.py).
3. **Frontend first-run modal (G4b3)** ✅ landed 2026-04-30. New composable [`frontend/app/composables/useFirstRun.ts`](../../../frontend/app/composables/useFirstRun.ts) polls `GET /api/local/first-run/status` at 1 Hz while the orchestrator is mid-pipeline; mirrors the singleton-supervisor shape of [`useReindexStatus`](../../../frontend/app/composables/useReindexStatus.ts) (refcount + Nuxt `useState` + lifecycle hooks). Exposes the §B state machine plus computed helpers — `chatReady` (true the moment the foreground primary lands so the user is released into chat while fallback + probe continue silently), `active`, `finished`, `stageLabel`. [`frontend/app/components/OnboardingLocalFlow.vue`](../../../frontend/app/components/OnboardingLocalFlow.vue) rewritten as a two-layer wizard: **Layer 1** (orchestrator-driven, default in the bundled build) drives the §B pipeline UI — probing label → pulling_primary progress bar with bytes/pct + "I'll pick my own model later" skip → chatReady ready screen with a non-blocking background-fallback indicator + Open Jarvis button → complete ready screen. **Layer 2** (legacy `useLocalSetupFlow` manual picker) is kept verbatim for the §B skip path AND the dev-mode fallback when `localModels.isOllamaReady()` is false at mount. Step indicators redesigned to "Detect hardware → Download model → Start using Jarvis" (replaces the legacy "Install runtime → Choose model" which assumed the user installs Ollama themselves). Marker-present early return: if `firstRun.status.marker_present` is true at mount the wizard emits `model-ready` immediately and bails — second/third launches with the marker on disk skip the wizard entirely (per §B step 6). Wiring into [`pages/onboarding.vue`](../../../frontend/app/pages/onboarding.vue) is unchanged (same `model-ready` / `back` emits contract).
4. **Memory-pressure monitor + downgrade ladder (G4b4)** ✅ landed 2026-04-30. New service [`backend/services/memory_pressure_monitor.py`](../../../backend/services/memory_pressure_monitor.py) exposes three synchronous helpers — `current_free_ram_bytes()` (psutil wrapper, falls back to 0 to force floor refusal when psutil is unavailable), `check_can_run(entry, ctx_len_tokens, ...)` (pass if `effective_footprint_bytes` ≤ `free × 0.80`), and `pick_runnable_model(requested, *, tier, ctx_len_tokens, installed_ollama_tags, ...)` which walks `downgrade_ladder_for(tier)` from the requested model toward the floor and returns a `MemoryPressureSwap` with `chosen` (None on floor refusal), `did_swap`, `reason`, and a per-step `trail` for telemetry. **Design pushback**: ADR §149 originally described this as a watcher with `pressure` events; rejected in favour of a synchronous predicate because the two triggers it serves (OOM-during-inference catch + pre-flight) both want a fast "does this still fit *right now*?" question, not an event firehose — see the module's design-note comment for the full reasoning. Plus a fourth helper `looks_like_oom(error_message)` for the §C trigger 1 OOM-string match (Ollama returns plain-text errors, no structured codes) and a fifth `find_entry_by_litellm_or_ollama(model)` reverse-lookup for the chat-router integration. [`backend/routers/chat.py`](../../../backend/routers/chat.py) gains two private helpers (`_apply_memory_pressure_swap`, `_ladder_step_after_oom`) and integrates them at two points: pre-flight swap before `_make_llm` (§C trigger 2 — looks at `prompt_stats.context_tokens`, walks the ladder, emits a `warning` event on swap or a fatal `error` + `done` on floor refusal); OOM-retry around the initial stream loop (§C trigger 1 — catches Ollama errors matching the OOM signature *before any text streams*, walks one further ladder step, recreates the LLMService against the smaller model, retries the same turn once). Text-already-streamed OOMs flow through as plain error events — no double-emission. Coverage: 18 unit tests in [`backend/tests/test_memory_pressure_monitor.py`](../../../backend/tests/test_memory_pressure_monitor.py) (predicate, ladder picker happy path + pressure swap + uninstalled-step walkthrough + floor refusal + never-upgrade invariant + tier B ladder, OOM string matcher, lookup helper, free-RAM fallback) + 7 router-integration tests in [`backend/tests/test_chat_memory_pressure.py`](../../../backend/tests/test_chat_memory_pressure.py) (preflight no-swap for cloud, preflight swap emits warning, preflight floor refusal short-circuits before LLM, OOM pre-text triggers ladder retry, OOM with no fallback emits error, OOM after text does NOT retry, non-OOM error doesn't trigger retry).
5. **Lightweight-mode toggle (G4b5)** ✅ landed 2026-04-30. Implements §C trigger 3 — the user's explicit "just work, don't auto-downgrade" pin. New helper [`floor_entry_for_tier(tier, *, installed_ollama_tags)`](../../../backend/services/memory_pressure_monitor.py) returns the smallest installed entry on the tier's ladder. New endpoints in [`backend/routers/settings.py`](../../../backend/routers/settings.py): `GET /api/settings/lightweight-mode` and `PATCH /api/settings/lightweight-mode` with body `{enabled: bool}`. Persistence rides the existing `preference_service` (workspace `app/preferences.json` as `"lightweight_mode": "true"|"false"`). Chat router integration: [`_apply_memory_pressure_swap`](../../../backend/routers/chat.py) gains a short-circuit branch — when `_is_lightweight_mode_on()` returns True and the floor is a different entry from the requested one, the helper emits a "Lightweight mode — using {floor}" warning and returns the floor immediately, *before* the pressure-aware `pick_runnable_model` walk runs. When the requested model is already the floor (or no installed-on-ladder entry exists) the helper falls through to the normal pre-flight which is a no-op. New frontend composable [`useLightweightMode.ts`](../../../frontend/app/composables/settings/useLightweightMode.ts) (mirrors `usePrivacySettings`'s shape) and component [`PerformanceSection.vue`](../../../frontend/app/components/settings/PerformanceSection.vue) added to the Settings page between LocalModels and Workspace; collapsible section with a status badge ("🪶 Lightweight mode active" / "Auto") and a toggle. Coverage: 4 new unit tests for `floor_entry_for_tier` (happy path Tier A, skips uninstalled floor, none when nothing installed, none when only off-ladder models installed) + 5 endpoint tests in `test_settings_api.py` (default off, toggle-on persists across re-read, toggle-off, rejects non-bool, rejects missing field) + 2 chat-router integration tests in `test_chat_memory_pressure.py` (lightweight short-circuits to floor with the correct warning, no-op when already at floor). 216 passing across the full G4b + settings regression suite.

6. **End-to-end test (G4b6)** — split into automated + manual halves.
   - **Automated** ✅ landed 2026-04-30. [`backend/tests/test_g4b_cold_launch.py`](../../../backend/tests/test_g4b_cold_launch.py) simulates the full first-launch sequence across G4b1–G4b5 wiring with mocked Ollama: fresh workspace (no marker) → orchestrator runs the §B pipeline (probe → primary pull → marker → fallback pull → probe) → marker present → idempotent re-call returns `already_complete` → chat-router pre-flight pressure check resolves cleanly against the just-installed catalog (no swap when free RAM is ample, swaps to fallback under tight RAM) → lightweight-mode floor lookup returns the smallest installed entry. Catches integration drift across the five chunks that any single unit suite would miss.
   - **Manual** — fresh `<app_data>` directory, cold-launch the bundled+notarized app, watch the pipeline complete in the modal, verify the chat-model-probe pinned the right model. Pending G4a re-notarization (separate user-authorized step).

Each chunk is independently testable; the bundle does not have to be re-notarized between chunks. Notarization is a single round at the end of G4b alongside G4a.

## Open follow-ups

1. **Granite 4.0 H-Micro plumbing-model bundling** — deferred to v1.1. Trigger to revisit: real-world classifier accuracy ceiling on the existing rule-based path.
2. **TTS bundling** — Kokoro-82M is in the catalog but voice output is v1.1+. Will fold into the §D bundled list when voice scope returns.
3. **Tier mapping for non-x86_64 / non-arm64 hardware** — Linux on ARM, RISC-V workstation, etc. Defer to ADR 003 §C (Linux v1.1 amendment).
4. **Manifest-driven catalog** — eventually the catalog should be served from the signed manifest, so adding a model to the picker doesn't require a desktop bundle re-cut. v2 concern.
5. **Per-vertical model packs** — the model-research §"Strategic angle" hints at a defense-engineering pack, a legal pack, etc. Out of scope for v1; revisit when the engineering vertical matures.
