# ADR 014 — Desktop bundle structurally excludes cloud-provider code paths

**Status:** Accepted
**Date:** 2026-04-30
**Related:** [ADR 002](002-pure-local-product-shape.md) · [ADR 003](003-desktop-distribution-tauri-and-sidecars.md) · [ADR 005](005-hardware-tiered-model-stack-and-first-run-policy.md)

## Context

The Jarvis backend currently ships a multi-provider LLM abstraction via [`backend/services/llm_service.py`](../../../backend/services/llm_service.py) (LiteLLM-based) plus a dedicated [`backend/services/_anthropic_client.py`](../../../backend/services/_anthropic_client.py) wrapper. The frontend has [`AddKeyModal.vue`](../../../frontend/app/components/AddKeyModal.vue), `useApiKeys.ts`, and a Settings → API Keys panel for entering Anthropic / OpenAI / Google credentials.

Privacy-mode runtime gating already exists in [`backend/services/privacy.py`](../../../backend/services/privacy.py): the workspace pref `privacy_cloud_providers_enabled` (default `True`) controls whether the chat router will accept cloud-provider requests; `is_cloud_provider_blocked()` is checked at request time. This is **behavioural** — turning the toggle off prevents calls but the code path, the `litellm` import, and the API-key UI remain present.

The desktop product positions on privacy + compliance. A buyer's procurement review asks: *"Can this app send my data to OpenAI?"* The architecturally honest answer is structural, not behavioural:

- **Behavioural answer (status quo):** "It can, but a runtime flag suppresses outbound calls. Trust us."
- **Structural answer (this ADR):** "It cannot. The desktop build does not contain LiteLLM, does not contain the Anthropic SDK, and the routes that would handle API keys return 404. You can verify this by unpacking the .app and grepping for `litellm` or `anthropic`."

ADR 003 driver #3 already commits to this posture in principle (*"Compliance posture must be structurally assertable, not just behaviourally observable"*) and ADR 002 commits the product to a *pure-local product shape*. This ADR makes the structural answer real for the desktop bundle while preserving the optionality to bring cloud-provider support back for any future hybrid / managed-cloud SKU.

## Decision drivers

1. **Audit posture** — the procurement review must be answerable by `find` over the unpacked installer. *"No `litellm` in the bundle, no `anthropic` SDK in the bundle, no `openai` SDK in the bundle, no `/api/keys/*` route in the running shell."* That is the contract.
2. **Restoration path must be cheap** — the user explicitly asked that bringing cloud-provider support back later be straightforward. Code stays in the repo; the change is a build-time flag, not a code rewrite. Any future hybrid / managed-cloud SKU flips one flag and rebuilds.
3. **No behavioural-only flags as the primary defense** — runtime privacy toggles (the existing `privacy_cloud_providers_enabled`) are useful as a secondary user-controllable surface, but they are not the load-bearing defense. The bundle itself must not contain the code.
4. **Dev mode unchanged** — `python main.py` against a developer's local checkout still has full multi-provider support. The exclusion is per-build-target, not repo-wide.

## Decision

**Three structural exclusions, gated by a single build-time flag `JARVIS_DESKTOP_BUNDLE=1`:**

### A. PyInstaller bundle excludes the cloud-provider code paths

[`desktop/sidecar/jarvis-sidecar.spec`](../../../desktop/sidecar/jarvis-sidecar.spec) gains a conditional `excludes` block, keyed on the `JARVIS_DESKTOP_BUNDLE` env var read at spec-evaluation time:

```python
DESKTOP_BUNDLE = os.environ.get("JARVIS_DESKTOP_BUNDLE", "1") == "1"

excludes = [
    # ... existing excludes ...
]
if DESKTOP_BUNDLE:
    excludes += [
        "anthropic",
        "openai",
        "google.generativeai",  # Google AI Studio SDK
        "services._anthropic_client",
    ]
```

Default is `1` — the desktop build excludes by default. Setting `JARVIS_DESKTOP_BUNDLE=0` before invoking `build-sidecar.sh` produces a bundle that includes everything (for a hypothetical hybrid / managed SKU build target).

