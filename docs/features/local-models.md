---
title: Local Models
status: active
type: feature
sources:
	- backend/services/ollama_service.py
	- backend/services/ollama_dispatcher.py
	- backend/services/system_prompt.py
	- backend/services/first_run_orchestrator.py
	- backend/services/memory_pressure_monitor.py
	- backend/routers/local_models.py
	- backend/routers/chat.py
	- backend/tests/test_local_models.py
	- backend/tests/test_local_models_integration.py
	- backend/tests/test_ollama_dispatcher.py
	- backend/tests/test_first_run_orchestrator.py
	- backend/tests/test_memory_pressure_monitor.py
	- backend/tests/test_chat_memory_pressure.py
	- frontend/app/composables/useLocalModels.ts
	- frontend/app/composables/useLocalSetupFlow.ts
	- frontend/app/composables/useFirstRun.ts
	- frontend/app/composables/useChatModel.ts
	- frontend/app/composables/settings/useLightweightMode.ts
	- frontend/app/composables/useChat.ts
	- frontend/app/components/OllamaStatus.vue
	- frontend/app/components/LocalModelCard.vue
	- frontend/app/components/ModelSelector.vue
	- frontend/app/components/PullProgress.vue
	- frontend/app/components/OnboardingLocalFlow.vue
	- frontend/app/components/ChatPanel.vue
	- frontend/app/components/StatusBar.vue
	- frontend/app/pages/main.vue
	- frontend/app/pages/settings.vue
	- frontend/app/types/index.ts
depends_on: [chat, workspace-onboarding]
last_reviewed: 2026-04-30
last_updated: 2026-05-04
---

# Local Models

Run Jarvis with on-device AI via Ollama — no API key required.

## Summary

Per [ADR 015](../architecture/decisions/015-single-target-local-only-stack.md), Ollama is the **only** chat-dispatch target — there is no multi-provider abstraction, no LiteLLM, and no cloud SDKs in the codebase. This feature owns the full local stack: hardware detection, a curated catalog of model presets with hardware-based recommendations, the [`OllamaDispatcher`](../../backend/services/ollama_dispatcher.py) that streams `ollama.AsyncClient` events into the router's `StreamEvent` shape, model download with streaming progress, tool-calling mode detection per model, runtime health monitoring with reconnection flow, and slow response indicators.

The catalog is split into two layers: **user-pickable chat models** (3 entries with verified Ollama tags — `qwen3-1.7b`, `qwen3-4b`, `qwen3-8b`) and **internal entries** (8 entries — Qwen3-2507 split fine-tunes, Qwen3-14B, Qwen3-30B-A3B-Instruct-2507, Qwen3-30B-A3B-Thinking-2507, gpt-oss-120b, Granite 4.0 H-Micro/H-Tiny/H-Small — all carry `internal=True` because their Ollama registry tags are unverified). The user-facing endpoint filters internal entries out so a stale-tag pull never 404s the customer; callers that need the full universe (e.g. footprint planning, ADR 005 first-run policy, downgrade ladder lookup) pass `build_catalog(include_internal=True)`. Each internal entry carries a `TODO: verify Ollama tag` comment marking the verification gap; promotion to user-pickable requires `ollama pull <tag>` against the live registry, then flipping `internal=False`.

[ADR 005](../architecture/decisions/005-hardware-tiered-model-stack-and-first-run-policy.md) §A's catalog-discipline rule says only Apache-2.0 / MIT entries belong in the v1 bundle. The `license` field on each entry is typed `Literal["Apache-2.0", "MIT"]` so non-permissive licenses are rejected at construction time — adding a Mistral Research License or Gemma Terms-of-Use entry is a Pydantic validation error, not a runtime metadata flag. The 2026-05-05 cleanup (audit finding #6) deleted the four entries that had been carrying `license="non-permissive"` as placeholder metadata: `ministral-3-8b` + `devstral-small-2-24b` (Mistral Research License — research/eval only, paid commercial license required) and `gemma4-e4b` + `gemma4-26b-a4b` (Gemma Terms of Use — flow-down restrictions on derivative deployment). Coding workloads that previously routed to the `code` preset (devstral) now route through `balanced` / `best-local` against `qwen3-30b-a3b-instruct-2507` or `granite-4-h-small`.

## How It Works

### Hardware Probe

`probe_hardware()` detects the user's system specs (OS, RAM, disk, CPU cores, GPU vendor/VRAM, Apple Silicon) and classifies them into a tier: light (8–16 GB), balanced (16–32 GB), strong (32–48 GB), or workstation (48+ GB). Uses `psutil` for RAM with fallback to OS-native commands.

### Runtime Probe

`probe_runtime()` checks if Ollama is installed (via `shutil.which`) and running (via `GET /api/version` on the configured base URL). Returns installation status, version, and reachability.

### Runtime Load Probe

