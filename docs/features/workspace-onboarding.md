---
title: Workspace & Onboarding
status: active
type: feature
sources:
  - backend/routers/workspace.py
  - backend/services/workspace_service.py
  - backend/models/schemas.py
  - frontend/app/pages/onboarding.vue
  - frontend/app/components/OnboardingLocalFlow.vue
depends_on: [database, preferences-settings]
last_reviewed: 2026-05-05
last_updated: 2026-05-05
---

## Summary

Workspace & Onboarding handles the one-time setup required before Jarvis can be used: running the bundled-Ollama first-run pipeline (hardware probe + primary-model pull + chat-model self-test), creating the local directory tree, seeding built-in specialists, and recording that setup is complete. It runs exactly once — on subsequent starts the app detects an existing workspace and routes straight to the main view.

Per [ADR 015](../architecture/decisions/015-single-target-local-only-stack.md) the v1 product is local-only: there is no API key to collect, no provider choice to make, and no cloud branch to opt into. The whole onboarding surface is a single screen that drives a local-model pipeline and then creates the workspace.

## How it works

### Frontend

[`onboarding.vue`](../../frontend/app/pages/onboarding.vue) renders one component, [`OnboardingLocalFlow.vue`](../../frontend/app/components/OnboardingLocalFlow.vue), and listens for its `model-ready` event. When the event fires, the page calls `useApi().initWorkspace()` and navigates to `/main`.

`OnboardingLocalFlow` orchestrates two layered paths:

- **Layer 1 — orchestrator-driven** (default in the bundled build, [ADR 005](../architecture/decisions/005-hardware-tiered-model-stack-and-first-run-policy.md) §B). Drives [`useFirstRun`](../../frontend/app/composables/useFirstRun.ts) against `/api/local/first-run/*`. Auto-kicks the pipeline on mount when there is no marker file. Releases the user into chat the moment the foreground primary pull lands; the background fallback pull and the chat-model probe continue silently.
- **Layer 2 — manual model picker.** Engaged when the user clicks "Pick my own model later" (the §B opt-out path) or when Ollama is not reachable (dev mode without bundled sidecar). Drives [`useLocalSetupFlow`](../../frontend/app/composables/useLocalSetupFlow.ts), the legacy state machine still used by `LocalModelsSection.vue` in Settings.

The `wizardStep` indicator (1 → 2 → 3 = "Detect hardware" → "Download model" → "Start using Jarvis") maps both orchestrator state and manual flow.state onto the same 1..3 progress so the indicator stays stable across mode flips.

### Backend

[`create_workspace`](../../backend/services/workspace_service.py) does the work in a fixed sequence:

1. Checks that `{workspace}/app/config.json` does not already exist. If it does, raises `WorkspaceExistsError` immediately — no partial writes.
2. Creates the full directory tree (16 subdirectories under `app/`, `memory/`, `graph/`, and `agents/`) using `mkdir(parents=True, exist_ok=True)`.
3. Writes `app/config.json` with version, creation timestamp, and workspace path.
4. Seeds built-in specialists (e.g. Jira Strategist) via `seed_builtin_specialists`. Failures here are logged but non-fatal — the workspace is still considered created.
5. On any failure during steps 3–4, the directory tree is removed (`shutil.rmtree`) so the next run starts clean.

`get_workspace_status` reads `app/config.json` and returns `{ initialized, workspace_path? }`. The app shell calls this on mount to decide whether to route to onboarding or to `/main`.

`get_api_key()` is preserved as an inert shim that always returns `None`. It exists only so that legacy chat-router signatures (`api_key: str = ""`) keep working without a router-wide refactor; the chat dispatcher does not consume it.

## Key files

- [`backend/routers/workspace.py`](../../backend/routers/workspace.py) — Two HTTP endpoints (`/status`, `/init`) and `WorkspaceExistsError` → 409 mapping.
- [`backend/services/workspace_service.py`](../../backend/services/workspace_service.py) — Directory creation, config read/write, specialist seeding, inert `get_api_key` shim.
- [`backend/models/schemas.py`](../../backend/models/schemas.py) — `WorkspaceInitRequest` (empty body), `WorkspaceInitResponse`, `WorkspaceStatusResponse`.
- [`frontend/app/pages/onboarding.vue`](../../frontend/app/pages/onboarding.vue) — Single-screen host; emits no API-key UI.
- [`frontend/app/components/OnboardingLocalFlow.vue`](../../frontend/app/components/OnboardingLocalFlow.vue) — Orchestrator + manual-picker first-run pipeline.

## API / interface

```
GET /api/workspace/status
→ WorkspaceStatusResponse
  {
    initialized: boolean
    workspace_path?: string   // present only when initialized
  }

POST /api/workspace/init
Body: WorkspaceInitRequest {}     // empty by design (ADR 015)
→ 201 WorkspaceInitResponse { status: "ok", workspace_path: string }
→ 409 if workspace already exists
```

Helper functions available to the rest of the backend (imported directly from `workspace_service`):

```python
get_api_key(workspace_path?) -> Optional[str]   # always None per ADR 015
workspace_exists(workspace_path?) -> bool
get_workspace_status(workspace_path?) -> dict
```

## Gotchas

- **Workspace existence is determined solely by `app/config.json`.** Not by checking directories or the database. If the file is manually deleted, the system treats the workspace as uninitialized and re-creates it; `mkdir(exist_ok=True)` is safe but the existing memory/graph/agents trees survive untouched.

- **`WorkspaceInitRequest` is intentionally empty.** Per ADR 015 the body carries no API key or provider selection. The schema is preserved as a typed marker so router signatures keep their `body: WorkspaceInitRequest` parameter and OpenAPI introspection still describes the endpoint.

- **`get_api_key()` always returns `None`.** Removing it would force a router-wide refactor of `_handle_message` and friends. The shim is documented in code as removable when those callsites drop the `api_key` parameter; its presence is not a sign that v1 supports keys.

- **Onboarding does not initialize SQLite.** The workspace directories are created here, but the database is initialized at backend startup via `main.py`'s lifespan handler. `create_workspace` only guarantees the file structure and config exist; the DB is ready as soon as the backend finishes starting.

- **Specialist seed failures are non-fatal.** If `seed_builtin_specialists` raises, the error is logged and onboarding continues. A user can hit the main view with zero specialists and recover from Settings → Specialists.