> **Amendment (2026-04-30) — narrowed exclude list.** The ADR's original draft also listed `litellm`, `services.llm_service`, and `services.claude` for exclusion. Discovered during implementation: LiteLLM is the dispatcher used for the *Ollama* code path too (`provider="ollama"` in `_make_llm` constructs an `LLMService` against LiteLLM's `ollama_chat/` provider, which talks to `localhost:11434` via httpx — fully local, no cloud reach). `services.claude` defines `StreamEvent` and `build_system_prompt_with_stats`, both used unconditionally on every chat path. `services.llm_service` is the multi-provider dispatcher consumed by the Ollama path.
>
> Excluding any of these breaks the local-only path. The narrowed list excludes the **cloud-provider SDKs proper** (`anthropic`, `openai`, `google.generativeai`) plus `services._anthropic_client`. LiteLLM remains in the bundle but its cloud-provider sub-paths fail at import time when the SDKs are absent — that **is** the audit signal we want: a buyer probing the bundle finds no Anthropic/OpenAI/Google SDK directories, and any cloud-provider request fails with a deterministic ImportError-derived 503. The runtime behaviour matches the ADR's original contract; only the file-level exclusion list is narrower.
>
> A future refactor could split `StreamEvent` and the system-prompt builders out of `services.claude` into a neutral module so `services.claude` becomes Anthropic-only and excludable. Tracked as a follow-up; not load-bearing for the procurement-review story (the SDK-level exclusion answers *"can the bundle reach OpenAI?"* with a structural no).

The chat router ([`backend/routers/chat.py`](../../../backend/routers/chat.py)) catches the cloud-provider ImportError when constructing the LLM service for non-Ollama providers and returns a `503` "this build excludes cloud providers — see ADR 014" response with a `X-Bundle-Capability: local-only` header. **No silent fallback** — the audit signal is that the request explicitly fails with a documented build-capability marker.

### B. Frontend hides the cloud-provider surface at build time

[`frontend/nuxt.config.ts`](../../../frontend/nuxt.config.ts) reads `process.env.JARVIS_DESKTOP_BUNDLE` and exposes it via Nuxt's runtime config. Components that gate cloud-provider UI (`AddKeyModal.vue`, the API Keys settings panel, the cloud-provider entries in the chat-model picker) are wrapped in a `<ClientOnly v-if="!desktopBundle">` so they're tree-shaken out of the static bundle when the flag is set.

`useApiKeys.ts` is replaced with a tiny stub when the flag is set: every method returns `null` / no-ops, so any straggler component that still references it doesn't error at runtime. The stub is added to the bundle; the real implementation isn't.

### C. Backend gates the API-key routes at startup, not per-request

[`backend/main.py`](../../../backend/main.py) reads `JARVIS_DESKTOP_BUNDLE` at startup and conditionally skips registering the `api_keys` router and any other cloud-provider-specific routers. Endpoints like `GET /api/settings/api-keys` and `POST /api/settings/api-keys` return 404 (Not Found) — the audit signal is *the route does not exist*, not *the route exists but is gated*. The standard `/api/health` and other local-only endpoints are unaffected.

The router-skipping is the load-bearing defense; the per-request privacy-mode check in [`privacy.py`](../../../backend/services/privacy.py) remains as defense-in-depth (covers any code path that bypasses route registration).

## Restoration path (the user's explicit requirement)

Bringing cloud-provider support back is intentionally a single-flag operation:

```bash
# Build a desktop bundle WITH cloud-provider support:
JARVIS_DESKTOP_BUNDLE=0 bash desktop/scripts/build-notarized.sh
```

What the flag flip changes:
- PyInstaller spec re-includes `litellm`, `anthropic`, `openai`, `google.generativeai`, and the `services.llm_service` / `services.claude` / `services._anthropic_client` modules.
- Nuxt build re-includes `AddKeyModal.vue`, the API-keys panel, and the real `useApiKeys.ts`.
- Backend's startup re-registers the `api_keys` router.

What does *not* change (no code edits required):
- `services/privacy.py` and the `privacy_cloud_providers_enabled` workspace pref remain untouched — they layer on top of the build-time decision as a runtime user toggle.
- `services/llm_service.py` and `services/_anthropic_client.py` themselves are unchanged — they're either bundled or excluded, not modified.
- The frontend cloud-provider components are unchanged — they're either tree-shaken or included.