`probe_runtime_load()` snapshots the current local-runtime load — RAM/swap via `psutil`, GPU VRAM (best-effort: NVIDIA via `nvidia-smi`, Apple Silicon reports unified memory via the RAM signals only), and the list of models currently resident in Ollama via `GET /api/ps`. Consumed by the future memory-pressure auto-downgrade — when free RAM drops below the headroom needed for the loaded model + its KV cache, swap to a smaller model that fits rather than letting Ollama OOM or thrash swap.

Today's implementation is pure-Python; the macOS branch graduates to a Tauri-side native helper after [ADR 003](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md) lands — `vm_stat` parsing via psutil is brittle across macOS versions and the M5 + macOS 26 + Ollama segfault is a recent reminder that platform quirks bite. The Python version gives the right *shape* (consumers can wire to it) without locking in fragile platform code.

Exposed at `GET /api/local/runtime/load`. Response shape: `RuntimeLoad` with `total_ram_gb`, `available_ram_gb`, `used_ram_gb`, `ram_pct`, `swap_total_gb`, `swap_used_gb`, `swap_pct`, `gpu_vendor`, `gpu_vram_total_gb`, `gpu_vram_used_gb`, `loaded_models[]` (each `LoadedOllamaModel` with `name`, `size`, `size_vram`, `expires_at`), `ollama_reachable`, `timestamp_utc`. When Ollama is unreachable the system signals still populate; only `loaded_models` and `ollama_reachable` reflect the runtime gap.

### URL Safety Guardrails

`_normalize_and_validate_ollama_base_url()` sanitizes and validates user-provided Ollama URLs before any outbound HTTP request:
- only `http` / `https` schemes are accepted
- only loopback hosts are accepted (`localhost`, `127.0.0.1`, `::1`)
- userinfo (`user:pass@`) is rejected
- invalid or unsafe URLs fall back to the default `http://localhost:11434`

This protects local-model routes against partial SSRF patterns reported by static analysis.

### Model Catalog

The catalog covers the spectrum from weak laptops to workstations across eight presets (`fast`, `everyday`, `balanced`, `long-docs`, `reasoning`, `code`, `best-local`, `plumbing`). Context windows below are the model's *native* context — RoPE / YaRN-extended ranges are listed in `strengths`, not in `context_window`.

**User-pickable chat models** (3 — all with verified Ollama tags, all Apache-2.0):

| Preset | Model | Size | Native Context | Best RAM | Notes |
|--------|-------|------|---------------|----------|-------|
| Fast | qwen3:1.7b | 1.4 GB | 32K | 8–16 GB | 128K via YaRN |
| Everyday | qwen3:4b | 2.5 GB | 32K | 12–24 GB | 128K via YaRN |
| Balanced | qwen3:8b | 5.2 GB | 32K | 16–32 GB | 128K via YaRN |

**Internal entries** (8 — present in catalog, filtered from user picker until Ollama tag verified, all Apache-2.0):

| Preset | Model | Size | Native Context | Best RAM | Role / Status |
|--------|-------|------|---------------|----------|---------------|
| Long Docs | qwen3:4b-instruct-2507-q4_K_M | 2.6 GB | 256K | 12–24 GB | ADR 005 Tier A downgrade-ladder target — tag unverified |
| Balanced | qwen3:14b | 9.0 GB | 32K | 24–32 GB | best dense Qwen3 for 24 GB unified memory — tag unverified |
| Best Local | qwen3:30b-a3b-instruct-2507-q4_K_M | 18 GB | 256K | 24–48 GB | ADR 005 Tier B first-run primary — tag unverified |
| Reasoning | qwen3:30b-a3b-thinking-2507-q4_K_M | 18 GB | 256K | 24–48 GB | ADR 005 Tier B reasoning primary (duel-mode opt-in) — tag unverified |
| Best Local | gpt-oss:120b | 63 GB | 128K | 80–128 GB | ADR 005 Tier C first-run primary (single H100 / 96+ GB unified) — tag unverified |
| Plumbing | granite4:h-micro | 2.0 GB | 32K | 8–16 GB | always-on classifier (ISO-42001 certified) — tag unverified |
| Plumbing | granite4:h-tiny | 4.0 GB | 128K | 12–24 GB | ADR 005 Tier B opt-in plumbing upgrade — tag unverified |
| Plumbing | granite4:h-small | 18 GB | 128K | 32–48 GB | dispatcher top-tier (tool-capable) — tag unverified |

