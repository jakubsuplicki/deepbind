---
title: Local Models
status: active
type: feature
sources:
	- backend/services/ollama_service.py
	- backend/routers/local_models.py
	- backend/routers/chat.py
	- backend/tests/test_local_models.py
	- backend/tests/test_local_models_integration.py
	- frontend/app/composables/useLocalModels.ts
	- frontend/app/composables/useLocalSetupFlow.ts
	- frontend/app/composables/useChat.ts
	- frontend/app/components/OllamaStatus.vue
	- frontend/app/components/LocalModelCard.vue
	- frontend/app/components/PullProgress.vue
	- frontend/app/components/OnboardingLocalFlow.vue
	- frontend/app/components/ChatPanel.vue
	- frontend/app/components/StatusBar.vue
	- frontend/app/pages/main.vue
	- frontend/app/pages/settings.vue
	- frontend/app/types/index.ts
	- frontend/tests/composables/useLocalModelsIntegration.test.ts
depends_on: [chat, api-key-management, workspace-onboarding, inference-router, profiles]
last_reviewed: 2026-04-28
last_updated: 2026-04-28
---

# Local Models

Run Jarvis with on-device AI via Ollama — no API key required.

## Summary

The local models feature adds support for running Jarvis with locally-hosted LLMs via Ollama. It provides hardware detection, a curated catalog of model presets with hardware-based recommendations, model download with streaming progress, seamless integration with the existing multi-provider chat pipeline via LiteLLM, tool calling mode detection per model, runtime health monitoring with reconnection flow, and slow response indicators for local inference.

The catalog is split into two layers: **user-pickable chat models** (6 entries with verified Ollama tags) and **internal entries** (7 entries — Qwen3-2507 split fine-tunes, Qwen3-14B, Gemma 4 26B-A4B, Granite 4.0 H-Micro/H-Tiny/H-Small — all carry `internal=True` because their Ollama registry tags are unverified). The user-facing endpoint filters internal entries out so a stale-tag pull never 404s the customer; the future InferenceRouter ([ADR 004](../architecture/decisions/004-inference-router-architecture.md)) consumes them via `build_catalog(include_internal=True)`. Each internal entry carries a `TODO: verify Ollama tag` comment marking the verification gap; promotion to user-pickable requires `ollama pull <tag>` against the live registry, then flipping `internal=False`.

## How It Works

### Hardware Probe

`probe_hardware()` detects the user's system specs (OS, RAM, disk, CPU cores, GPU vendor/VRAM, Apple Silicon) and classifies them into a tier: light (8–16 GB), balanced (16–32 GB), strong (32–48 GB), or workstation (48+ GB). Uses `psutil` for RAM with fallback to OS-native commands.

### Runtime Probe

`probe_runtime()` checks if Ollama is installed (via `shutil.which`) and running (via `GET /api/version` on the configured base URL). Returns installation status, version, and reachability.

### Runtime Load Probe

`probe_runtime_load()` snapshots the current local-runtime load — RAM/swap via `psutil`, GPU VRAM (best-effort: NVIDIA via `nvidia-smi`, Apple Silicon reports unified memory via the RAM signals only), and the list of models currently resident in Ollama via `GET /api/ps`. The future InferenceRouter ([ADR 004](../architecture/decisions/004-inference-router-architecture.md)) consumes this snapshot to decide whether a new request can fit in the current footprint or requires unloading another slot first.

Today's implementation is a pure-Python scaffold per ADR 004 §"Buildable today". Per the same ADR's §"Blocked by upstream ADRs", the macOS branch graduates to a Tauri-side native helper after [ADR 003](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md) lands — `vm_stat` parsing via psutil is brittle across macOS versions and the M5 + macOS 26 + Ollama segfault is a recent reminder that platform quirks bite. The Python scaffold gives the right *shape* (consumers can wire to it) without locking in fragile platform code.

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

**User-pickable chat models** (6 — all with verified Ollama tags):

| Preset | Model | Size | Native Context | Best RAM | Notes |
|--------|-------|------|---------------|----------|-------|
| Fast | qwen3:1.7b | 1.4 GB | 32K | 8–16 GB | 128K via YaRN |
| Everyday | qwen3:4b | 2.5 GB | 32K | 12–24 GB | 128K via YaRN |
| Balanced | qwen3:8b | 5.2 GB | 32K | 16–32 GB | 128K via YaRN |
| Long Docs | ministral-3:8b | 6.0 GB | 256K | 16–32 GB | |
| Reasoning | gemma4:e4b | 9.6 GB | 128K | 24–40 GB | |
| Code | devstral-small-2:24b | 15 GB | 256K | 32–64 GB | 384K via RoPE extension |

**Internal entries** (7 — present in catalog, filtered from user picker until Ollama tag verified):

