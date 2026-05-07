---
title: Chat Model Self-Test (Probe)
status: active
type: feature
sources:
	- backend/services/chat_model_probe.py
	- backend/routers/local_models.py
	- backend/tests/test_chat_model_probe.py
	- backend/tests/test_local_models.py
	- frontend/app/composables/useChatModelProbe.ts
	- frontend/app/components/ChatModelProbePanel.vue
	- frontend/app/components/OnboardingLocalFlow.vue
	- frontend/app/components/settings/LocalModelsSection.vue
	- frontend/app/pages/onboarding.vue
	- frontend/app/pages/main.vue
depends_on: [local-models, latency-benchmark, preferences-settings]
last_reviewed: 2026-05-04
last_updated: 2026-05-04
---

# Chat Model Self-Test (Probe)

Per-machine, per-environment validation that the locally-installed chat
model produces clean output, fits in RAM, and runs fast enough to feel
competitive — runs on first launch and re-runs on environment change.

See [ADR 012](../architecture/decisions/012-chat-model-self-test.md) for
the full decision record and the per-environment failure modes that
motivated the probe.

## Summary

The probe runs three falsifiable checks against each user-pickable
catalog entry, in cheapest-first order, and persists the verdict to
`app/config.json`:

1. **Hardware fit** — `effective_footprint_bytes(entry, default_ctx) ≤ 80% × total_ram`.
   Same predicate the future memory-pressure auto-downgrade uses.
2. **Correctness** — `Say hi in one word.` with `think: false` and
   `num_predict: 8`; the response must not match a panel of
   thinking-prose patterns. Catches the Qwen3-30B-A3B + Ollama 0.18.0
   chain-of-thought leak documented in
   [ADR 010 §Issue 4](../architecture/decisions/010-conversation-replay-eval-harness.md).
3. **Speed** — `warm-short` total ≤ 1500 ms AND `chat-realistic-shallow`
   decode TPS ≥ 8. Reuses the latency-harness scenarios so dev numbers
   and customer numbers come from byte-identical code.

The orchestrator iterates user-pickable entries largest-first; the first
candidate that passes all three wins. If nothing passes, the recommendation
is `None` and `safe_fallback_used` is `True` — the chat router falls back
to the smallest catalog entry until the user resolves the situation
manually.

## How It Works

### Backend

`services/chat_model_probe.py` exposes:

- `iter_probe_events(*, base_url, ollama_version, candidates) → AsyncIterator[dict]` —
  the streaming primitive. Yields `started`, `candidate_start`,
  `candidate_evidence`, and a final `complete` event whose payload
  matches the on-disk record. Both the SSE endpoint and the blocking
  `recommend_chat_model` wrapper drain this generator.
- `recommend_chat_model(...)` — drains `iter_probe_events` and returns
  the final `ProbeResult`. Test-friendly, no streaming.
- `current_environment(*, ollama_version)` — captures `(ollama_version,
  platform, catalog_models)` for re-run-trigger comparison. `platform`
  includes the macOS major version on darwin (`darwin-arm64-macos14`)
  because the Qwen3-30B-A3B leak appeared on macOS 26 but not 14.
- `catalog_models` is a sorted snapshot of the user-pickable catalog at
  probe time. `needs_rerun` compares against this — *not* against
  `candidates_evaluated`, which is a subset because the orchestrator
  breaks on the first passing candidate. Without the snapshot, every
  unevaluated catalog entry would falsely trigger `catalog_added_models`
  on every load.
- `needs_rerun(persisted, current) → (bool, reason)` — returns the
  re-run reason (`no_prior_probe` / `ollama_version_changed` /
  `platform_changed` / `catalog_added_models` / `fresh`). Catalog
  *removals* don't trigger a re-run — only additions warrant
  re-evaluating.
- `persist_probe_result(...)` / `read_probe_result(...)` /
  `set_user_override(...)` / `effective_chat_model(...)` — persistence
  helpers, all routed through `locked_config_update` so concurrent
  writers don't lose data.

### HTTP API

Three endpoints in `routers/local_models.py`:

- `GET /api/local/chat-model-probe` — returns the persisted record (or
  `null`), the `needs_rerun` flag with its reason, the current captured
  environment, and `runtime_reachable`. The frontend calls this on every
  app boot and on the settings page.
- `POST /api/local/chat-model-probe/run` — server-sent events. Refuses
  with 503 when Ollama is unreachable so we don't waste 30 seconds
  marking every candidate `fail_unreachable`. On the `complete` event
  the result is persisted; any pre-existing `user_override` is
  preserved across re-runs.
