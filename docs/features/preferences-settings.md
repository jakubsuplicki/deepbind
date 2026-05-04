---
title: Preferences & Settings
status: active
type: feature
sources:
  - backend/routers/preferences.py
  - backend/routers/settings.py
  - backend/services/preference_service.py
  - backend/services/privacy.py
  - backend/services/token_tracking.py
  - backend/config.py
  - frontend/app/pages/settings.vue
  - frontend/app/composables/usePreferences.ts
depends_on: [workspace-onboarding, local-models, chat-model-probe]
last_reviewed: 2026-05-04
last_updated: 2026-05-04
---

# Preferences & Settings

## Summary

Preferences & Settings covers two related concerns:
- **User preferences** — arbitrary key/value pairs persisted to disk and injected into the model's system prompt via [`preference_service.format_for_prompt()`](../../backend/services/preference_service.py).
- **Application settings** — the [`/settings`](../../frontend/app/pages/settings.vue) page surface: local-model management, voice toggles, privacy kill-switches, retrieval/graph-expansion tuning, sharpen-with-local-AI, and maintenance actions.

Both are backed by a flat JSON file in the workspace (`{workspace}/app/preferences.json`), so they survive database resets and remain human-readable.

> **Scope under ADR 015.** The single-target local-only stack ([ADR 015](../architecture/decisions/015-single-target-local-only-stack.md)) removed all cloud-provider machinery from the repo. The settings surface no longer carries: the API key entry UI, an "Allow cloud LLM providers" toggle, dollar-cost estimates, or the daily token-budget gate. Anything previously documented here that referenced Anthropic / OpenAI / Google has been deleted along with the underlying code paths.

## How It Works

### Two layers, one page

The Settings page (`/settings`) composes one Vue section per concern from [`frontend/app/components/settings/`](../../frontend/app/components/settings/). Most sections live in their own feature docs; this page documents the cross-cutting plumbing plus the sections owned by *preferences-settings* itself (privacy, sharpen, maintenance, graph expansion, lightweight mode).

`GET /api/settings` is the small aggregating view-model. Mutations go to dedicated sub-routes (`PATCH /api/settings/voice`, `PATCH /api/settings/privacy`, `PATCH /api/settings/retrieval`, `PATCH /api/settings/lightweight-mode`). The raw preferences CRUD ([`/api/preferences`](../../backend/routers/preferences.py)) is a separate surface used by composables and the system-prompt builder, not by the settings page directly.

### Preference storage

[`preference_service.py`](../../backend/services/preference_service.py) reads and writes a single file: `{workspace}/app/preferences.json`. Format is a flat `{ "key": "value" }` object where all values are strings — no schema enforcement beyond the non-empty key check.

On write the service does a read–modify–write cycle: load file, update the one key, overwrite. Not atomic, but preferences are low-frequency writes from a single local process — torn-write risk is negligible.

### Preferences as system-prompt context

`preference_service.format_for_prompt()` converts the full preference map into a bulleted string formatted as `- [key] value`. The system-prompt builder injects this into every chat turn so the model sees declared preferences without a retrieval round-trip. If the file is empty or absent, the function returns `None` and nothing is injected.

### Voice preferences are namespaced preferences

Voice toggles (`auto_speak`, `tts_voice`) are stored as regular preference entries with a `voice_` prefix. `PATCH /api/settings/voice` enforces a whitelist of valid keys before writing. The settings view-model strips the prefix when returning them so callers see plain `auto_speak` / `tts_voice` fields.

### Privacy kill-switches

[`services/privacy.py`](../../backend/services/privacy.py) is the single source of truth for "is this outbound network call allowed?" — every feature that touches the public internet (web search, URL ingest) consults this module. The model has three layers, in priority order:

1. **`JARVIS_OFFLINE_MODE` environment variable.** Hard lock. When set to `1`/`true`/`yes`/`on`, every outbound integration is blocked and the UI cannot override it. The settings page receives `offline_mode_locked: true` and disables the master toggle.
2. **`privacy_offline_mode` preference.** User-controlled master toggle. When on, all per-feature toggles are forced off regardless of their stored value — flipping one switch is one click rather than two.
3. **Per-feature preferences:** `privacy_web_search_enabled`, `privacy_url_ingest_enabled`. Default `true` so existing users see no behavioural change; the kill-switches are opt-in protections.