| Preset | Model | Size | Native Context | Best RAM | Role / Status |
|--------|-------|------|---------------|----------|---------------|
| Best Local | gemma4:26b-a4b | 15 GB | 256K | 24–48 GB | 26B-A4B MoE (renamed from incorrect "Gemma 4 27B") — tag unverified |
| Long Docs | qwen3:4b-instruct-2507 | 2.6 GB | 256K | 12–24 GB | 256K *native* sibling of qwen3:4b — tag unverified |
| Balanced | qwen3:14b | 9.0 GB | 32K | 24–32 GB | best dense Qwen3 for 24 GB unified memory — tag unverified |
| Best Local | qwen3:30b-a3b-instruct-2507 | 18 GB | 256K | 24–48 GB | ADR 008 v1 chat-pinned slot — tag unverified |
| Plumbing | granite4:h-micro | 2.0 GB | 32K | 8–16 GB | always-on classifier (ISO-42001 certified) — tag unverified |
| Plumbing | granite4:h-tiny | 4.0 GB | 128K | 12–24 GB | dispatcher mid-tier — tag unverified |
| Plumbing | granite4:h-small | 18 GB | 128K | 32–48 GB | dispatcher top-tier (tool-capable) — tag unverified |

Each internal entry carries a `TODO: verify Ollama tag` comment in [`ollama_service.py`](../../backend/services/ollama_service.py). Promotion to user-pickable requires verifying the tag against `https://ollama.com/library/<name>`, then flipping `internal=False`.

Internal entries are present in `MODEL_CATALOG` but excluded from `build_catalog()` unless the caller passes `include_internal=True`. The [InferenceRouter](inference-router.md) ([ADR 004](../architecture/decisions/004-inference-router-architecture.md)) is the consumer.

### KV-aware footprint accounting

Per ADR 004 §"KV-aware footprint accounting", every catalog entry carries:

- `bytes_per_kv_token: int` — approximate per-token KV cache cost, derived from architectural intuition for ADR 004 §"KV-aware footprint accounting". Granite 4 hybrid-mamba models report ~256–1024 bytes/token (state-space cache is fixed-size); Gemma 4 SWA models ~1024–1536 bytes/token (only the sliding window is stored); transformer Qwen3 / Devstral land at 2048–5120 bytes/token. The ratios across architectures (mamba << swa < transformer) are the load-bearing signal; absolute numbers are refined as measurement data lands.
- `attention_arch: Literal["transformer", "mamba", "swa"]` — Literal-typed so a typo in a catalog entry fails at construction. Drives the architectural footprint difference.
- `slot_class: str` — which stack slot the model fills (see [ProfilePack](profiles.md)). The router's `dispatch()` reads this to fill the audit decision; the keep_alive policy table reads it to decide eviction cadence.

`effective_footprint_bytes(entry, ctx_len_now) → int` computes `weights + bytes_per_kv_token × ctx_len_now` and is the predicate the router will use for `can_load(model)` checks once the production-grade memory-pressure signals land (ADR 003 gate).

### Per-slot keep_alive policy

`KEEP_ALIVE_BY_SLOT` (in [`ollama_service.py`](../../backend/services/ollama_service.py)) maps slot class to Ollama `keep_alive` semantics per ADR 004 §"`keep_alive` policy table":

| Slot class | keep_alive | Rationale |
|---|---|---|
| `embedding` / `plumbing` | `-1` (forever) | Always-resident; latency-critical, cheap to hold |
| `conversational` / `reasoning` / `long_context` / `best_local` | `30m` | Behavior preservation — today's hard-coded value |
| `code` / `vision` | `5m` | On-demand; evict aggressively to free unified memory |

`warm_up_model()` consumes this policy: when called without an explicit `slot_class`, it looks up the catalog entry by `ollama_model` and uses the entry's `slot_class`. Falls back to `DEFAULT_KEEP_ALIVE = "30m"` for non-catalog models.

### Recommendation Engine

`score_model()` computes a compatibility score (0–100) for each model based on:
- **Disk check**: hard block if not enough space
- **RAM check**: different thresholds for CPU-only vs GPU/Apple Silicon
- **Bonuses**: CPU-friendly models on CPU-only machines, use-case matching

`build_catalog()` scores all models, sorts by score, and marks top 3 as recommended.

### Tool Calling Mode