- `POST /api/local/chat-model-probe/override` (body: `{ "model":
  "qwen3:14b" }` or `{ "model": null }`) — sets or clears
  `user_override`. Setting an override does not re-run the probe; the
  user is opting out of the recommendation.

### Re-run triggers

The probe re-runs (and overwrites the persisted recommendation) when:

- No prior probe exists in `app/config.json` (first launch).
- `ollama_version` changed since the last run.
- The platform string changed (macOS major-version bump, OS swap, arch
  swap).
- A new model was added to the user-pickable catalog (catalog
  *removals* don't trigger a re-run; the existing pick is still valid
  for the models that remain).

The user can also force a re-run from the settings page at any time.

### Frontend

`composables/useChatModelProbe.ts` exposes:

- `fetchStatus()` — `GET /api/local/chat-model-probe`; populates
  `persisted`, `needsRerun`, `rerunReason`, `currentEnvironment`,
  `runtimeReachable`.
- `runProbe()` — opens the SSE stream; pushes events into a reactive
  `events` array so onboarding can render per-candidate progress
  (`probing 1/3: qwen3:30b-a3b-instruct-2507-q4_K_M…`).
- `setOverride(model | null)` — `POST /api/local/chat-model-probe/override`.
- `effectiveModel` — computed; returns `user_override` or
  `recommended_model` or `null`.

`OnboardingLocalFlow.vue` triggers the probe automatically after a
model finishes downloading and before transitioning to the
`model_ready` state, surfacing per-candidate progress.

`settings/LocalModelsSection.vue` renders a "Chat-model self-test"
panel showing the persisted verdict, evidence, the re-run trigger
reason, a "Re-run probe" button, and an override picker.

`pages/main.vue` calls `fetchStatus()` on mount; when `needsRerun` is
true it surfaces a non-blocking banner offering to re-run in
background.

## Persistence shape

Stored under the `chat_model_probe` key in `app/config.json`:

```jsonc
{
  "chat_model_probe": {
    "schema_version": 1,
    "timestamp_utc": "2026-04-29T...",
    "ollama_version": "0.18.0",
    "platform": "darwin-arm64-macos14",
    "ram_gb": 24,
    "recommended_model": "qwen3:14b",
    "safe_fallback_used": false,
    "candidates_evaluated": [
      {"model": "qwen3:30b-a3b", "verdict": "fail_correctness",
       "correctness_response": "Okay, the user asked..."},
      {"model": "qwen3:14b", "verdict": "pass",
       "warm_short_total_ms": 299, "realistic_tps": 14.0}
    ],
    "user_override": null,
    "catalog_models": ["qwen3:14b", "qwen3:30b-a3b", "qwen3:8b"]
  }
}
```

The `complete` SSE event payload uses the exact same keys, so the
frontend can persist in-memory state directly without re-fetching after
the run finishes.

## Testing

44 unit tests in `test_chat_model_probe.py` cover:

- Thinking-prose pattern panel (8 leaked + 7 clean cases).
- Hardware-fit at threshold edges + KV-architecture differences
  (transformer vs Mamba/SWA).
- Orchestrator with stubbed Ollama (largest-first iteration,
  safe-fallback when nothing passes, hardware-fit prefilter skips
  before any Ollama call).
- Persistence round-trip + non-clobbering of other config keys +
  `catalog_models` survives the round trip (regression: dropping it
  caused `needs_rerun` to return `no_prior_probe` forever, leaving the
  re-run banner stuck on) + strict key parity (the persisted JSON key
  set equals `{f.name for f in fields(ProbeResult)}` exactly, so any
  field added to the dataclass without a matching persistence write
  fails CI immediately) + override-vs-recommendation precedence.
- `set_user_override` on empty config / existing record / clear.
- `needs_rerun` across all five reason codes; macOS-major version is
  baked into `platform`. Two regression tests cover the
  `catalog_models` snapshot path: (1) early-exit evidence subset must
  not falsely trigger `catalog_added_models`, and (2) pre-snapshot
  records (missing the field) force a fresh probe so the new shape
  gets persisted.
- `iter_probe_events` event sequence + final-event payload shape
  matches the persisted record.

12 endpoint tests in `test_local_models.py::TestChatModelProbeEndpoints`
cover GET status / run-refused-when-Ollama-down / override get+clear /
SSE streaming + persistence / user-override preservation across re-runs.
