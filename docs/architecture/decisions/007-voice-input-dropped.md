# ADR 007 — Voice input dropped from v1 (TTS preserved)

**Status:** Accepted
**Date:** 2026-04-27
**Related:** [`docs/research/models/model-research-4.md`](../../research/models/model-research-4.md) §"Voice input dropped" · [`docs/features/voice.md`](../../features/voice.md) · [`docs/research/product-direction-v1-v2.md`](../../research/product-direction-v1-v2.md) §12.2

## Context

The product was originally pitched as voice-first — voice input + voice output as the primary interface to a local knowledge system. [`docs/research/product-direction-v1-v2.md`](../../research/product-direction-v1-v2.md) §12.2 records that voice-first positioning was an unvalidated product-differentiation bet, never tested with real customers. The hybrid-deployment math, the licensing math, and the compliance math all work with or without voice.

Voice *input* (ASR + always-on listening + wake word) carries real cost in this product:

- An additional always-resident model (Phi-4-multimodal 5.6B or Qwen3-Omni-30B-A3B-Instruct), competing for the constrained Tier-A 14 GB realistic LLM budget.
- An always-on listening surface that is its own privacy / compliance liability — a microphone hot in a privileged-work environment is exactly the kind of risk a legal buyer's IT review will flag.
- A wake-word and ASR error-recovery UX that is its own substantial design surface.
- Whisper-equivalent ASR quality in Polish (and other non-English target languages) is meaningfully worse than English, eroding the headline feature for the EU market.

[`model-research-4.md`](../../research/models/model-research-4.md) recorded the decision to drop voice input from v1, preserving TTS output (Kokoro-82M, ~80 MB, Apache 2.0). The rationale: TTS is cheap, has zero privacy / always-on liability, and preserves a meaningful slice of the "Jarvis talks to you" experience without input-side complexity.

The current codebase carries STT plumbing as part of the [`voice`](../../features/voice.md) feature ([`useVoice.ts`](../../../frontend/app/composables/useVoice.ts), STT/TTS provider abstractions, tests). Removing it entirely would discard working code that may be useful in v1.5+ if we revisit voice input with better models or a customer-validated need. Keeping it visible as an unsupported feature would mislead users.

## Decision drivers

1. **The voice-first claim is unvalidated.** No deep-dive tested whether voice input matters to engineering or legal buyers. Marketing it without evidence is a credibility risk.
2. **The Tier-A 14 GB LLM budget is tight.** A second always-resident model purely for ASR is hard to justify against the alternative (smarter chat model, vision slot for figures).
3. **An always-on microphone is a compliance liability** in the privileged-work environments this product targets. Even if disabled, surfacing the option requires explanation in IT-review questionnaires.
4. **TTS has none of these costs.** Kokoro-82M is small, fast, and only activates on output.
5. **The codebase already exists.** Removing it for v1 is a setting flip; the code can be revisited if v1.5+ has evidence to support voice input.
6. **Marketing copy must align with shipping reality.** "Voice-first" is a brand promise that, if broken, signals carelessness on every other promise.

## Decision

**Voice input (STT, ASR, always-on listening, wake word) is removed from v1 default behavior. TTS output is preserved.**

### Implementation

- **STT code is retained**, gated behind a feature flag `voice.input.enabled = false` in profile defaults and user preferences. The flag is not surfaced in the v1 UI.
- **TTS code is unchanged.** Kokoro-82M ships in every profile's stack as the TTS slot.
- **No STT model is bundled** in any v1 profile. Phi-4-multimodal, Qwen3-Omni, and Voxtral are removed from the default catalog. The STT provider interface (`STTProvider`) survives in the codebase but resolves to the existing Web Speech API browser-native implementation if the flag is ever flipped.
- **The voice button** ([`VoiceButton.vue`](../../../frontend/app/components/VoiceButton.vue)) is hidden when `voice.input.enabled = false`, which is the v1 default for every profile.
- **Marketing positioning** shifts from "voice-first" to **"local-first knowledge system that talks back"** (or equivalent). Update at minimum: [`overview.md`](../../overview.md), [`JARVIS-PLAN.md`](../../JARVIS-PLAN.md), the workspace-onboarding feature doc, and any onboarding UI strings.
- **The [`voice`](../../features/voice.md) feature doc** is updated to record that input is feature-flagged off by default in v1, and that the implementation pathway exists for v1.5+ if customer evidence justifies it.

