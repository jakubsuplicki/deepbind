# ADR 012 — Install-time chat-model self-test

**Status:** Accepted — production wiring landed (2026-04-29)
**Date:** 2026-04-28
**Related:** [ADR 010](010-conversation-replay-eval-harness.md) · [ADR 011](011-latency-benchmark-harness.md) · [ADR 003](003-desktop-distribution-tauri-and-sidecars.md) · [`docs/features/chat-model-probe.md`](../../features/chat-model-probe.md) · [`docs/features/local-models.md`](../../features/local-models.md)

## Context

The product ships as a self-installing desktop app to many users on many environments — Apple Silicon generations M1 through M5+, Pro/Max/Ultra variants, macOS versions ranging from 14 to 26+, Linux + various GPU vendors, Windows + various GPUs, RAM tiers from 8 GB to 64+ GB. The "v1 canonical chat model" decisions made for the eval harnesses ([ADR 010](010-conversation-replay-eval-harness.md), [ADR 011](011-latency-benchmark-harness.md)) implicitly pinned a *single* model as the answer for *all* environments. That doesn't hold up under contact with reality.

Three distinct per-environment failure modes have been demonstrated:

1. **Correctness varies.** Qwen3-30B-A3B with `think: false` works correctly on some Ollama+OS combinations and emits chain-of-thought instead of an answer on others (specifically: Ollama 0.18.0 on macOS 26 + Apple M5 — see [ADR 010 Issue 4](010-conversation-replay-eval-harness.md#issue-4-2026-04-28-evening--qwen3-30b-a3b-thinkfalse-leak-canonical-chat-model-swap-to-qwen3-14b)). A user on macOS 14 + Ollama 0.21.x may see correct behavior; a user on the same M5+macOS-26 combination as the dev machine sees broken UX.
2. **Hardware fit varies.** A 30B-class model fits comfortably on M3 Max 64 GB, marginally on M5 Pro 24 GB (1 GB headroom — catastrophic), and not at all on a 16 GB MacBook Air.
3. **Speed thresholds vary.** What's "fast enough to displace shadow ChatGPT" depends on hardware. A model that hits 5 TPS on a 16 GB integrated-GPU laptop is unusable; the same model at 25 TPS on an M4 Max is great.

The current static "the canonical chat model is X" approach is the same trap [ADR 010 Issue 1](010-conversation-replay-eval-harness.md) was filed to escape — picking a global answer for a per-environment question. The fix is to **measure on the customer's machine, then pick**.

## Decision drivers

1. **Per-environment correctness is non-negotiable.** A buyer who installs the product and sees the model "thinking out loud" before answering will not give us a second chance. The probe must catch this before the user sees the chat path.
2. **No phone-home for measurement.** [ADR 002](002-pure-local-product-shape.md) commits to zero outbound calls by default. The probe runs entirely on the customer's machine; no telemetry, no aggregate-uploaded benchmarks, no vendor-side database of "what works where."
3. **Reuse the existing measurement substrate.** [ADR 011](011-latency-benchmark-harness.md)'s `scenarios.py` + `harness.py` already produce the right numbers. The probe is the *runtime* invocation of the same code; building a separate measurement stack would create drift between dev numbers and customer numbers.
4. **Bounded wall-clock cost.** The probe runs at install / on demand, not on every chat turn. Acceptable budget: ~30–60 seconds end-to-end for a "first-launch model selection" UX. That's enough for one warm-up + a small speed sample per candidate model, against a candidate set bounded by hardware-fit pre-filtering.
5. **Deterministic, falsifiable.** Same machine + same Ollama version + same models → same recommendation. The probe must produce a stable output the user can verify and override.
6. **Customer-overridable.** Some users will want to force a specific model regardless of probe verdict (research use, preference for capability over speed, hardware that fluctuates). The probe recommends; the user decides.

## Decision

Add a service at [`backend/services/chat_model_probe.py`](../../../backend/services/chat_model_probe.py) that runs three probes against each candidate model and returns a ranked recommendation. Probes return falsifiable booleans / numeric thresholds; the orchestrator picks the largest model that passes all three.

### Probe 1 — Correctness

Send the canonical "say hi in one word" prompt with `think: false` and `num_predict: 8` to the candidate model. Regex-check the response against a panel of thinking-prose patterns:

```
^(Okay|Let me|First|The user|Hmm|Wait|So[, ])
```

Match → the model is leaking chain-of-thought on this Ollama version → mark **fail**. No match (model produced "Hi.", "Hello!", or similar) → **pass**. The 8-token cap is intentional: a clean model produces the answer well within 8 tokens; a leaking model hasn't reached the answer yet by token 8.

### Probe 2 — Hardware fit

Reuse [`probe_runtime_load()`](../../../backend/services/ollama_service.py) (existing) and [`effective_footprint_bytes(entry, default_ctx)`](../../../backend/services/ollama_service.py) (existing). The probe passes if:

```
weights + bytes_per_kv_token × default_ctx_tokens ≤ 0.80 × available_ram
```

The 80% threshold leaves headroom for OS overhead, the user's other apps, and growing KV cache during long conversations. This is the predicate the future memory-pressure auto-downgrade will use too.

### Probe 3 — Speed

Run two scenarios from [`tests/eval/latency/scenarios.py`](../../../backend/tests/eval/latency/scenarios.py) — `warm_short()` and `chat_realistic()` — at 1 timed run each (no warm-up). Pass thresholds:

- `warm-short` total ≤ 1500 ms
- `chat-realistic-shallow` TPS ≥ 8

These numbers are calibrated against the Cmd-Tab-to-ChatGPT comparison: anything under 1.5 s for short chat is competitive; anything under 8 TPS on realistic chat fails the "feels snappy" test. Thresholds are tunable via config; the rationale lives next to the constants.

### Selection orchestrator

```
candidates = sort_by_capability_desc(user_pickable_catalog_entries)
for entry in candidates:
    if not probe_hardware_fit(entry):    # cheap, runs first
        continue
    if not probe_correctness(entry, ollama):  # ~5–10 sec per candidate
        continue
    speed = probe_speed(entry, ollama)         # ~10–20 sec per candidate
    if not speed.passes_thresholds():
        continue
    return entry  # first passing candidate wins
return SAFE_FALLBACK  # smallest catalog entry; last resort
```

Hardware-fit pre-filtering keeps the wall-clock budget bounded — we don't run correctness or speed probes on a 30B-class model when only 16 GB of RAM is available. Correctness runs before speed because a broken model has misleading speed numbers. Speed runs last because it's the most expensive probe.

### Persistence

The recommendation is persisted to `app/config.json` under a new key:

```jsonc
{
  "chat_model_probe": {
    "schema_version": 1,
    "timestamp_utc": "2026-04-28T...",
    "ollama_version": "0.18.0",
    "platform": "darwin-arm64",
    "ram_gb": 24,
    "recommended_model": "qwen3:14b",
    "candidates_evaluated": [
      {"model": "qwen3:30b-a3b-instruct-2507", "verdict": "fail_hardware_fit"},
      {"model": "qwen3:30b-a3b", "verdict": "fail_correctness", "evidence": "Okay, the user asked..."},
      {"model": "qwen3:14b", "verdict": "pass", "warm_short_ms": 299, "realistic_tps": 14.0}
    ],
    "user_override": null
  }
}
```

The `user_override` field lets a user force a different model regardless of probe verdict. The orchestrator respects override unconditionally; the recommendation is informational when override is set.

### Re-run triggers

The probe re-runs (and overwrites the persisted recommendation) when:

- The user explicitly invokes "re-test chat models" from settings.
- Ollama version changes (detected via cached `ollama_version` mismatch).
- macOS major version changes (detected via `platform.mac_ver()` cached comparison).
- A new model is added to the user-pickable catalog.

Hardware doesn't change between launches in practice; if it does (RAM upgrade, GPU added on Linux), the user re-runs manually.

### Settings surface

A new section in [`frontend/app/components/settings/LocalModelsSection.vue`](../../../frontend/app/components/settings/LocalModelsSection.vue) shows the probe verdict, evidence, and an "override / re-run" button. UI is deferred to the chunk that wires this into onboarding; the backend service lands first so the probe can be invoked programmatically while the UI catches up.

## Alternatives considered

### A. Hard-code by hardware tier (8 GB → 8B, 16 GB → 14B, 24 GB+ → 30B-A3B)
Rejected. Doesn't catch the correctness problem (the 30B-A3B leak is a software-environment issue, not a hardware one). Same trap [ADR 010 Issue 4](010-conversation-replay-eval-harness.md) just filed.

### B. Catalog with user-driven manual selection
Rejected as the *only* answer. A user who has no idea what `think: false` is shouldn't have to debug "why is my chat broken" — that's the product's job. Manual override is fine as a power-user surface; it shouldn't be the default UX.

### C. Telemetry-driven recommendation (collect benchmarks from users, recommend based on aggregate)
Rejected. Violates [ADR 002](002-pure-local-product-shape.md)'s no-phone-home posture. Even anonymous benchmarks would create a vendor-side database that subverts the "your data never leaves your machine" pitch.

### D. Skip the probe; ship 14B as the canonical and call it done
Rejected. 14B is the right call *for M5 Pro 24 GB on Ollama 0.18.0*. On a 64 GB Mac Studio Ultra the 30B-A3B-Instruct-2507 may work cleanly and be a better choice. On an 8 GB MacBook Air, even 14B doesn't fit. The static choice is wrong on both ends; the probe is the only honest answer.

### E. Run the full latency-benchmark grid as the probe
Rejected. The full grid takes 45–90 minutes — unacceptable for an install-time UX. The probe is a narrow subset (2 scenarios × 1 timed run × hardware-prefiltered candidates) that completes in 30–60 seconds, sufficient for the "is this model usable" question without the variance precision the dev grid needs.

## Consequences

### Positive
- The "broken chat on this Ollama+OS combo" failure mode is caught before the user encounters it.
- The hardware-fit decision moves from "trust the user picked correctly" to "verified at install."
- Customer-machine numbers and dev-machine numbers come from the same code (`scenarios.py` + `harness.py`), so dev work translates directly to customer experience.
- Per-machine recommendation makes the "30B works for some users / 14B for others / 8B for others" reality first-class, instead of being a static guess for one of the three.
- The probe verdict is auditable JSON in `app/config.json` — a buyer can see *why* a particular model was picked.

### Negative
- Adds 30–60 seconds to first launch UX. Mitigated by running it as part of the existing onboarding flow (the user is already waiting on something) and surfacing progress.
- New service to maintain. Mitigated by reusing the latency-harness modules; the probe is mostly orchestration, not new measurement code.
- The pattern panel for thinking-prose detection is heuristic. Will need calibration against more model+Ollama combinations as we test on additional environments. Open follow-up.

### What this changes about existing code
- New `backend/services/chat_model_probe.py` — orchestration, probes, persistence.
- New `backend/tests/test_chat_model_probe.py` — unit tests with stubbed Ollama.
- New `docs/features/chat-model-probe.md` — feature doc.
- [`docs/.registry.json`](../../.registry.json) — new feature entry.
- Cross-links from `local-models.md` and `chat.md` (already added in the swap chunk).
- Future frontend chunk: settings + onboarding UI surface.

### What this does NOT change
- Production chat router behavior on a fully-configured machine. The probe runs at install / on demand; once the recommendation is persisted, the chat router reads `chat_model_probe.recommended_model` (or `user_override`) and that's it.
- The latency benchmark harness itself. Same scenarios, same harness, different invocation surface.
- The conversation eval harness. Both eval harnesses will be updated to read the persisted probe result for "what model do I pin against" if running on a non-dev machine, but the current dev pinning is preserved with the 14B amendment from ADR 010 Issue 4.

## Migration path

1. **Backend service lands** (this chunk): `chat_model_probe.py` + tests + ADR 012 + feature doc + registry entry. Probe is invokable programmatically; not yet wired into onboarding.
2. **Onboarding wiring** (next chunk): [`frontend/app/components/OnboardingLocalFlow.vue`](../../../frontend/app/components/OnboardingLocalFlow.vue) calls a new `/api/local/chat-model-probe` endpoint, displays progress, persists the verdict.
3. **Settings surface** (subsequent): re-run / override UI in `LocalModelsSection.vue`.
4. **Pattern-panel calibration** as more environments land: the thinking-prose regex grows by evidence, not by guess.

## Production wiring landed (2026-04-29)

Steps 2 and 3 above merged. End-to-end shape:

- **Re-run trigger detection.** `current_environment(*, ollama_version)` captures `(version, platform-with-macos-major, sorted catalog ollama_models)`; `needs_rerun(persisted, current)` returns `(bool, reason)` with reasons `no_prior_probe`, `ollama_version_changed`, `platform_changed`, `catalog_added_models`, `fresh`. Catalog *removals* don't trigger a re-run — only additions do, since a new model might be a better recommendation than the current pick.
- **Streaming primitive.** `iter_probe_events(...)` async generator yields `started` → (`candidate_start`, `candidate_evidence`)+ → `complete`. The blocking `recommend_chat_model(...)` now drains it, so streaming and non-streaming callers share one probe loop.
- **HTTP API** in [`backend/routers/local_models.py`](../../../backend/routers/local_models.py): `GET /api/local/chat-model-probe` (status + needs_rerun + env), `POST /api/local/chat-model-probe/run` (SSE; refuses 503 when Ollama unreachable; preserves prior `user_override` across re-runs), `POST /api/local/chat-model-probe/override` (set/clear; does not re-run).
- **Frontend.** `useChatModelProbe` composable consumes the SSE stream. `OnboardingLocalFlow.vue` runs the probe automatically after a model finishes downloading (between `model_ready` and "Open Jarvis"). `LocalModelsSection.vue` surfaces the verdict, evidence, the re-run reason, and a re-run + override picker. `pages/main.vue` calls `fetchStatus()` on mount and shows a non-blocking banner when `needs_rerun` is true.
- **Tests grew by 30**: 18 new units in `test_chat_model_probe.py` (re-run-trigger across all five reasons, macOS-major capture, override on empty config, generator event sequence, complete-payload-key parity with persistence) + 12 new endpoint tests in `test_local_models.py::TestChatModelProbeEndpoints` (status/no-prior, refused-when-down, override get+clear, SSE+persist, override preservation across re-runs).

## Open follow-ups (non-blocking)

1. **Pattern-panel false-positive risk.** A clean model might start an answer with "Okay, sure!" on a casual prompt and get marked as leaking. Mitigation: the prompt is "say hi in one word" — a clean model under that constraint produces 1–3 tokens, none of which match the thinking-prose patterns. Low risk in practice but worth a calibration pass once we have more environments.
2. **Probe extension for thinking-mode-supporting models.** Some users will *want* thinking mode (Qwen3-30B-A3B-Thinking variants). The probe currently treats thinking-mode emission as a fail. A future extension: detect intentional `<think>...</think>` blocks (which would have a closing tag) and treat them as pass-with-thinking-mode rather than fail.
3. **Speed-threshold tunability.** The `total ≤ 1500 ms` and `TPS ≥ 8` thresholds may need tightening for the marketing claim of "snappier than ChatGPT" or loosening for resource-constrained ICPs. Surface as config in `app/config.json`.
4. **Cross-machine portability.** A `.deepfileslic`-bundled probe verdict export would let an IT admin pre-test on a reference machine and ship the verdict alongside the install. Defer to v1.5 when fleet-deployment matters.
5. **Integration with [ADR 003](003-desktop-distribution-tauri-and-sidecars.md) sidecar.** The probe assumes Ollama is reachable on `127.0.0.1:11434`. The Tauri sidecar will own Ollama lifecycle; the probe runs after the sidecar is up. Documented in the bundle's first-launch flow.