Local Ollama, embeddings, and the cross-encoder reranker are **never gated** — they run on this machine. There is no `cloud_providers_enabled` toggle: the bundle structurally cannot make cloud LLM calls (ADR 015 §F audit signal).

`get_privacy_settings()` returns the *effective* state (offline-mode-aware), not the raw preferences. Callers should always go through this helper rather than reading `privacy_*` preferences directly. `update_privacy_settings()` is the only sanctioned write path: it rejects unknown keys and refuses to disable offline mode while the env-var lock is engaged (raises `PrivacyBlockedError`).

When a feature is blocked, callers must surface the reason rather than silently failing — `web_search.py` returns `[{"error": "Web search is blocked because Offline Mode is enabled."}]` so the model sees the message and can fall back to local search.

### Token usage tracking (telemetry only)

[`services/token_tracking.py`](../../backend/services/token_tracking.py) logs per-turn token counts to a JSONL file (`{workspace}/app/logs/usage.jsonl`) and aggregates them via `get_usage_summary()` / `get_usage_today()` / `get_usage_by_day()`. The legacy `cost_estimate` field is hardcoded to `0.0` — local Ollama has no per-call dollar cost.

The pre-dispatch budget gate that previously blocked chat at 100% budget consumption was removed in 2026-05-04. `DEFAULT_DAILY_BUDGET` is `0` (unlimited). `check_budget()` and `get_daily_budget()` survive as library functions but are not currently consumed by any production code path; the budget UI was deleted along with the gate.

### Lightweight mode

The `lightweight_mode` preference (default off) pins chat dispatch to the smallest installed model on the user's hardware tier and bypasses memory-pressure auto-downgrade. Surfaced as the `PerformanceSection` toggle. Useful when the machine is RAM-pressured by other apps and the user wants chat to "just work" without mid-turn switching chatter. See [ADR 005 §C trigger 3](../architecture/decisions/005-hardware-tiered-model-stack-and-first-run-policy.md) for the underlying mechanism.

### Graph-expansion toggles

Three boolean knobs persisted under `config.json` → `retrieval.graph_expansion`:

| Key | Default | Effect |
|---|---|---|
| `use_related` | `true` | Pull notes via confirmed `related` edges. Highest-trust. |
| `use_part_of` | `true` | Pull sibling notes from the same project / area. |
| `use_suggested_strong` | `false` | Pull notes via unconfirmed `suggested_related` ≥ 80% confidence. Opt-in. |

Surfaced as `GraphExpansionSection`. Wired via `GET/PATCH /api/settings/retrieval`.

### Frontend composable

[`usePreferences.ts`](../../frontend/app/composables/usePreferences.ts) wraps the `/api/preferences` endpoints with an optimistic update pattern: it writes the new value into the local `preferences` ref immediately, calls the API, and reverts to a fresh server fetch if the call fails. Keeps the UI responsive while staying consistent on error.

### Settings page section breakdown

[`settings.vue`](../../frontend/app/pages/settings.vue) is a thin shell composing one Vue component per concern. Sections owned elsewhere are documented in their feature docs:

| Section component | Owning feature |
|---|---|
| `LocalModelsSection.vue` | local-models *(redesigned 2026-05-04 — see below)* |
| `PerformanceSection.vue` | preferences-settings (lightweight-mode toggle) |
| `WorkspaceSection.vue` | workspace-onboarding |
| `McpSection.vue` | mcp-server |
| `VoiceSection.vue` | voice |
| `MaintenanceSection.vue` | preferences-settings (reindex, graph rebuild) |
| `SmartConnectSection.vue` | smart-connect |
| `GraphExpansionSection.vue` | preferences-settings (graph-expansion knobs) |
| `PrivacySection.vue` | preferences-settings (offline mode + per-feature toggles) |
| `SharpenSection.vue` | preferences-settings (local-AI enrichment runner) |
| `SettingsSection.vue` | preferences-settings (base wrapper — collapsible card) |