### What's not changed

- The `STTProvider` / `TTSProvider` provider abstractions remain. They were correctly designed; the only question is which implementations ship enabled.
- The TTS feature is fully functional and shipped.
- Existing STT tests remain in the test suite. They run against the (still-present) Web Speech provider when the flag is flipped, primarily for regression detection if we ever reactivate.

## Alternatives considered

### A. Keep voice-first as the headline, ship STT in v1
Forces a choice we don't have evidence for. Adds an always-resident audio model that consumes 3–17 GB depending on choice; competes with chat ladder slots. **Rejected.**

### B. Remove voice entirely (delete STT code)
Discards a working implementation that may be useful in v1.5. Reactivating later requires re-implementing the provider abstraction. **Rejected.**

### C. Ship voice input as a "beta" feature surfaced in UI
Brand-risky — beta features in compliance products signal carelessness. **Rejected.**

### D. Ship voice input as a separate paid add-on SKU
Solves the bundle-size question but doubles the SKU complexity and marketing surface. Premature for v1. **Rejected.** Could become a future SKU if customer demand emerges.

## Consequences

### Positive
- Tier-A install footprint shrinks (no Phi-4-multimodal / Qwen3-Omni in the default stack).
- No always-on microphone surface; no IT-review concern about audio capture in privileged environments.
- Marketing copy aligns with shipped reality. "Local-first knowledge system that talks back" is honest and still differentiated from cloud chat tools.
- The provider abstraction survives intact for future reactivation.

### Negative
- The "voice-first" pitch in [`JARVIS-PLAN.md`](../../JARVIS-PLAN.md) and earlier marketing material is retired. Some prior-thinking artifacts are now superseded.
- Some early users may have wanted voice input. They become evidence for the v1.5 reactivation decision; in the meantime, dictation through the OS-level voice-input feature (macOS Dictation, Windows Voice Typing) covers most of the use case.
- TTS as the only voice surface makes the "Jarvis talks to you" experience asymmetric. The user types; Jarvis speaks. Some find this uncanny; mitigation is making TTS user-toggleable and not the default for every response.

### What this changes about existing code
- [`OnboardingLocalFlow.vue`](../../../frontend/app/components/OnboardingLocalFlow.vue) and the workspace-onboarding flow remove any "set up voice input" steps.
- [`useVoice.ts`](../../../frontend/app/composables/useVoice.ts) reads the `voice.input.enabled` flag; if false, the voice button is hidden and the `useSTT` machinery does not initialize.
- [`VoiceSection.vue`](../../../frontend/app/components/settings/VoiceSection.vue) splits into "Voice output (TTS)" — visible — and "Voice input (STT)" — hidden in v1.
- [`docs/features/voice.md`](../../features/voice.md) updates to note v1 status: TTS active, STT feature-flagged off.
- [`overview.md`](../../overview.md) updates the "Voice — Web Speech API" line to clarify TTS-only.
- [`docs/.registry.json`](../../.registry.json) entry for `voice` updates `last_updated` and the feature description.
- TTS in every install is Kokoro-82M.

## Open follow-ups (non-blocking)

1. **Marketing copy sweep.** Voice-first phrasing across [`overview.md`](../../overview.md), [`JARVIS-PLAN.md`](../../JARVIS-PLAN.md), onboarding strings, README, and any pitch-deck artifacts. Replace with the new positioning.
2. **TTS UX defaults for sensitive contexts.** Should installs targeted at litigation contexts default TTS off? Probably yes (audio leakage risk). Defer the default-off mechanism until the install-preset shape stabilises.
3. **v1.5+ voice-input reactivation criteria.** Document what evidence would justify reactivation — a candidate list: (a) a customer interview signal that voice is actually requested; (b) an ASR model in the strict-OSI catalog with Polish-quality parity; (c) a Tier-B+ default stack with headroom for the additional model.
4. **Browser-native dictation surface.** Until and unless STT reactivates, document in user-facing help that OS-level dictation works in any text input.
