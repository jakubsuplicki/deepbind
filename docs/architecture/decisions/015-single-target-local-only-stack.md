# ADR 015 — Single-target local-only stack (drop LiteLLM, drop cloud SDKs, drop duel)

**Status:** Accepted
**Date:** 2026-04-30
**Related:** [ADR 002](002-pure-local-product-shape.md), [ADR 003](003-desktop-distribution-tauri-and-sidecars.md), [ADR 005](005-hardware-tiered-model-stack-and-first-run-policy.md), [ADR 014](014-desktop-bundle-excludes-cloud-providers.md)
**Supersedes:** [ADR 014](014-desktop-bundle-excludes-cloud-providers.md) — the build-flag-gated dual-target shape is collapsed into a single local-only target. ADR 014's audit-signal logic is preserved in spirit (cloud capabilities structurally absent) but moved up a layer (no cloud code in the repo, not just no cloud code in the bundle).

## Context

[ADR 014](014-desktop-bundle-excludes-cloud-providers.md) split the build into two targets — a desktop bundle that structurally excludes cloud-provider SDKs (`anthropic`, `openai`, `google.generativeai`) and a cloud-SKU target that keeps them. The split was gated by a `JARVIS_DESKTOP_BUNDLE` env var consumed by:

- `desktop/sidecar/jarvis-sidecar.spec` (PyInstaller `excludes`)
- `backend/services/bundle.py` (capability advertising + route gating)
- `frontend/nuxt.config.ts` runtime config (frontend tree-shake)
- `.github/workflows/ci.yml` (matrix CI strategy)

ADR 014's amendment narrowed the PyInstaller exclude list to the cloud SDKs proper, **keeping LiteLLM bundled** because LiteLLM dispatches to both cloud and Ollama — the local path needs it.

That amendment is unsound. LiteLLM's `__init__.py` imports `openai` at its own module-top level. With `openai` excluded from the bundle, `import litellm` raises `ModuleNotFoundError: No module named 'openai'` — empirically verified by booting the bundled sidecar 2026-04-30 (sidecar smoke test failed with this exact error after the build pipeline reached the boot probe).

Three options surfaced to resolve the conflict:

- **A.** Restore the SDKs to the bundle. Audit signal becomes structural (Info.plist + 404 routes + frontend) rather than literal (no SDK code on disk). LiteLLM works.
- **B.** Stub the SDKs with shim modules that satisfy LiteLLM's import surface. Preserves "no real SDK code" audit signal. Maintenance cost is ongoing — LiteLLM imports a wide surface from `openai`.
- **C.** Drop LiteLLM. Replace with direct Ollama HTTP dispatch. The bundle has zero cloud-SDK code, including transitively, because nothing in the repo asks for it.

Option A is what ADR 014 would degrade to under engineering pressure. Option B is fragile and offers no real upside over A. Option C is the only option whose runtime artifact actually matches the v1 product story ([ADR 002](002-pure-local-product-shape.md): "100% per-laptop, zero outbound calls by default").

A second observation, surfaced while scoping the rework: the **duel / council feature** ([`backend/services/council.py`](../../../backend/services/council.py), 623 lines; [`frontend/app/components/DuelSetup.vue`](../../../frontend/app/components/DuelSetup.vue); [`frontend/app/composables/useDuel.ts`](../../../frontend/app/composables/useDuel.ts); WS `duel_start` handler) is a pre-pivot artifact from when the product was multi-cloud-provider. With local-only:

- Loading two models simultaneously violates the [G4b4 memory pressure mechanism](005-hardware-tiered-model-stack-and-first-run-policy.md): both models compete for the same RAM headroom, defeating the downgrade ladder we just landed.
- Sequential duel (load A → answer → unload → load B → answer) costs 10–60s per swap on first hit and discards Ollama's keep-alive cache.
- Conceptual value drops: cross-provider comparison was the point. Two Qwen3 variants share the same family, training corpus, and tendencies — the "second opinion" is barely a second opinion.

The duel feature was viable when there were two cloud providers with genuinely different blind spots. It is not viable as a v1 feature in a single-provider local-only stack.

## Decision drivers

