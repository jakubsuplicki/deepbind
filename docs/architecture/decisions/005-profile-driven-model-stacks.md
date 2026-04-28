# ADR 005 — Profile-driven model stacks (`ProfilePack`)

**Status:** Accepted
**Date:** 2026-04-27
**Related:** [ADR 002](002-pure-local-product-shape.md) · [ADR 004](004-inference-router-architecture.md) · [ADR 006](006-offline-signed-license.md) · [`docs/research/angle-engineering-firms.md`](../../research/angle-engineering-firms.md) · [`docs/research/product-direction-v1-v2.md`](../../research/product-direction-v1-v2.md) §11.7

## Context

The recommended stack from [`model-research-4.md`](../../research/models/model-research-4.md) is implicitly developer-flavored — embeddings + plumbing + conversational + reasoning + coder + TTS. But the buyer profile is heterogeneous. Per [`docs/research/product-direction-v1-v2.md`](../../research/product-direction-v1-v2.md) §11.7 and [`angle-engineering-firms.md`](../../research/angle-engineering-firms.md), the realistic ICPs are:

- Patent prosecutor (legal)
- Litigation / criminal-defence boutique (legal)
- Expert witness (legal)
- Mechanical / process engineer (engineering)
- Architecture / building design (AEC)
- Mining / geological engineering
- Pharma / medical pre-filing
- Generic professional knowledge worker
- Developer / DevOps (the implicit case the research stack was sized for)

Most of these do not need a coder slot. A patent prosecutor's coder model is dead weight on disk and a wasted downgrade rung in the chat ladder. The vision slot, by contrast, matters most for legal (figures and exhibits) and AEC (drawings) — and least for generic chat.

A one-size-fits-all stack at install ignores this and forces every customer to download every slot. For a Tier-A laptop, the difference between "patent profile" and "engineer profile" is roughly 30 GB versus 65 GB of weights. That is not optimization; that is the difference between a defensible install footprint and a procurement objection.

The earlier draft of this work treated profile selection as something done implicitly through specialist installation. That conflates two layers: the model stack (capability) and the persona prompt (style). They compose, but they are distinct.

## Decision drivers

1. **Don't ship dead-weight slots.** A profile that doesn't need coding shouldn't carry a coder model.
2. **Profiles are post-purchase, not SKUs.** Architecturally compatible with per-vertical SKUs *or* a single SKU with profile pick at onboarding. The decision between these is GTM, not architectural; lock the architecture, defer the GTM choice.
3. **Profile change must be free, in-app, immediate** — only the model download deltas cost disk and time. No renewal gate, no reactivation.
4. **One canonical schema.** The `ProfilePack` manifest is the contract every other component (router, specialists, tool dispatch, onboarding, license) reads from.
5. **Specialists are persona prompts, not capability bundles.** The patent-prosecution profile pre-installs the patent-prosecution specialist; both are present, doing different jobs at different layers.
6. **Composition is a v1 trap.** "Patent prosecutor + occasional developer" sounds reasonable until conflict resolution multiplies. Single primary profile in v1; revisit composition only if real customers ask.

## Decision

### Schema

```
ProfilePack:
  id:                "patent-prosecutor"            # stable identifier
  display_name:      "Patent prosecution"
  version:           1                              # for migration
  stack:
    embeddings:      always-resident model spec     # required
    plumbing:        always-resident model spec     # required
    conversational:  ladder                         # required
    reasoning:       model or mode-toggle reference # required
    coder:           ladder | null                  # optional
    vision:          model | null                   # optional
    long_context:    model | null                   # optional (Tier B+ usually)
    tts:             model | null                   # optional
  specialists:       [ specialist_id, ... ]         # pre-installed personas
  tools:             { tool_id: enabled, ... }      # MCP tool defaults
  ingest_defaults:   { pdf_section_split: ..., cad_metadata: ..., ... }
  ui_strings:        locale + per-vertical phrasing
  context_recent_n:  default for the conversation's recent verbatim window  # see ADR 009
  context_perf_mode_default: "balanced" | "quality" | "lightweight"        # see ADR 004
```

Each model spec inside the stack carries: `model_id` (catalog reference), `quant`, `pinned_sha`, `expected_footprint_at_default_ctx`. Pinning is per [ADR 003](003-desktop-distribution-tauri-and-sidecars.md).

### Initial profile catalog

| Profile | Coder | Vision | Long-context | Notes |
|---|---|---|---|---|
| Patent prosecutor | — | required (figures) | optional (Tier B+) | Prior-art / claims specialists pre-installed |
| Litigation / criminal-defence | — | required (exhibits) | optional | Heppner-aware audit affordances |
| Expert witness | — | required | optional | Citation-heavy specialist defaults |
| Mechanical / process engineer | required | optional (drawings) | — | Devstral 2 + Qwen3-Coder both available |
| Architecture / AEC | optional add-on | required (drawings) | — | Ingest defaults tuned for drawing-heavy PDFs |
| Mining / geological | optional | required (maps, logs) | — | |
| Pharma / medical pre-filing | — | required | optional | Strict audit defaults |
| Generic knowledge worker | — | optional | — | Smallest install footprint |
| Developer / DevOps | required | optional | — | The original implicit profile |

Common to every profile: Qwen3-Embedding-0.6B (embeddings), Granite 4.0 H-Micro (plumbing), Qwen3 chat ladder (conversational + reasoning), Kokoro-82M (TTS).

### Mutability — four layers

| Layer | Surface | Cost when changed |
|---|---|---|
| **1. Profile** | Settings → "Change profile" | Diff download (delta only); 30-day GC on dropped models |
| **2. Slot enable/disable** | Settings → Slots | Per-slot download or evict |
| **3. Model within slot** | Settings → Advanced | Single model swap |
| **4. Version pin** | Settings → Advanced (auto-update toggle) | None; or staged on next manifest |