Each non-trivial section pairs with a composable under [`frontend/app/composables/settings/`](../../frontend/app/composables/settings/) (`useGeneralSettings.ts`, `useSharpen.ts`, `usePrivacySettings.ts`, `useGraphExpansionSettings.ts`, `useSettingsStatus.ts`, `useLightweightMode.ts`). Composables read/write through `/api/preferences` and `/api/settings/*` — no per-section backend.

### LocalModelsSection — active-model hero (redesigned 2026-05-04)

The Local Models section now leads with a hero card for the **active chat model** so switching models and reading current capacity is a one-glance operation:

- **Capacity strip** — shows hardware label (`16 GB · Apple Silicon`) and what the machine runs comfortably (`up to 13B models`) plus effective context window. Driven by `useLocalSetupFlow.hardwareSummary`.
- **Active model card** — eyebrow ("Active chat model") + pulsing status dot (cyan healthy / amber slow / soft-green fast, per `useChatHealth.statusFor`) + model name + capability descriptor ("Solid chat — good speed and depth") + quality dots. Stats row: real tok/s baseline (from the chat-model-probe via `useChatHealth.getBaseline`) + health pill, context window, size on disk.
- **Change model** — `<select>` dropdown listing all installed models with quality + context metadata. One-step switch. Disabled when only one model is installed (prompts the user to install another).
- **Probe panel** — `ChatModelProbePanel` mounts directly below the hero card, so the speed reading and the "re-test" surface live next to the model they belong to.
- **Install another model** — collapsed disclosure separating *install* from *switch*. Inside: "Recommended for your hardware" (top 3 not yet installed) and "All other models" (the rest).
- **Advanced** — Ollama URL override + test connection.

The section now defaults open (`:default-open="true"`) since it is the primary control surface for local AI.

## Key Files

- [`backend/routers/preferences.py`](../../backend/routers/preferences.py) — CRUD endpoints for arbitrary key/value preferences; used by composables and internal services.
- [`backend/routers/settings.py`](../../backend/routers/settings.py) — Aggregated settings view, voice update, privacy CRUD, retrieval graph-expansion CRUD, lightweight-mode CRUD, token-usage read-only endpoints.
- [`backend/services/preference_service.py`](../../backend/services/preference_service.py) — Reads, writes, and deletes entries in `preferences.json`; provides `format_for_prompt()` for system-prompt injection.
- [`backend/services/privacy.py`](../../backend/services/privacy.py) — Central privacy / network kill-switches. `get_privacy_settings()` resolves env lock + master toggle + per-feature flags into an effective state; `web_search_enabled()` / `url_ingest_enabled()` gate every outbound integration.
- [`backend/services/token_tracking.py`](../../backend/services/token_tracking.py) — JSONL-based token-usage logging plus daily / all-time aggregation. Cost field is structurally zero; budget gate is unwired.
- [`backend/config.py`](../../backend/config.py) — Pydantic-settings `Settings` model with `JARVIS_`-prefixed env-var support and `.env` file bootstrap; singleton via `lru_cache`.
- [`frontend/app/pages/settings.vue`](../../frontend/app/pages/settings.vue) — Thin shell composing one section per concern.
- [`frontend/app/composables/usePreferences.ts`](../../frontend/app/composables/usePreferences.ts) — Thin composable over `/api/preferences` with optimistic update and automatic revert on failure.
- [`frontend/app/composables/settings/useGeneralSettings.ts`](../../frontend/app/composables/settings/useGeneralSettings.ts) — Loads `/api/settings`, exposes `workspacePath` + `autoSpeak`, and provides reindex / rebuild-graph actions.

## API / Interface

### `GET /api/settings`

Returns the small view-model used by the settings page on mount.

```typescript
{
  workspace_path: string         // absolute path to the workspace
  voice: {
    auto_speak: string           // "true" | "false" (string, not boolean)
    tts_voice: string            // "default" or a voice name
  }
}
```

There is no `api_key_set` and no `key_storage` field — keys are not a concept in v1 (ADR 015). `PATCH /api/settings/api-key` returns 404 (route does not exist) — that absence is part of the audit signal a procurement reviewer probes for.