1. **The bundle's contents must match the product story.** ADR 002 says "your data never leaves your laptop." A bundle whose PyInstaller payload includes `anthropic.AsyncAnthropic` makes that claim defensible only by call-graph reasoning, not by inspection. The v1 product's audit story — for the buyers ADR 002 actually serves — requires the literal property, not the structural one.
2. **Single-target builds eliminate an entire class of bug.** ADR 014's failure mode (a transitive dependency surviving the exclude list) is the kind of bug that re-surfaces at every dependency bump. Single-target eliminates it permanently.
3. **LiteLLM's value disappears in a single-provider stack.** LiteLLM's selling point is provider-agnosticism. With Ollama as the sole dispatch target, we pay for an abstraction we no longer need.
4. **Duel was multi-cloud differentiation. It is not v1 product surface.** Removing it does not subtract from v1 because it was never wired into the production UX in the first place (no page or composable references it).
5. **The rework cost is bounded and front-loadable.** The largest single piece is the LiteLLM-to-Ollama dispatcher rewrite (~200–300 lines new code with tests). Everything else is mechanical deletion. The work has a clean ordering: design → implement → swap → delete.

## Decision

**V1 ships a single-target build with no cloud-provider code in the repository.** Specifically:

### A. No cloud-provider SDKs in the codebase
- `anthropic` and `openai` Python packages removed from `backend/requirements.txt` (and any transitive expectation of them).
- All `import anthropic` / `import openai` references — and modules that depended on them — deleted from `backend/services/`, `backend/routers/`, `backend/models/`.

### B. No LiteLLM
- `litellm` removed from `backend/requirements.txt`.
- A new module [`backend/services/ollama_dispatcher.py`](../../../backend/services/ollama_dispatcher.py) replaces the LiteLLM dispatch path in `services/llm_service.py`.
- The dispatcher uses the **official `ollama` Python package** (Apache 2.0, maintained upstream by Ollama, ~15 KB wheel). Audit-clean: every transitive dependency (`httpx`, `httpcore`, `h11`, `pydantic`, `pydantic-core`, `annotated-types`, `typing-extensions`, `typing-inspection`, `anyio`, `idna`, `certifi`) is already in our requirements via FastAPI/httpx — net new is just the `ollama` wheel itself, no cloud SDKs anywhere in the tree.
- The dispatcher's responsibility is narrow: adapter from `ollama.AsyncClient.chat(stream=True)` events → our existing `StreamEvent` shape. Roughly 80 lines of pure mapping logic plus Anthropic-style ↔ Ollama-style message converter.
- Token counting via `tiktoken` (already bundled). The official client's `prompt_eval_count` / `eval_count` fields on the final chunk are the authoritative post-hoc counts; `tiktoken` remains for prompt-budgeting predictions.
- Exception types are the official client's (`ollama.RequestError`, `ollama.ResponseError`) plus `httpx.TimeoutException` / `httpx.ConnectError` for transport failures — all mapped into `StreamEvent(type="error", content=...)` at the adapter boundary.
- **Tool-call IDs synthesized locally.** Ollama's wire format does not carry tool-call ids (only `function.name` + `function.arguments`). The dispatcher synthesizes a stable id per tool call at emit time (UUID-derived) so downstream `tool_use` ↔ `tool_result` correlation remains intact. The Anthropic→Ollama message converter consumes these ids when round-tripping `tool_result` blocks back as `{role: "tool", tool_name: ..., content: ...}` messages.

**Why the official client over a hand-rolled httpx wrapper:** the streaming JSON-lines parsing, tool-call chunk handling, and connection lifecycle are upstream-tested code that tracks the Ollama server we're already pinned to. Hand-rolling adds ~150 lines of code we'd own and chase against future Ollama versions, with no audit-signal benefit (both options have zero cloud-SDK transitive deps). The official client is also Apache 2.0, satisfying [ADR 002](002-pure-local-product-shape.md) §4's strict-OSI policy.

### C. No multi-provider machinery
- No "provider" abstraction anywhere — chat dispatch always targets Ollama.
- Specialists drop their provider field; default to the conversation-pinned chat model ([ADR 008](008-conversation-pinned-chat-model.md) where applicable).
- `services/privacy.py` drops provider gates; what remains is the offline-mode entitlement check.