Most users stop at layer 1. Layers 2–3 live behind an "Advanced" reveal. Layer 4 is automatic with a pinned-version escape hatch for compliance buyers who want SHA-stable installs across audits.

A slot toggled off mid-session does not crash an in-flight request — the router treats slot config as a per-request check. If the user disables the coder slot while a code request is queued, the request fails gracefully with a clear message ("coder slot disabled mid-request"), not a crash.

### License interaction

The signed license file (per [ADR 006](006-offline-signed-license.md)) carries an optional `allowed_profiles` field. By default this is unset, meaning the customer can switch to any profile in the catalog. Per-vertical SKUs (a future GTM option) populate this field to scope a discounted SKU to one or more profiles. The default v1 license issues with `allowed_profiles` unset — full profile mobility.

### Profile change does not retroactively affect active conversations

A conversation pinned to its loadout at start ([ADR 008](008-conversation-pinned-chat-model.md)) keeps that loadout for its lifetime. Profile change applies to subsequent conversations. Avoids the ugly case where a user mid-chat with the coder slot active changes profile and the active turn's tooling vanishes.

### Specialists are still distinct

The `specialists` feature ([`docs/features/specialists.md`](../../features/specialists.md)) is unchanged in shape. A specialist is a constrained system-prompt configuration; a profile is a stack manifest plus specialist *defaults*. The patent-prosecutor profile pre-installs the patent-prosecution specialist; the user can install additional specialists ("biotech claims drafter") on top, no re-profile required.

## Alternatives considered

### A. No profiles — one stack for everyone
Forces every customer to download every slot. ~65 GB Tier-A install for a patent boutique that will never run a coder model. **Rejected.**

### B. Per-vertical SKUs (no in-product profile picker)
Marketing team picks a SKU, customer buys it, install matches. Couples GTM to architecture: a customer evolving from "patent only" to "patent + occasional engineer" requires a new license purchase. **Rejected as the architectural default.** The `allowed_profiles` license field preserves this option as a GTM choice without forcing it.

### C. Composable profile fragments ("base + coder add-on + vision add-on")
Initially attractive. Combinatorial conflict resolution multiplies fast (which UI strings? which specialist set wins? which ingest defaults?). For v1, a small fixed catalog is more honest. Composition can land in v2 if real customer demand justifies the complexity. **Rejected for v1.**

### D. Profile = specialist
Conflates capability bundle with persona prompt. Already explored; doesn't fit because specialists run *inside* the conversational model and don't determine which slots are loaded. **Rejected.**

### E. Profile chosen by hardware tier alone
Smaller hardware = lighter profile. Treats hardware as the dimension, when the actual dimension is *what the user does for a living*. **Rejected.**

## Consequences

### Positive
- Install footprint is right-sized to the customer's actual work.
- Onboarding asks one extra question ("what do you do?") and removes the model-pick screen entirely.
- The `ProfilePack` is the single contract every component reads from — router, specialists, tools, ingest, UI strings, license.
- The architecture is GTM-neutral: per-vertical SKUs and single-SKU-with-profile-pick both ship from the same code.
- Profile change is free and immediate; customer evolution doesn't require new license purchase.

### Negative
- More state in the schema. Migration of any profile-related field requires schema versioning (already accommodated by the `version` field).
- The initial catalog is a judgment call. Adding a new profile requires (a) catalog entry, (b) specialist defaults, (c) UI strings, (d) ingest defaults validation. Not heavy individually; heavy if the catalog inflates.
- Profile change has a real cost (model downloads). The 30-day GC of dropped models is a UX choice; users who flip profiles aggressively could see disk thrash. Mitigated by the explicit "free now" button and the fact that profile changes are infrequent in practice.

### What this changes about existing code
- New `backend/services/profile_service.py` (or extension of existing config service). Loads + validates profile manifests; surfaces active profile to the router.
- `app/config.json` schema change: replace `local_model.active` with `local_stack.{slots}` and `active_profile_id`. Lossless one-shot migration.
- `frontend/app/pages/onboarding.vue` and [`OnboardingLocalFlow.vue`](../../../frontend/app/components/OnboardingLocalFlow.vue): add profile-pick step before hardware probe; remove single-model-pick step.
- New `frontend/app/components/ProfilePicker.vue` and `ProfileSettings.vue` for layer 1–4 mutability.
- [`specialist_service.py`](../../../backend/services/specialist_service.py) gains a "profile-default" attribute; specialist installs are no longer purely user-driven.
- `Tools` filter logic in [`chat.py`](../../../backend/routers/chat.py) reads from active profile's `tools` map instead of a global default.

## Open follow-ups (non-blocking)

1. **Lock the initial catalog of nine profiles.** The list above is directionally right per the research; specifics of specialist defaults, tool defaults, and UI strings per profile need to be filled in by someone close to each vertical. Mining/architecture/medical defaults in particular need a domain pass.
2. **Profile version migration policy.** When a profile manifest changes shape (new field, removed slot), how do existing installs upgrade? Likely: bump `version`, ship a migrator, fall back to defaults for missing fields.
3. **Profile JSON schema published as part of the public API.** Customers may want to author internal profiles (large firms with bespoke needs). Out of scope for v1, on the radar.
4. **`allowed_profiles` enforcement test.** When the license restricts profiles, the UI must not show the disallowed entries; the router must refuse to route to slots that wouldn't exist. Testable.
5. **Generic-knowledge-worker profile** is a fallback. Verify it isn't actually the most-installed profile in practice — if it is, that's a signal the more specific catalog is too narrow.