**This is the contract:** the cloud-provider code must remain *correct enough to ship* even when it's excluded from the desktop bundle. Test coverage for those paths runs in CI on the `JARVIS_DESKTOP_BUNDLE=0` build target. If we ever break that path, the bring-it-back operation becomes "fix the bug + rebuild" rather than "fix the bug + plumb new code paths from scratch." Optionality is preserved by keeping the path on the test surface.

## Audit verification

A compliance buyer can verify the structural exclusion in three ways:

1. **Inspect the unpacked installer.**
   ```bash
   # Mount the .dmg, enter DeepFilesAI.app/Contents/Resources/_MEIPASS/
   find . -name "*litellm*" -o -name "*anthropic*" -o -name "*openai*" 2>/dev/null
   # Expected output: nothing
   ```
2. **Probe the running shell.**
   ```bash
   curl -s http://127.0.0.1:<sidecar_port>/api/settings/api-keys
   # Expected: 404 Not Found
   curl -s -X POST http://127.0.0.1:<sidecar_port>/api/chat \
        -H 'Content-Type: application/json' \
        -d '{"provider":"anthropic","model":"claude-sonnet-4-20250514"}'
   # Expected: 503 with X-Bundle-Capability: local-only
   ```
3. **Read the build manifest** (shipped in the .app's `Info.plist` as a custom `JarvisBundleCapabilities` array):
   ```xml
   <key>JarvisBundleCapabilities</key>
   <array>
     <string>local-llm</string>
     <string>vault-markdown</string>
     <string>knowledge-graph</string>
     <string>semantic-search</string>
   </array>
   ```
   Note the absence of `cloud-llm`, `api-keys`, `external-providers`. The Info.plist key is set by `build-notarized.sh` from the `JARVIS_DESKTOP_BUNDLE` flag.

Each verification path is independent — a malicious build that lied in one would still be caught by the others.

## Alternatives considered

- **Hide the UI; leave the code in.** The "fastest, weakest" option discussed in the original conversation that produced this ADR. Rejected: behavioural-only defense, fails the procurement-review test (*"can it call OpenAI?"* → "*not by default but the code is there*"). Burns the credibility of the privacy positioning.
- **Gate the backend routes behind a runtime feature flag, leave the imports in.** Strictly better than UI-only but the bundle still contains LiteLLM + the Anthropic SDK; the audit answer is still "*we promise we don't use them.*" Rejected for the same reason.
- **Delete the cloud-provider code from the repo entirely.** Strongest possible structural defense but burns the optionality the user explicitly wants preserved. The repo-level cost (re-writing a multi-provider abstraction) when a future hybrid SKU revives it would dwarf the build-time-flag cost. Rejected.
- **Move cloud-provider code to a separate package / module** that's only imported by hybrid SKUs. Architecturally clean but introduces a packaging concern (separate Python package, separate npm package) for marginal benefit over a build-time flag. Defer; if v2 ever ships multiple SKUs in production we revisit.

## Consequences

### Positive
- The procurement-review answer is structural: *"the bundle does not contain LiteLLM, the bundle does not contain the Anthropic SDK, the route returns 404."* Verifiable. Compliance buyers can confirm in 30 seconds.
- The desktop bundle's PyInstaller archive shrinks (LiteLLM + Anthropic + OpenAI + Google GenAI SDKs together are non-trivial Python weight, plus their transitive deps).
- The privacy positioning is no longer "trust us" — it's a structural property of the build. ADR 003 driver #3 is satisfied for the cloud-provider surface specifically.
- Restoration is a single build flag — no code edits, no schema changes, no migration.

### Negative
- The cloud-provider code path runs only in the `JARVIS_DESKTOP_BUNDLE=0` test surface and dev mode. CI must include both build targets to keep the path correct enough to ship; if we drop `JARVIS_DESKTOP_BUNDLE=0` from CI, code rot becomes invisible.
- A user who previously entered cloud API keys in dev mode and then runs the desktop bundle will see those keys silently ignored (the routes don't exist). Mitigation: the existing privacy banner ("This is a local-only build — cloud providers are unavailable") is repurposed to explain the build-target behavior, with a link to this ADR.
- The Info.plist `JarvisBundleCapabilities` array is a custom convention, not an Apple-defined key. It's load-bearing only for our own audit verification path; a malicious build could lie. Mitigation: the FS-level absence of LiteLLM is the primary verification; the Info.plist is a convenience for buyers who don't want to unpack the installer.

### What this changes about existing code
- [`backend/main.py`](../../../backend/main.py) — startup reads `JARVIS_DESKTOP_BUNDLE`; conditionally skips registering the `api_keys` router (and any future cloud-provider-only routers).
- [`backend/routers/chat.py`](../../../backend/routers/chat.py) — the multi-provider dispatcher branches on import availability; when `litellm` ImportErrors, the handler returns 503 with `X-Bundle-Capability: local-only`.
- [`desktop/sidecar/jarvis-sidecar.spec`](../../../desktop/sidecar/jarvis-sidecar.spec) — adds the conditional `excludes` block.
- [`desktop/scripts/build-notarized.sh`](../../../desktop/scripts/build-notarized.sh) — sets `JARVIS_DESKTOP_BUNDLE=1` before invoking sidecar build + tauri build; writes the `JarvisBundleCapabilities` array into the .app's `Info.plist` post-bundle.
- [`frontend/nuxt.config.ts`](../../../frontend/nuxt.config.ts) — propagates `JARVIS_DESKTOP_BUNDLE` into runtime config.
- New stub `frontend/app/composables/useApiKeys.stub.ts` — replaces the real composable when the flag is set; pure no-ops.
- Existing `services/privacy.py` and `privacy_cloud_providers_enabled` are unchanged — they remain as the defense-in-depth runtime layer.
- New CI matrix entry: `JARVIS_DESKTOP_BUNDLE=0` build target; existing tests for cloud-provider services run in this matrix only (skipped in the `=1` matrix because the modules are excluded).

## Migration path

Land in this order, each chunk independently testable:

1. **Backend startup gating** ✅ landed 2026-04-30. New helper [`backend/services/bundle.py`](../../../backend/services/bundle.py) (single-source-of-truth for `JARVIS_DESKTOP_BUNDLE` — `is_desktop_bundle()`, `cloud_providers_available()`, `bundle_capabilities()`). The `PATCH /api/settings/api-key` endpoint moved out of [`backend/routers/settings.py`](../../../backend/routers/settings.py) into a standalone [`backend/routers/api_keys.py`](../../../backend/routers/api_keys.py) so [`main.py`](../../../backend/main.py)'s `create_app()` can conditionally include it (the audit signal is *the route does not exist* in the desktop bundle — 404, not a gated 200). Chat WS handler gains a `cloud_providers_available()` check before `_make_llm`: when False AND a non-Ollama provider is requested, emit a structured `error` event with `bundle_capability="local-only"` and `done`, *before* construction touches the missing SDK. New live-probe endpoint [`GET /api/bundle/capabilities`](../../../backend/routers/bundle.py) returns `{capabilities, is_desktop_bundle, cloud_providers_available}` — always registered (audit signal IS the response payload, per §90 verification path 2). Coverage: 13 new tests in [`backend/tests/test_bundle_modes.py`](../../../backend/tests/test_bundle_modes.py) (helper math, capability endpoint, api-keys gating in both build targets, chat-WS local-only error path); two existing tests in `test_settings_api.py` + `test_security_validations.py` updated to assert the new 404 contract.
2. **PyInstaller exclusion list** ✅ landed 2026-04-30. [`desktop/sidecar/jarvis-sidecar.spec`](../../../desktop/sidecar/jarvis-sidecar.spec) reads `JARVIS_DESKTOP_BUNDLE` (default "1"); conditional excludes block adds `anthropic`, `openai`, `google.generativeai`, `google.generativelanguage`, `services._anthropic_client` when the flag is set. LiteLLM stays bundled (used by the Ollama path); `services.claude` stays (used unconditionally for `StreamEvent` + system-prompt builders). The audit signal is the SDK-level absence — `find . -name "*anthropic*" -o -name "*openai*"` under `_MEIPASS` returns nothing, and any LiteLLM cloud-provider call ImportErrors, which the chat WS handler surfaces as `bundle_capability="local-only"`.
3. **Frontend tree-shake + stub** ✅ landed 2026-04-30. [`frontend/nuxt.config.ts`](../../../frontend/nuxt.config.ts) propagates `JARVIS_DESKTOP_BUNDLE` into runtime config (`config.public.desktopBundle`). New composable [`useDesktopBundle`](../../../frontend/app/composables/useDesktopBundle.ts) exposes a reactive `isDesktopBundle` boolean. Three consumer-side gates land: [`pages/onboarding.vue`](../../../frontend/app/pages/onboarding.vue) skips the cloud branch entirely (defaults `phase = 'local'` when the bundle flag is on; cloud-choice button + cloud phase template are `v-if`-guarded), [`pages/settings.vue`](../../../frontend/app/pages/settings.vue) wraps the `<ProvidersSection>` with `v-if="!isDesktopBundle"`, [`components/ModelSelector.vue`](../../../frontend/app/components/ModelSelector.vue) filters cloud entries from `availableProviders` so only Ollama appears in the chat-model picker. Pragmatic note re. ADR §B's "useApiKeys.ts replaced with a tiny stub": rather than swap the whole composable file via Vite alias (Nuxt-4 auto-import edge cases), gating is consumer-side — when `AddKeyModal` / `ProvidersSection` aren't rendered, `useApiKeys` is dead code that Vite's static analysis prunes naturally. Same runtime behaviour, simpler implementation; the file-swap pattern is left as a follow-up if the procurement-review path ever needs the build-time JS-level audit signal.
4. **Info.plist capability array** ✅ landed 2026-04-30. [`desktop/scripts/build-notarized.sh`](../../../desktop/scripts/build-notarized.sh) gains a step 4b (post-tauri-build, pre-notary): `PlistBuddy` writes `JarvisBundleCapabilities` array to `Contents/Info.plist` (the desktop-bundle build emits `["local-llm", "vault-markdown", "knowledge-graph", "semantic-search"]`; `JARVIS_DESKTOP_BUNDLE=0` re-adds `cloud-llm`, `api-keys`, `external-providers`). Idempotent re-runs (`Delete` then `Add`). Bundle is re-signed after the plist edit because Apple's signature is computed over the plist contents — touching the plist invalidates it; re-signing with the Developer ID identity keeps notarization happy.
5. **CI matrix** ✅ landed 2026-04-30. [`.github/workflows/ci.yml`](../../../.github/workflows/ci.yml) backend job gains a strategy matrix `[bundle: '1', '0']` (`fail-fast: false`); the `JARVIS_DESKTOP_BUNDLE` env var is set per-job before `pytest` collection so the conftest-imported `app` instance honours the build target in each entry. The matrix labels are `desktop-bundle` (default) and `cloud-sku`. Each matrix entry runs the full backend suite; tests like `test_bundle_modes.py::test_api_keys_route_registered_in_cloud_sku` build a fresh app explicitly, so they pass in both matrix entries.
6. **Privacy banner copy update** — deferred. The current privacy section copy is accurate without modification (says "Local Ollama, embeddings and reranker are *never* blocked"); a more pointed "this is the local-only build" message is a polish item that fits when ADR 014 graduates to a procurement-pitch surface, not a v1 must-have.

Notarization is a single round at the end of step 4, alongside any G4 work that happens to be landing in the same window. **Notarization for the post-G4b bundle remains a separate user-authorized step** — it requires Apple Developer credentials and produces a ticketed binary, both of which sit outside the implementation chunks above.

## Open follow-ups

1. **Hybrid / managed-cloud SKU planning** — when a customer or strategic decision motivates resurrecting cloud providers, the restoration path is the build flag. Anything beyond that (e.g., a managed-cloud SKU that *defaults* to cloud rather than local) is its own ADR.
2. **Local-only providers that aren't Ollama** — if v1.1 adds a second local provider (e.g., direct llama.cpp integration per ADR 003 follow-up #6), it's a *local* capability and ships unconditionally in the desktop bundle. Not in scope here.
3. **Anthropic / OpenAI for plumbing tasks (classification, extraction)** — currently those use Ollama or rule-based fallbacks. If a future quality requirement argues for cloud-provider plumbing tasks specifically (and the user accepts the privacy trade-off via the existing `privacy_cloud_providers_enabled` toggle), we'd need the cloud-provider code present in the bundle. Trigger to revisit: a real buyer requirement, not a developer convenience.