### D. No duel / council
- [`backend/services/council.py`](../../../backend/services/council.py) deleted.
- WS `duel_start` handler in [`backend/routers/chat.py`](../../../backend/routers/chat.py) deleted.
- [`frontend/app/components/DuelSetup.vue`](../../../frontend/app/components/DuelSetup.vue) and [`frontend/app/composables/useDuel.ts`](../../../frontend/app/composables/useDuel.ts) deleted.
- Any specialist or chat surface referencing duel deleted.
- Feature is **deferred indefinitely**, recoverable from the pre-deletion git branch if a future product reason surfaces (e.g., small fast model auditing a large slow one for hallucinations — that would be a different feature with a different name).

### E. No `JARVIS_DESKTOP_BUNDLE` flag
- The flag, the [`backend/services/bundle.py`](../../../backend/services/bundle.py) helper module, the [`frontend/app/composables/useDesktopBundle.ts`](../../../frontend/app/composables/useDesktopBundle.ts) composable, and the CI matrix all collapse to a single target.
- [`desktop/sidecar/jarvis-sidecar.spec`](../../../desktop/sidecar/jarvis-sidecar.spec) excludes block deleted (no SDKs in the install means nothing to exclude).
- `desktop/scripts/build-notarized.sh` keeps the `JarvisBundleCapabilities` Info.plist write but with a static value (the capability list no longer varies per build).
- `.github/workflows/ci.yml` matrix collapses to a single backend job.

### F. Audit signal — restated
The desktop bundle's "no cloud calls" claim is now defensible four ways:
1. **Source repo** — `grep -r anthropic backend/ frontend/` returns nothing in v1 src files. The capability is structurally absent at the code level, not just at the bundle level.
2. **Bundle contents** — `find DeepFilesAI.app -name "*anthropic*" -o -name "*openai*"` returns nothing.
3. **Routes** — `curl /api/settings/api-key` returns 404 (route does not exist).
4. **Runtime egress** — `tcpdump host api.anthropic.com or host api.openai.com` shows zero traffic during normal use.

This is materially stronger than ADR 014's structural-only signal: a static-analysis tool examining the repo cannot find a code path to the cloud APIs because none exists.

## Restoration path