Each model gets a `tool_mode` classification (renamed 2026-04-28 per ADR 004's "Tool-format hardening"):
- **`native_qwen3`** — model exposes native function-calling in the Qwen3 family format (e.g. qwen3:8b, qwen3:14b, qwen3:30b-a3b-instruct-2507, gemma4 variants, devstral). The dispatcher's adapter standardises on this format.
- **`adapted`** — tool calls are adapted via JSON-mode prompting through LiteLLM (e.g. qwen3:4b, ministral-3:8b).
- **`excluded_from_tools`** — very small models (< 2 GB) that don't reliably tool-call; the future router excludes them from tool-using request classes (e.g. qwen3:1.7b).

`_tool_mode_for()` derives tool_mode from the catalog entry's `native_tools` flag and `download_size_gb`.

Old taxonomy → new taxonomy mapping (for any external integrations that still consume the previous values):

| Old | New |
|-----|-----|
| `native` | `native_qwen3` |
| `json_fallback` | `adapted` |
| `limited` | `excluded_from_tools` |

### Runtime Health Monitoring

`useLocalModels.ts` provides `startHealthPolling()` / `stopHealthPolling()` that periodically probe the Ollama runtime. When `activeProvider === 'ollama'`, the main chat page starts polling every 30s. If Ollama becomes unreachable, `ollamaDown` state triggers a banner in ChatPanel with Reconnect/Switch to Cloud options.

### Slow Response Indicator

`useChat.ts` starts a timer when sending a message with the ollama provider. After 10s with no text_delta, it shows "Local model is loading...". After 30s, it shows an extended message suggesting a smaller model. Cleared immediately when the first text arrives or the response completes.

### Model Pull

`pull_model_stream()` proxies Ollama's `POST /api/pull` with `stream: true` and converts the progress events into SSE format for the frontend.

### Chat Integration

`_make_llm()` in `chat.py` routes through the [InferenceRouter](inference-router.md) (ADR 004) — for `provider == "ollama"`, the router resolves the user-selected model against the catalog and produces a `DispatchDecision`; `_llm_from_decision()` then constructs an `LLMService` with `api_base` set to the Ollama URL and a 1800s timeout (vs 120s for cloud). No API key is required — the sentinel value `"ollama"` satisfies LiteLLM's non-empty requirement.

The chat WS `done` event includes a `route` field with the routing decision's audit dict (`provider`, `model_id`, `request_class`, `slot_class`, `reason`). Frontends consume this to render which slot served the turn.

## Key Files

| File | Purpose |
|------|---------|
| [ollama_service.py](../../backend/services/ollama_service.py) | All Ollama logic: hardware/runtime probes, catalog, scoring, pull, config |
| [local_models.py](../../backend/routers/local_models.py) | REST API endpoints for local model management |
| [llm_service.py](../../backend/services/llm_service.py) | LLMConfig extended with `api_base` and `timeout`; model resolution for ollama |
| [chat.py](../../backend/routers/chat.py) | `_make_llm()` handles ollama provider; `base_url` passed through WS messages |
| [test_local_models.py](../../backend/tests/test_local_models.py) | 45 tests covering scoring, probes, config, routing |
| [useLocalModels.ts](../../frontend/app/composables/useLocalModels.ts) | Frontend composable: hardware/runtime/catalog state, pull SSE, model selection |
| [OllamaStatus.vue](../../frontend/app/components/OllamaStatus.vue) | Runtime status card (not installed / not running / running) with hardware info |
| [LocalModelCard.vue](../../frontend/app/components/LocalModelCard.vue) | Model recommendation card with compatibility badge, tool mode badge, download/use actions |
| [PullProgress.vue](../../frontend/app/components/PullProgress.vue) | Download progress bar with percentage and byte counts |
| [OnboardingLocalFlow.vue](../../frontend/app/components/OnboardingLocalFlow.vue) | Local model setup wizard for onboarding (hardware detect, Ollama check, recommend, download) |
| [test_local_models_integration.py](../../backend/tests/test_local_models_integration.py) | 22 tests: tool_mode, timeouts, warm-up, test endpoint, catalog |
| [useLocalModelsIntegration.test.ts](../../frontend/tests/composables/useLocalModelsIntegration.test.ts) | 6 tests: ollama chat, slow indicator, health polling |

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/local/hardware` | GET | Hardware profile (RAM, disk, CPU, GPU, tier) |
| `/api/local/runtime` | GET | Ollama status (installed, running, version) |
| `/api/local/runtime/load` | GET | Runtime load snapshot (RAM/swap/GPU + Ollama-loaded models) — consumed by ADR 004 dispatcher |
| `/api/local/models/catalog` | GET | Model catalog with recommendations |
| `/api/local/models/installed` | GET | Models downloaded in Ollama |
| `/api/local/models/pull` | POST | Download model (SSE progress stream) |
| `/api/local/models/select` | POST | Set active local model |
| `/api/local/models/{name}` | DELETE | Remove model from Ollama |
| `/api/local/models/test` | POST | Quick model validation (latency, tok/s) |
| `/api/local/models/warm-up` | POST | Keep model loaded in memory |

## Gotchas

- Ollama must be running separately — Jarvis doesn't start it
- Local models on CPU can be very slow (2–10 tok/s); 600s timeout is intentional
- Not all models support native function calling; `tool_mode` field in catalog classifies support level
- `done` event includes `tool_mode` for ollama provider so frontend can display tool support info
- `psutil` is required for hardware probe; falls back to OS commands if missing
- `api_base` must be passed to LiteLLM via `acompletion()` kwargs
- Health polling runs every 30s only when provider is ollama; stopped on unmount or provider change
- `base_url` from query/body is normalized and restricted to local loopback endpoints for safety