**Removed 2026-05-05** (audit finding #6, ADR 005 §A): `ministral-3:8b` and `devstral-small-2:24b` (Mistral Research License — research/eval only) and `gemma4:e4b` and `gemma4:26b-a4b` (Gemma Terms of Use — flow-down derivative restrictions). The narrowed `license: Literal["Apache-2.0", "MIT"]` type on `ModelCatalogEntry` rejects re-introduction at construction time.

Each internal entry carries a `TODO: verify Ollama tag` comment in [`ollama_service.py`](../../backend/services/ollama_service.py). Promotion to user-pickable requires verifying the tag against `https://ollama.com/library/<name>`, then flipping `internal=False`.

Internal entries are present in `MODEL_CATALOG` but excluded from `build_catalog()` unless the caller passes `include_internal=True`.

### KV-aware footprint accounting

Two catalog fields feed the future memory-pressure auto-downgrade — when free RAM drops below the headroom needed for the loaded model + its KV cache, swap to a smaller model that fits rather than letting Ollama OOM or thrash swap.

- `bytes_per_kv_token: int` — approximate per-token KV cache cost. Granite 4 hybrid-mamba models report ~256–1024 bytes/token (state-space cache is fixed-size); Gemma 4 SWA models ~1024–1536 bytes/token (only the sliding window is stored); transformer Qwen3 / Devstral land at 2048–5120 bytes/token. The ratios across architectures (mamba << swa < transformer) are the load-bearing signal; absolute numbers are refined as measurement data lands.
- `attention_arch: Literal["transformer", "mamba", "swa"]` — Literal-typed so a typo in a catalog entry fails at construction.

`effective_footprint_bytes(entry, ctx_len_now) → int` computes `weights + bytes_per_kv_token × ctx_len_now`. This is the predicate the auto-downgrade will use to ask "does this still fit in available RAM?" — a long Jira-ingest can balloon the chat-model footprint mid-session and a weights-only check would underestimate the swap risk.

### Hardware tiers + first-run policy + downgrade ladder (ADR 005)

[ADR 005](../architecture/decisions/005-hardware-tiered-model-stack-and-first-run-policy.md) layers a tier model onto the catalog. Each entry has up to three new tier-related fields:

- `license: Literal["Apache-2.0", "MIT"]` — catalog discipline gate per ADR §A. Non-permissive families (Gemma TOU, Mistral Research, Llama community, etc.) are rejected at construction time. The 2026-05-05 cleanup narrowed this from a three-value `Literal` (which had carried `"non-permissive"` as transitional metadata) to the permissive-only pair.
- `first_run_default_tiers: List[str]` — the tiers (subset of `{"A", "B", "C"}`) where this entry is the first-run primary the orchestrator pulls in the foreground on first launch.
- `ladder_positions: Dict[str, int]` — per-tier downgrade-ladder slot. Smaller integer = closer to the floor (refuse-with-error, position 0). An entry not present in `ladder_positions` for a tier is not on that tier's ladder.

Three module-level helpers consume these fields:

- `tier_for_hardware(HardwareProfile) → Literal["A", "B", "C"]` — maps the `probe_hardware()` result to an ADR 005 §A tier. Boundary rules: ≥96 GB RAM or ≥80 GB VRAM (H100/A100/MI300X-class) → C; ≥24 GB VRAM (RTX 4090+) or ≥48 GB unified Apple Silicon → B; everything else → A. Conservative on the 32 GB Apple Silicon boundary — stays in A even though raw RAM crosses 32 GB, because the 20 GB Tier B primary OOMs once the OS + browser take their share. Promotion to B requires evidence the larger primary fits, which is the chat-model probe's job (ADR 012), not first-run defaults'.
- `first_run_default_for(tier) → Optional[ModelCatalogEntry]` — Tier A → `qwen3-8b`, Tier B → `qwen3-30b-a3b-instruct-2507`, Tier C → `gpt-oss-120b`.
- `downgrade_ladder_for(tier, *, include_opt_in=True) → List[ModelCatalogEntry]` — top → floor by ladder_positions descending. Tier A: opt-in 30B-A3B → 8B → 4B-Instruct-2507 (the 256K-native variant per ADR §A "downgrade ladder target"). Tier B/C: opt-in/primary gpt-oss-120b → 30B-A3B → 8B → 4B. `include_opt_in=False` filters out ceiling positions that require explicit user opt-in (gpt-oss-120b on Tier B is opt-in; on Tier C it's the primary).

These helpers are the foundation for the first-run orchestrator (described below) and the upcoming runtime memory-pressure auto-downgrade (G4b4). Boundary tests for tier mapping + ladder ordering live in [`tests/test_local_models.py`](../../backend/tests/test_local_models.py)::`TestTierForHardware`, `TestFirstRunDefault`, `TestDowngradeLadder`.

### First-run pull orchestrator (ADR 005 §B)

[`services/first_run_orchestrator.py`](../../backend/services/first_run_orchestrator.py) owns the once-per-install state machine that puts the right chat model on disk for the user's hardware. Pipeline:

```
idle → probing → pulling_primary → (marker write) → pulling_fallback → running_probe → complete
                                                                                    ↘ skipped
                                                                                    ↘ failed
```

Mirrors the singleton-supervisor shape of [`services/reindex_supervisor.py`](../../backend/services/reindex_supervisor.py) (ADR 003 §I): module-level `FirstRunStatus` dataclass + `asyncio.Lock` for re-entrancy + single `asyncio.Task` lifetime + lifespan-cancel hook on FastAPI shutdown. Two divergences:

- **Marker is written mid-pipeline.** As soon as the foreground primary pull lands, `<workspace>/app/.first_run_complete` is written. The user can chat *now*; the fallback pull and chat-model-probe continue in the background. Subsequent launches see the marker via `is_first_run_complete()` and skip the entire pipeline. Per ADR §B step 6.
- **Fallback / probe failures are non-fatal.** If the primary lands but the fallback errors out, chat still works — the only consequence is that the runtime auto-downgrade has nothing on disk to fall back to (G4b4 will lazy-pull on first OOM in that case). Status records `fallback_failed=True` / `probe_failed=True`; state still advances to `complete`.

Skip path (`POST /api/local/first-run/start { "skip": true }`) writes no marker, sets `state="skipped"`, returns immediately. Per ADR §B "Skip / opt-out" — next launch re-prompts.

To consume Ollama's pull stream internally without parsing SSE wrapping, the catalog file exposes `pull_model_events()` (raw-dict async generator); `pull_model_stream()` is a thin SSE wrapper over it. Single source of truth for the pull loop, two consumers (the SSE endpoint and the orchestrator).

**Endpoints**:

| Endpoint | Purpose |
|----------|---------|
| `POST /api/local/first-run/start` | Idempotent kickoff. Returns `started` / `already_running` / `already_complete` / `skipped`. Body: `{"skip": false, "base_url": "http://127.0.0.1:11434"}` (both optional). |
| `GET /api/local/first-run/status` | Snapshot of state + per-pull progress (mirrors the shape of `/api/memory/reindex/status`). Frontend polls 1× per second while the modal is open. Includes `marker_present` so the frontend can decide whether to mount the modal at all. |

**Fallback selection**: `downgrade_ladder_for(tier, include_opt_in=False)` returns the tier ladder (top → floor) excluding opt-in ceilings. The orchestrator picks the highest-position entry below the primary:

| Tier | Primary | Fallback (background pull) |
|------|---------|----------------------------|
| A | `qwen3:8b` | `qwen3:4b-instruct-2507-q4_K_M` (256K-native per ADR §A) |
| B | `qwen3:30b-a3b-instruct-2507-q4_K_M` | `qwen3:8b` |
| C | `gpt-oss:120b` | `qwen3:30b-a3b-instruct-2507-q4_K_M` |

State-machine + endpoint contract tests live in [`tests/test_first_run_orchestrator.py`](../../backend/tests/test_first_run_orchestrator.py): happy-path Tier A + Tier C, primary-pull-fatal, fallback-non-fatal, probe-non-fatal, skip path, marker-already-present short-circuit, concurrent-start idempotency, lifespan cancel.

### First-run wizard frontend (ADR 005 §B Layer 1)

The frontend half of the first-run pipeline lives in two pieces:

- [`useFirstRun.ts`](../../frontend/app/composables/useFirstRun.ts) — composable that polls `GET /api/local/first-run/status` at 1 Hz while the orchestrator is mid-pipeline. Mirrors the singleton-supervisor shape of [`useReindexStatus`](../../frontend/app/composables/useReindexStatus.ts) (refcount + Nuxt `useState` + lifecycle hooks). Exposes the `FirstRunStatus` snapshot plus four computed helpers:
  - `chatReady` — true once the foreground primary lands (i.e. `marker_written` flips, or state advances past `pulling_primary`). The wizard uses this to release the user into chat while the background fallback pull and chat-model probe continue silently. Per ADR §B step 5.
  - `active` — true while the orchestrator is mid-pipeline (`probing` / `pulling_primary` / `pulling_fallback` / `running_probe`).
  - `finished` — true on `complete` or `skipped`.
  - `stageLabel` — single-line UI label per state ("Detecting your hardware…", "Downloading qwen3:8b", "Topping up qwen3:4b-instruct-2507-q4_K_M in the background", "Validating your setup…", etc).
- [`OnboardingLocalFlow.vue`](../../frontend/app/components/OnboardingLocalFlow.vue) — two-layer wizard. **Layer 1** (orchestrator-driven, default in the bundled build) drives the §B pipeline UI: probing label → `pulling_primary` progress bar with bytes / pct + "I'll pick my own model later" skip → `chatReady` ready screen with non-blocking background-fallback indicator + Open Jarvis button → `complete` ready screen. **Layer 2** (legacy [`useLocalSetupFlow`](../../frontend/app/composables/useLocalSetupFlow.ts) manual picker) is kept verbatim for the §B skip path AND for the dev-mode fallback when `localModels.isOllamaReady()` is false at mount (no bundled sidecar). Step indicators redesigned to "Detect hardware → Download model → Start using Jarvis" (replaces the legacy "Install runtime → Choose model" which assumed the user installs Ollama themselves).

**Marker-present early return.** If `firstRun.status.marker_present` is true at mount the wizard emits `model-ready` immediately and bails — second/third launches with the marker on disk skip the wizard entirely (per §B step 6 "Subsequent launches skip the entire pipeline").

**Auto-kickoff.** On mount, when Ollama is reachable AND no marker is present AND state is `idle`, the wizard auto-calls `firstRun.start()` so the user doesn't have to click anything to begin. The pipeline proceeds without intervention; the user only interacts to skip or to continue past the ready screen.

**Skip path.** "I'll pick my own model later" calls `firstRun.start({ skip: true })` (writes no marker per §B Skip / opt-out), refreshes the catalog, and flips the wizard mode to `manual` with `flow.state = 'model_selection'` — the user lands in the legacy model picker. No marker is written; next launch re-prompts.

### Memory-pressure monitor + downgrade ladder runtime (ADR 005 §C)

[`services/memory_pressure_monitor.py`](../../backend/services/memory_pressure_monitor.py) supplies the runtime predicate and the ladder-walk that prevent OOM crashes when free RAM drops below the active model's effective footprint. Two synchronous helpers and one ladder picker:

- `current_free_ram_bytes()` — psutil wrapper. Returns 0 if psutil is unavailable so callers conservatively fall back to the floor-refusal path rather than dispatching against an unmeasured machine.
- `check_can_run(entry, ctx_len_tokens, *, free_ram_bytes=None, headroom_fraction=0.80)` — pass condition: `effective_footprint_bytes(entry, ctx_len_tokens) ≤ free × 0.80`. The 20% buffer absorbs OS overhead, browser tabs, and in-turn KV-cache growth as the model decodes.
- `pick_runnable_model(requested, *, tier, ctx_len_tokens, installed_ollama_tags, free_ram_bytes=None, headroom_fraction=0.80)` — walks `downgrade_ladder_for(tier)` from `requested` toward the floor and returns a `MemoryPressureSwap` with `chosen` (None on floor refusal), `did_swap`, `reason` (human-readable swap explanation), and a per-step `trail` of `(model_id, runnable | over_footprint | not_installed)` for telemetry. The picker never walks *up* the ladder under pressure, even if a higher rung would fit.

**Design note.** ADR §149 originally described this module as a watcher that emits `pressure` events on threshold crossings. That shape was rejected in the implementation in favour of a synchronous predicate — both §C triggers (OOM-during-inference catch + pre-flight) want a fast "does this still fit *right now*?" answer, not an event firehose. The module-level docstring carries the full reasoning so a future reader sees why the watcher pattern wasn't built.

Two more helpers ride along:

- `looks_like_oom(error_message)` — string match against seven OOM phrases (`out of memory`, `oom`, `memory exhausted`, `cannot allocate`, `metal: failed to allocate`, `cuda out of memory`, `ggml_metal_graph_compute`). Ollama returns OOM as plain-text strings via `/api/chat`'s error stream; structured codes don't exist, so matching the message is the only handle.
- `find_entry_by_ollama_model(model)` — reverse-lookup keyed on the raw Ollama tag (e.g. `qwen3:8b`). For safety against stale persisted state from older builds it still strips a leading `ollama_chat/` if present.

**Chat router integration** ([`routers/chat.py`](../../backend/routers/chat.py)). Two private helpers (`_apply_memory_pressure_swap`, `_ladder_step_after_oom`) integrate at two points in `_handle_message`:

1. **Pre-flight swap** (§C trigger 2) — runs before `_make_llm`. Looks at `prompt_stats.context_tokens`, walks the ladder, and either:
   - Returns the same model (no-op),
   - Emits a `warning` WS event with the swap reason and returns the smaller model,
   - Or signals floor refusal — the router emits a fatal `error` + `done` and returns *before* the LLMService is constructed, guaranteeing the model never gets the chance to OOM.
2. **OOM-retry loop** (§C trigger 1) — wraps the initial stream loop. If Ollama errors with an OOM signature *before any text streams* (`text_started` flag), the router walks one further ladder step, recreates the LLMService, and retries the same turn once. Text-already-streamed OOMs flow through as plain `error` events to avoid double-emission. The retry is bounded to one attempt; a second consecutive OOM surfaces a user-facing "Out of memory and no smaller installed model fits" error.

**Coverage.** 18 unit tests in [`tests/test_memory_pressure_monitor.py`](../../backend/tests/test_memory_pressure_monitor.py) (predicate math, ladder picker happy path + pressure swap + uninstalled-step walkthrough + floor refusal + never-upgrade invariant + Tier-B ladder ordering, OOM string matcher, lookup helper, free-RAM fallback) + 7 router-integration tests in [`tests/test_chat_memory_pressure.py`](../../backend/tests/test_chat_memory_pressure.py) (pre-flight no-swap-for-cloud, pre-flight swap emits warning, pre-flight floor refusal short-circuits before LLM construction, OOM pre-text triggers ladder retry with replayed stream, OOM with no fallback emits user-facing error, OOM-after-text does NOT retry, non-OOM errors skip the retry path entirely).

### Lightweight mode (ADR 005 §C trigger 3)

The user-facing pin for "just work, don't auto-downgrade." When on, the chat router pre-flight skips the pressure walk entirely and dispatches to the smallest installed entry on the user's tier ladder. Useful when other RAM-heavy apps are running and the user prefers a smaller model running stably to a larger one auto-downgrading mid-turn.

**Backend.** New helper `floor_entry_for_tier(tier, *, installed_ollama_tags)` in [`memory_pressure_monitor.py`](../../backend/services/memory_pressure_monitor.py) walks the tier ladder top → floor and returns the *last* installed entry (the smallest rung on disk). Endpoints in [`routers/settings.py`](../../backend/routers/settings.py): `GET /api/settings/lightweight-mode` and `PATCH /api/settings/lightweight-mode` with body `{enabled: bool}`. Persistence rides the existing `preference_service` (workspace `app/preferences.json` as `"lightweight_mode": "true"|"false"`).

[`_apply_memory_pressure_swap`](../../backend/routers/chat.py) gains a short-circuit branch:

- When `_is_lightweight_mode_on()` returns True AND `floor_entry_for_tier()` returns a different entry from the requested one → emit a `Lightweight mode — using {floor.label}` warning and return the floor immediately, *before* the pressure-aware `pick_runnable_model` walk runs.
- When the requested model is already the floor (or no installed-on-ladder entry exists) → fall through to the normal pre-flight, which is a no-op when not under pressure.

The "Lightweight mode" copy on the warning differentiates the explicit-user-pin path from the generic auto-downgrade warning (which uses "doesn't fit … switched to …" phrasing). Frontend can route the two warning shapes to different UI affordances if it needs to.

**Frontend.** New composable [`useLightweightMode.ts`](../../frontend/app/composables/settings/useLightweightMode.ts) (mirrors `usePrivacySettings`'s shape — `load` + `set`, `error` + `saving` refs, server-side persistence so the setting survives launches without per-tab localStorage drift) and new component [`PerformanceSection.vue`](../../frontend/app/components/settings/PerformanceSection.vue) added to the Settings page between LocalModels and Workspace. Collapsible section, status badge ("🪶 Lightweight mode active" when on, "Auto" when off), toggle copy explains the trade-off ("Pin chat to the smallest installed model on your hardware tier … Auto-downgrade is bypassed").

**Coverage.** 4 unit tests for `floor_entry_for_tier` in [`tests/test_memory_pressure_monitor.py::TestFloorEntryForTier`](../../backend/tests/test_memory_pressure_monitor.py) (Tier A happy path, skips-uninstalled-floor falls back to next-smallest, None when nothing installed, None when only off-ladder models installed) + 5 endpoint tests in [`tests/test_settings_api.py`](../../backend/tests/test_settings_api.py) (default off, toggle-on persists across re-read, toggle-off, rejects non-bool, rejects missing field) + 2 chat-router integration tests in [`tests/test_chat_memory_pressure.py`](../../backend/tests/test_chat_memory_pressure.py) (`test_lightweight_mode_short_circuits_to_floor`, `test_lightweight_mode_no_warning_when_already_at_floor`).

### keep_alive

Ollama keeps a model resident for `DEFAULT_KEEP_ALIVE = "24h"` after last use. Both [`services/ollama_dispatcher.py`](../../backend/services/ollama_dispatcher.py) (the chat path) and [`services/ollama_service.py::warm_up_model`](../../backend/services/ollama_service.py) (called when a chat model is freshly selected) read this value, so chat traffic and freshly-selected models share the same retention.

The 24 h floor exists because Ollama's own 5 min default — and our previous 30 min default — both produced a ~17 s cold-reload (mmap of the GGUF weights + KV-cache rebuild) on the next message after any normal idle gap (coffee break, meeting, lunch). For a workstation that already chose to install a chat model, keeping it resident across a day of intermittent use is the right trade-off; bounded eviction at 24 h still prevents indefinite RAM growth on multi-day-uptime sessions.

Background ETL paths ([`services/ingest.py`](../../backend/services/ingest.py), [`services/document_classifier.py`](../../backend/services/document_classifier.py)) keep their own `keep_alive="30m"` literal — those are one-shot operations that don't benefit from long retention. Memory-pressure auto-downgrade (`probe_runtime_load()` + `effective_footprint_bytes()`) still evicts explicitly when it needs to swap models; the keep-alive is a *floor*, not a contract.

### Latency baseline

Per-model TTFT, decode throughput, and end-to-end wall clock are measured by the sibling latency harness at [`backend/tests/eval/latency/`](../../backend/tests/eval/latency/). Captures baselines per `(machine, knob_stack)` against the canonical loadouts on the actual ICP hardware (single-machine first: Apple M5 Pro 24 GB), with one Anthropic Claude Sonnet reference scenario as the explicit competitor benchmark. Bootstrap-CI gate on metric diffs determines whether an optimization knob (flash attention, KV-cache q8, prefix caching, speculative decoding) earned its complexity. See [`docs/concepts/latency-baseline.md`](../concepts/latency-baseline.md) and [ADR 011](../architecture/decisions/011-latency-benchmark-harness.md). CLI: `python -m tests.eval.latency.run_bench`.

### Canonical chat-model selection (the per-machine problem)

The "v1 canonical chat model" used to be hard-coded — initially Qwen3-30B-A3B per [ADR 010](../architecture/decisions/010-conversation-replay-eval-harness.md), now Qwen3-14B per ADR 010 §"Issue 4" — but the right shape for a self-installing desktop product is per-machine selection. Different users have different Ollama versions, OS major versions, hardware tiers, and memory budgets, and behavior like `think: false` honoring is environment-specific (the 30B-A3B leaks chain-of-thought on Ollama 0.18.0 + macOS 26 + M5 Pro, and works correctly on other combinations).

[ADR 012](../architecture/decisions/012-chat-model-self-test.md) files the architectural answer: at install or on demand, run a three-probe self-test against each candidate model in the user-pickable catalog —

1. **Correctness probe** — does the model honor `think: false` and produce a clean answer to "say hi in one word"?
2. **Hardware-fit probe** — does `effective_footprint_bytes(entry, default_ctx)` fit comfortably under available RAM?
3. **Speed probe** — does the model achieve usable TPS on warm-short and chat-realistic-shallow?

Pick the largest passing model. Persist the choice. Re-run on Ollama / OS / hardware change. Implementation lives at [`backend/services/chat_model_probe.py`](../../backend/services/chat_model_probe.py); reuses the same scenario definitions and Ollama HTTP client as the latency benchmark, so the runtime probe and the dev benchmark stay in lockstep.

### Recommendation Engine

`score_model()` computes a compatibility score (0–100) for each model based on:
- **Disk check**: hard block if not enough space
- **RAM check**: different thresholds for CPU-only vs GPU/Apple Silicon
- **Bonuses**: CPU-friendly models on CPU-only machines, use-case matching

`build_catalog()` scores all models, sorts by score, and marks top 3 as recommended.

### Tool Calling Mode

Each model gets a `tool_mode` classification consumed by the [`OllamaDispatcher`](../../backend/services/ollama_dispatcher.py):
- **`native_qwen3`** — model exposes native function-calling in the Qwen3 family format (e.g. qwen3:8b, qwen3:14b, qwen3:30b-a3b-instruct-2507-q4_K_M, qwen3:30b-a3b-thinking-2507-q4_K_M, gpt-oss:120b, granite4:h-small). The dispatcher passes the tool spec straight to `ollama.AsyncClient.chat(tools=…)` and consumes `tool_calls` chunks from the stream.
- **`adapted`** — small/mid-size models (e.g. qwen3:4b, granite4:h-micro, granite4:h-tiny) that handle tool calls via JSON-mode prompting; the dispatcher wraps the tool spec into the system prompt and parses the model's JSON output back into a synthetic `tool_use` block.
- **`excluded_from_tools`** — very small models (< 2 GB) that don't reliably tool-call (e.g. qwen3:1.7b). The dispatcher refuses to attach tools at all.

`_tool_mode_for()` derives tool_mode from the catalog entry's `native_tools` flag and `download_size_gb`. Ollama's wire format does not carry tool-call ids (only `function.name` + `function.arguments`); the dispatcher synthesizes a stable id per tool call at emit time so downstream `tool_use` ↔ `tool_result` correlation stays intact.

Old taxonomy → new taxonomy mapping (for any external integrations that still consume the previous values):

| Old | New |
|-----|-----|
| `native` | `native_qwen3` |
| `json_fallback` | `adapted` |
| `limited` | `excluded_from_tools` |

### Runtime Health Monitoring

`useLocalModels.ts` provides `startHealthPolling()` / `stopHealthPolling()` that periodically probe the Ollama runtime. The main chat page starts polling every 30s on mount (per ADR 015 there is only one provider, so polling is unconditional). If Ollama becomes unreachable, `ollamaDown` state triggers a banner in ChatPanel with a single Reconnect action.

### Slow Response Indicator

`useChat.ts` starts a timer when sending a message. After 10s with no text_delta, it shows "Local model is loading...". After 30s, it shows an extended message suggesting a smaller model. Cleared immediately when the first text arrives or the response completes.

### Model Pull

`pull_model_stream()` proxies Ollama's `POST /api/pull` with `stream: true` and converts the progress events into SSE format for the frontend.

### Chat Integration

[`_make_llm()`](../../backend/routers/chat.py) constructs an [`OllamaDispatcher`](../../backend/services/ollama_dispatcher.py) — there is no `provider` branch because Ollama is the sole target. The dispatcher uses the official `ollama==0.6.2` Python package (Apache-2.0; the only net-new transitive dep over what FastAPI/httpx already pull in). Streaming, tool-call decoding, and connection lifecycle are upstream-tested code that tracks the Ollama server we're already pinned to.

The dispatcher's responsibilities:
- adapter from `ollama.AsyncClient.chat(stream=True)` events → the existing `StreamEvent` shape ([`backend/services/system_prompt.py`](../../backend/services/system_prompt.py)) so the chat router and WS event consumers see no behavioural change vs. the pre-ADR-015 path;
- Anthropic-style ↔ Ollama-style message converter (the chat router passes Anthropic-shaped tool-result blocks back as `{role: "tool", tool_name, content}` messages);
- tool-call id synthesis (Ollama's wire format has no ids — see Tool Calling Mode above);
- error mapping: `ollama.RequestError`, `ollama.ResponseError`, `httpx.TimeoutException`, `httpx.ConnectError` are all collapsed to `StreamEvent(type="error", content=…)` at the adapter boundary, which feeds the OOM-retry loop in `_handle_message`.

Token counting uses `tiktoken` for prompt-budget predictions; the official client's `prompt_eval_count` / `eval_count` fields on the final chunk are the authoritative post-hoc counts.

## Key Files

| File | Purpose |
|------|---------|
| [ollama_service.py](../../backend/services/ollama_service.py) | All Ollama logic: hardware/runtime probes, catalog, scoring, pull, config |
| [ollama_dispatcher.py](../../backend/services/ollama_dispatcher.py) | Streaming adapter: `ollama.AsyncClient` events → `StreamEvent` shape; tool-call id synthesis; error mapping |
| [system_prompt.py](../../backend/services/system_prompt.py) | `StreamEvent` dataclass + `build_system_prompt` rescued from the deleted `services/claude.py` |
| [local_models.py](../../backend/routers/local_models.py) | REST API endpoints for local model management |
| [chat.py](../../backend/routers/chat.py) | `_make_llm()` constructs the dispatcher (single Ollama path); `base_url` passed through WS messages |
| [test_local_models.py](../../backend/tests/test_local_models.py) | 70 tests covering scoring, probes, config, routing |
| [test_ollama_dispatcher.py](../../backend/tests/test_ollama_dispatcher.py) | Adapter unit tests against a mocked `ollama.AsyncClient` |
| [useLocalModels.ts](../../frontend/app/composables/useLocalModels.ts) | Frontend composable: hardware/runtime/catalog state, pull SSE |
| [useChatModel.ts](../../frontend/app/composables/useChatModel.ts) | Active-model selection (replaces the deleted `useApiKeys`); persists to localStorage |
| [OllamaStatus.vue](../../frontend/app/components/OllamaStatus.vue) | Runtime status card (not installed / not running / running) with hardware info |
| [LocalModelCard.vue](../../frontend/app/components/LocalModelCard.vue) | Model recommendation card with compatibility badge, tool mode badge, download/use actions |
| [ModelSelector.vue](../../frontend/app/components/ModelSelector.vue) | Header model picker — local-only loop, no provider switching |
| [PullProgress.vue](../../frontend/app/components/PullProgress.vue) | Download progress bar with percentage and byte counts |
| [OnboardingLocalFlow.vue](../../frontend/app/components/OnboardingLocalFlow.vue) | Local model setup wizard for onboarding (hardware detect, Ollama check, recommend, download) |
| [test_local_models_integration.py](../../backend/tests/test_local_models_integration.py) | 22 tests: tool_mode, timeouts, warm-up, test endpoint, catalog |

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/local/hardware` | GET | Hardware profile (RAM, disk, CPU, GPU, tier) |
| `/api/local/runtime` | GET | Ollama status (installed, running, version) |
| `/api/local/runtime/load` | GET | Runtime load snapshot (RAM/swap/GPU + Ollama-loaded models) — feeds the future memory-pressure auto-downgrade |
| `/api/local/models/catalog` | GET | Model catalog with recommendations |
| `/api/local/models/installed` | GET | Models downloaded in Ollama |
| `/api/local/models/pull` | POST | Download model (SSE progress stream) |
| `/api/local/models/select` | POST | Set active local model |
| `/api/local/models/{name}` | DELETE | Remove model from Ollama |
| `/api/local/models/test` | POST | Quick model validation (latency, tok/s) |
| `/api/local/models/warm-up` | POST | Keep model loaded in memory |

## Gotchas

- Bundled Ollama runtime under `desktop/src-tauri/binaries/ollama-runtime/` is launched by the Tauri shell at app start (per [ADR 003](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md) §G); for backend-only dev the user must run Ollama themselves.
- Local models on CPU can be very slow (2–10 tok/s); the dispatcher's per-call timeout (1800s) is intentional.
- Not all models support native function calling; `tool_mode` field in catalog classifies support level.
- `done` event includes `tool_mode` so the frontend can display tool-support info.
- `psutil` is required for hardware probe; falls back to OS commands if missing.
- Tool-call ids are synthesized client-side — Ollama's wire format omits them. The dispatcher emits a UUID-derived stable id per call so `tool_use` ↔ `tool_result` round-trips intact.
- Health polling runs unconditionally every 30s while the chat page is mounted (no provider gate per ADR 015).
- `base_url` from query/body is normalized and restricted to local loopback endpoints for safety.