If a future enterprise customer requires a multi-provider build:
- Restoration is a feature add against a pure-local v1, not a flag flip on a dual-build v1.
- The duel feature, the LiteLLM dispatcher, and the cloud-SDK paths all exist in the pre-deletion git branch (preserved at the commit immediately before this ADR's chunk 1 lands). Recovery is a cherry-pick + rebase exercise, not a fresh implementation.
- Any restoration would itself be ADR-worthy — the v1.5+ product team would document why the buyer profile changed.

## Audit verification

Same shape as ADR 014's audit-verifiability section, with the literal signals upgraded:

| Question a buyer asks | How to verify | Answer in v1 |
|---|---|---|
| Does the bundle phone Anthropic / OpenAI? | `tcpdump -i any host api.anthropic.com or host api.openai.com` for an hour of normal use | Zero traffic |
| Is cloud SDK code in the binary? | `find DeepFilesAI.app -name "*anthropic*" -o -name "*openai*" -o -name "*litellm*"` | Returns nothing |
| Is cloud SDK code even in the *source repo*? | `grep -r 'import anthropic\|import openai\|import litellm' backend/ frontend/` | Returns nothing in v1 src files |
| Can a user enter an API key? | Settings UI; `curl /api/settings/api-key` | No UI; route returns 404 |
| Does the manifest declare cloud capability? | `defaults read DeepFilesAI.app/Contents/Info.plist JarvisBundleCapabilities` | `["local-llm", "vault-markdown", "knowledge-graph", "semantic-search"]` |

## Alternatives considered

### A. Restore SDKs to the bundle (ADR 014 retreat)
The simplest fix to the build crash. Keeps LiteLLM. Requires swapping the `cloud_providers_available()` gate to `is_desktop_bundle()` in `routers/chat.py`. Audit signal weakens to structural-only.

**Rejected** because the audit signal is the product. v1's buyer profile (compliance-led firms, per ADR 002) reads the bundle. "We have the SDKs but they're unreachable" is a sentence that loses procurement reviews. The runtime claim ("no calls happen") is true under both options; the difference is whether a buyer has to take our word for it or can verify it locally with `find`.

### B. Stub `openai` / `anthropic` with shim modules
Keeps LiteLLM. Provides empty-shell `openai`/`anthropic` packages whose top-level imports satisfy LiteLLM's expectations and whose attribute accesses raise on actual use.

**Rejected** because LiteLLM imports a wide surface from `openai` — `OpenAI`, `AzureOpenAI`, error types, types, helpers. The shim has to track LiteLLM's import surface across version bumps. Ongoing maintenance for a feature we don't want.

### C. Keep cloud code in repo, drop only LiteLLM
Removes LiteLLM, replaces with direct Ollama. Keeps `services/claude.py` and the cloud-dispatch branches in `routers/chat.py` for a hypothetical future cloud-SKU build.

**Rejected** because the cloud-SKU build is hypothetical. Half-decisions accumulate cost: the `JARVIS_DESKTOP_BUNDLE` flag stays, the CI matrix stays, the privacy gates stay, the API-key router stays — each one a place where a future bug can hide. If the cloud SKU is ever built, it gets built fresh against the new architecture.

## Consequences

### Positive
- The bundle's contents match the v1 product story.
- Single CI target.
- LiteLLM's transitive dependency surface (~30 packages) leaves the bundle.
- Eliminates the `services/claude.py` system-prompt + `LLMService` LiteLLM-call duplication; one dispatch path.
- Memory pressure mechanism ([G4b4](005-hardware-tiered-model-stack-and-first-run-policy.md)) becomes the only model-selection path — no need to reconcile its decisions with provider-level routing.
- Future ADR 006 license-activation UX has a simpler architecture to integrate with.

### Negative
- Duel feature gone from v1.
- Cloud-SKU build target gone from v1 — restoration is a v1.5+ project.
- Token-cost tracking (`services/token_tracking.py`'s `litellm.completion_cost` path) loses meaning for local-only; cost-tracking simplifies or goes away. Existing per-conversation token counts (via `tiktoken`) survive; per-call dollar estimates do not.
- New `services/ollama_dispatcher.py` is a maintenance surface we own (vs. depending on LiteLLM upstream). Real cost: streaming, tool-call decoding, error mapping all become our problem.

### What this changes about existing code

| Path | Action | Notes |
|---|---|---|
| `backend/services/claude.py` (589 lines) | Split: rescue ~110 lines (`StreamEvent`, `build_system_prompt`, `build_system_prompt_with_stats`, `_SYSTEM_PROMPT_BUDGET_FRACTION`) into `backend/services/system_prompt.py`; delete the rest | The `ClaudeService` class, `_iter_stream`, `_ToolAccumulator`, all anthropic-specific code goes |
| `backend/services/_anthropic_client.py` (2 lines) | Delete | Empty stub |
| `backend/services/llm_service.py` (394 lines) | Replace with new `backend/services/ollama_dispatcher.py` | Direct Ollama HTTP, native streaming, native tool-call decode |
| `backend/services/token_tracking.py` (231 lines) | Delete `litellm.completion_cost` path; tighten to local-only token counts via `tiktoken` | Net ~100 lines deleted |
| `backend/services/privacy.py` (159 lines) | Drop provider gates | Net ~80–100 lines deleted; offline-mode entitlement check survives |
| `backend/services/council.py` (623 lines) | Delete | Recoverable from pre-deletion branch |
| `backend/services/document_classifier.py` (236 lines, partial) | Delete `classify_section_llm` cloud branch (~50 lines); rule-based path remains | Section-classification quality may regress slightly; revisit if measurable in eval set |
| `backend/services/bundle.py` (~80 lines) | Delete | Capability advertising machinery no longer varies |
| `backend/routers/chat.py` (~50 line edit) | `_make_llm` collapses to single Ollama path; duel WS branch deleted; `cloud_providers_available()` gate deleted | The pressure-ladder integration ([G4b4](005-hardware-tiered-model-stack-and-first-run-policy.md)) is unaffected |
| `backend/routers/api_keys.py` (40 lines) | Delete | Route 404 becomes "route never existed" |
| `backend/main.py` | Drop conditional `api_keys_router` registration; drop `bundle_router` | Single startup path |
| `backend/tests/test_*.py` | ~25 of 98 affected; most either delete with the feature or simplify | `test_bundle_modes.py` deletes entirely (no flag to test); cloud-path chat tests delete; duel tests delete |
| `frontend/app/composables/useApiKeys.ts` | Delete | |
| `frontend/app/composables/useDesktopBundle.ts` | Delete | |
| `frontend/app/composables/useDuel.ts` | Delete | |
| `frontend/app/components/AddKeyModal.vue` | Delete | |
| `frontend/app/components/settings/ProvidersSection.vue` | Delete | |
| `frontend/app/components/DuelSetup.vue` | Delete | |
| `frontend/app/components/ModelSelector.vue` | Simplify — drop the `availableProviders` filter; only Ollama remains | |
| `frontend/app/pages/onboarding.vue` | Drop cloud branch; `phase` always `'local'` | |
| `frontend/app/pages/settings.vue` | Drop ProvidersSection; drop `useDesktopBundle` import | |
| `frontend/app/pages/main.vue` | Drop provider switching UI | |
| `frontend/app/components/StatusBar.vue` | Drop provider status | |
| `frontend/app/components/ChatPanel.vue` | Drop provider state | |
| `frontend/app/components/SpecialistWizard.vue`, `SpecialistCard.vue` | Drop provider field on specialists | |
| `frontend/nuxt.config.ts` | Drop `desktopBundle` runtime config | |
| `desktop/sidecar/jarvis-sidecar.spec` | Drop conditional excludes block — single-shape spec | |
| `desktop/scripts/build-notarized.sh` | Static `JarvisBundleCapabilities` plist value | |
| `.github/workflows/ci.yml` | Collapse matrix to single backend job | |
| `docs/architecture/decisions/014-*.md` | Status → "Superseded by ADR 015" | Keep file for history |
| `docs/.registry.json` | Drop `bundle-capabilities` and `api-key-management` features; update `local-models` source list | |

## Migration path

The work is sequenced so the test suite passes after each chunk and the bundle is buildable at the end of each chunk that touches build/dispatch surface.

### Chunk 1 — Pre-deletion preservation (user)
- Commit current state (post-ADR 014 implementation, post-ADR 015 draft).
- Branch: `archive/v1-pre-adr-015` (or similar) — preserves the duel feature, LiteLLM dispatcher, cloud-SDK code, ADR 014 dual-target build for restoration.
- Continue work on `main` (or a dedicated `adr-015` branch that merges back).

### Chunk 2 — New Ollama dispatcher (backend)
- Add `ollama` to [`backend/requirements.txt`](../../../backend/requirements.txt). Net new transitive deps: zero (the package's deps are already pulled in by FastAPI/httpx).
- Write [`backend/services/ollama_dispatcher.py`](../../../backend/services/ollama_dispatcher.py) — adapter from `ollama.AsyncClient.chat(stream=True)` events → `StreamEvent` shape. Includes Anthropic-style ↔ Ollama-style message converter, tool-call id synthesis, error mapping. ~80 lines of mapping logic plus ~80 lines of message converter.
- Write [`backend/tests/test_ollama_dispatcher.py`](../../../backend/tests/test_ollama_dispatcher.py) — unit tests against a mocked `ollama.AsyncClient` (mock yields a sequence of `ChatResponse`-shaped objects; the adapter is the only thing under test).
- Both `LLMService` (LiteLLM) and `OllamaDispatcher` (new) coexist briefly so chunk 3 can swap.

### Chunk 3 — Wire dispatcher into chat router
- Update [`backend/routers/chat.py`](../../../backend/routers/chat.py)'s `_make_llm` to construct `OllamaDispatcher` instead of `LLMService`.
- Drop the `cloud_providers_available()` / `is_desktop_bundle()` gate (no cloud branch exists to gate).
- Drop the `provider == "anthropic"` / `ClaudeService` branch.
- Run full backend suite — pressure-ladder tests should pass unchanged because they mock `_make_llm`.

### Chunk 4 — Delete cloud + LiteLLM surface (backend)
- Delete `services/claude.py` (after rescuing system-prompt + StreamEvent to `services/system_prompt.py`).
- Delete `services/_anthropic_client.py`.
- Delete `services/llm_service.py`.
- Delete `services/council.py`.
- Delete `services/bundle.py`.
- Delete `routers/api_keys.py`.
- Update `main.py` — drop conditional router registrations.
- Update `services/privacy.py` — drop provider gates.
- Update `services/token_tracking.py` — drop `litellm.completion_cost`.
- Update `services/document_classifier.py` — drop `classify_section_llm` cloud branch.
- Drop `litellm`, `anthropic`, `openai` from `backend/requirements.txt`.
- Run full backend suite — expect 25 test files affected; resolve each.

### Chunk 5 — Delete cloud + duel surface (frontend)
- Delete: `useApiKeys.ts`, `useDesktopBundle.ts`, `useDuel.ts`, `AddKeyModal.vue`, `ProvidersSection.vue`, `DuelSetup.vue`.
- Simplify: `ModelSelector.vue`, `onboarding.vue`, `settings.vue`, `main.vue`, `StatusBar.vue`, `ChatPanel.vue`, `SpecialistWizard.vue`, `SpecialistCard.vue`.
- Drop `desktopBundle` from `nuxt.config.ts`.
- `nuxt build` should pass with all routes prerendering.

### Chunk 6 — Drop build flag surface
- Remove `JARVIS_DESKTOP_BUNDLE` references from `desktop/sidecar/jarvis-sidecar.spec` (delete excludes block).
- Remove env var from `desktop/scripts/build-notarized.sh` (capability list becomes static).
- Remove matrix from `.github/workflows/ci.yml` (single backend job).

### Chunk 7 — Re-attempt notarized bundle build
- `bash desktop/scripts/build-notarized.sh`.
- Sidecar smoke-test now boots cleanly (no LiteLLM, no `import openai` chain).
- Notarize + staple `.app` and `.dmg`.
- Run `find DeepFilesAI.app -name "*anthropic*" -o -name "*openai*" -o -name "*litellm*"` — expected: empty output.
- Run `tcpdump host api.anthropic.com or host api.openai.com` while exercising the app — expected: zero traffic.

### Chunk 8 — Documentation
- Mark ADR 014 superseded.
- Update [`docs/.registry.json`](../../.registry.json) — drop `bundle-capabilities`, `api-key-management`; update `local-models` source list to reference the new dispatcher.
- Update [`docs/features/local-models.md`](../../features/local-models.md) — drop multi-provider mentions; describe the Ollama dispatcher.
- Update [`docs/features/desktop-shell-graduation.md`](../../features/desktop-shell-graduation.md) — note the single-target shift.
- Update [`docs/overview.md`](../../overview.md) if it still mentions multi-provider.
- Write [`docs/runbooks/release-build-macos.md`](../../runbooks/release-build-macos.md) (the runbook the user requested, now writable against the simpler single-target build).

## Open questions

- **Section classification quality.** Dropping `classify_section_llm`'s cloud branch may regress section-classification accuracy in the ingest pipeline. The rule-based fallback exists but has not been measured against the eval set with the cloud branch removed. If the regression is material, the local-Ollama-call replacement is a chunk-4 follow-up.
- **Token-cost UI surface.** Any frontend surface showing per-call dollar costs needs to either disappear or change to "rough token count" — depends on what the UI currently shows. To be cataloged in chunk 5.
- **`tiktoken` for non-OpenAI tokenizers.** `tiktoken` is OpenAI's BPE; it's a reasonable approximation for Qwen3's tokenizer but not exact. If exactness matters for prompt-budgeting accuracy, swap to a Qwen-family-aware tokenizer (which is itself bundleable). Probably acceptable approximation for v1.