### `PATCH /api/settings/voice`

```typescript
// Request — any subset of valid keys
{ auto_speak?: string, tts_voice?: string }

// Response — full updated voice block
{ auto_speak: string, tts_voice: string }
```

Returns 422 if any key in the body is not in the allowed set (`{auto_speak, tts_voice}`).

### `GET /api/settings/privacy` / `PATCH /api/settings/privacy`

```typescript
// GET response — effective state (env lock + master toggle + per-feature)
{
  offline_mode: boolean
  offline_mode_locked: boolean       // true when JARVIS_OFFLINE_MODE env var is set
  web_search_enabled: boolean
  url_ingest_enabled: boolean
}

// PATCH body — any subset of:
{ offline_mode?: boolean, web_search_enabled?: boolean, url_ingest_enabled?: boolean }
```

PATCH returns 409 with a `PrivacyBlockedError` message if the user attempts to disable offline mode while the env-var lock is engaged. Returns 422 for unknown keys or non-boolean values.

### `GET /api/settings/retrieval` / `PATCH /api/settings/retrieval`

```typescript
{
  graph_expansion: {
    use_related: boolean
    use_part_of: boolean
    use_suggested_strong: boolean
  }
}
```

PATCH expects a `graph_expansion` object containing any subset of the three keys. Persisted to `{workspace}/app/config.json` under `retrieval.graph_expansion`.

### `GET /api/settings/lightweight-mode` / `PATCH /api/settings/lightweight-mode`

```typescript
// Both shapes
{ enabled: boolean }
```

Persisted as the `lightweight_mode` preference (string `"true"` / `"false"`).

### `GET /api/settings/usage` / `GET /api/settings/usage/today` / `GET /api/settings/usage/history`

Read-only delegates to `token_tracking`. `cost_estimate` is always `0.0`. Daily history returns the last 14 UTC days as `[{ date, total_tokens, ... }]`.

### `GET /api/preferences` / `PATCH /api/preferences` / `DELETE /api/preferences/{key}`

```typescript
// GET response
Record<string, string>

// PATCH body (PreferenceSetRequest)
{ key: string, value: string }

// PATCH response
Record<string, string>            // full updated map

// DELETE response
{ status: "deleted" }             // idempotent — succeeds even if the key did not exist
```

PATCH returns 400 if `key` is empty.

## Gotchas

- **All preference values are strings.** The `auto_speak` voice preference is stored as the string `"true"` or `"false"`, not a boolean. UI code must compare against the string explicitly — a plain truthiness check on a non-empty string would always evaluate to `true`.
- **`format_for_prompt()` returns `None`, not an empty string**, when no preferences exist. Callers in the system-prompt builder must guard for `None` before concatenating.
- **`DELETE /api/preferences/{key}` is silently idempotent** — a delete on a non-existent key returns 200, not 404. Callers should not rely on the response to confirm the key ever existed.
- **`lru_cache` on `get_settings()` means runtime env-var changes are invisible.** A process restart is required to pick up a changed `JARVIS_WORKSPACE_PATH`.
- **The `.env` parser uses `os.environ.setdefault`.** Values already in the environment at startup will never be overridden by `.env`. By design (container/system env wins) but can surprise developers who edit `.env` and expect it to take effect over a pre-existing shell export.
- **Workspace must exist before preferences can be written.** `preference_service.save_preference()` creates `app/` with `mkdir(parents=True, exist_ok=True)`, but the parent `workspace/` directory is assumed to already exist from the onboarding flow.
- **Privacy `cloud_providers_enabled` does not exist.** Older clients that PATCH this key receive a 422 (`Unknown key: cloud_providers_enabled`). The bundle has no cloud-provider code paths to gate at runtime — the gate is structural (no SDK, no router, no UI) per [ADR 015](../architecture/decisions/015-single-target-local-only-stack.md).
- **The token-budget UI is gone.** Any historical reference to setting a daily budget through the settings page no longer applies. The `check_budget()` function survives in the codebase but is not currently consumed; if reintroduced, the gate must apply at a controllable surface (chat router) and ship with a UI to raise the limit.
