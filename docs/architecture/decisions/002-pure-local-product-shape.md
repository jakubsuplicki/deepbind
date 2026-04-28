# ADR 002 — Pure local product shape

**Status:** Accepted
**Date:** 2026-04-27
**Related:** [`docs/research/product-direction-v1-v2.md`](../../research/product-direction-v1-v2.md) §1, §3, §3.3 · [`docs/SELF-CONTAINED-APP-REVIEW.md`](../../SELF-CONTAINED-APP-REVIEW.md) §1
**Supersedes:** "Option C — Hybrid (recommended)" in [`docs/SELF-CONTAINED-APP-REVIEW.md`](../../SELF-CONTAINED-APP-REVIEW.md) §1 (pre-2026-04-27)

## Context

V1 of the product runs on a customer's laptop. The buyer profile is engineering and IP-sensitive firms (patent boutiques, expert witnesses, mining and architecture firms, mechanical / process engineering shops, criminal-defence solos) priced out of Microsoft Copilot GCC and unable to use cloud LLMs at all. Two earlier internal artifacts disagreed on whether the product is local-first with optional cloud, or hybrid by default with local-first as a checkbox:

- [`JARVIS-PLAN.md`](../../JARVIS-PLAN.md) (predates the local-first pivot) describes a Claude-API browser product.
- The earlier draft of [`docs/SELF-CONTAINED-APP-REVIEW.md`](../../SELF-CONTAINED-APP-REVIEW.md) §1 recommended a hybrid default.
- [`docs/research/product-direction-v1-v2.md`](../../research/product-direction-v1-v2.md) §3.1 — written later — locked V1 as 100% per-laptop, zero outbound calls by default.

The buyer-imposed reality is sharper than any one of those framings:

- **CMMC L2** (32 CFR Part 170, effective 2024-12-16; DFARS 252.204-7021 effective 2025-11-10) treats any phone-home from a CUI-handling workstation as in-scope of the audit. Even a model-update check on vendor infrastructure becomes an audit finding.
- **U.S. v. Heppner** (SDNY, ruling Feb 17 2026) made disclosure of attorney-originated information to a third-party AI a candidate for **subject-matter privilege waiver** of the underlying communications. The court grounded its analysis in the AI tool's privacy policy. A product whose data never reaches a vendor makes the privacy-policy-as-waiver question inapplicable.
- The same architectural traits — no phone-home, on-disk data only, signed offline license files, no vendor-side retention — win both audits.

A "hybrid by default" or "local-first with cloud assist" stance loses both customer classes. Hybrid is not a *worse* product; it is a *different* product, and not the one the wedge was chosen for.

## Decision drivers

1. **The buyer cannot use cloud, period.** The compliance trees treat any vendor-side data flow as in-scope. Cloud as default disqualifies us before the procurement conversation starts.
2. **"Your data never leaves your laptop" must be technically true, not aspirationally true.** A single backdoor call falsifies the marketing.
3. **Markdown source-of-truth doctrine is unaffected.** [`CLAUDE.md`](../../../CLAUDE.md) commits to canonical Markdown + rebuildable derived layers regardless of inference shape. Pure-local does not change the data model.
4. **The strict-OSI license filter (Apache 2.0 / MIT only) is a procurement *accelerant* in 2026.** See [`docs/research/models/model-research-2.md`](../../research/models/model-research-2.md) and [`-3.md`](../../research/models/model-research-3.md). There is no capability sacrifice to staying inside Tier 1 licensing in 2026.
5. **A future cloud-fallback toggle must be possible without breaking the pure-local pitch.** Users who want it must opt in per workspace, with audit-log evidence of every outbound call.

## Decision

**V1 is 100% per-laptop. The product makes zero outbound calls by default.** The only sanctioned outbound calls during normal operation are:

- One-time model-blob fetch on first launch, from pinned URLs with SHA-verified manifests (filed in [ADR 003](003-desktop-distribution-tauri-and-sidecars.md) under "first-launch model fetch"). Optional pre-bundled USB SKU exists for true air-gap buys.
- License-file delivery is out-of-band (email, admin upload). The license verification itself is local Ed25519 signature check ([ADR 006](006-offline-signed-license.md)).
- OS-level update channels (Homebrew Cask, winget, Microsoft Store, Apple notarization stapler). These are OS behaviors, disclosed in marketing as such. The app's own servers do not see per-customer hits.

**No cloud LLM call by default. No vendor admin portal. No telemetry beyond license verification.** Every paid feature gates at the service layer on entitlement-check against the local signed license file.

A **cloud-fallback toggle** is recorded as a future v1.1+ addition, off by default, gated behind:

- Per-workspace explicit opt-in.
- A hash-chained outbound audit log (Trillian / Certificate Transparency pattern) capturing timestamp, model, token count, content hash, and originating tool call for every outbound call.
- Customer's own API key — Jarvis never proxies cloud inference through our infrastructure.
- UI surface that makes the toggle's posture obvious (active state + last-call summary).

The shape is documented in [`docs/research/product-direction-v1-v2.md`](../../research/product-direction-v1-v2.md) §3.3 and §6. The toggle is **not in v1 code paths and not in v1 marketing.** Adding it before v1 ships dilutes the pitch.

## License posture (corollary of pure-local)

Default catalog includes Apache 2.0 / MIT only. Bundle-shippable models, no AUP flow-down, no MAU caps, no revenue thresholds. Specifically:

- **Allowed default:** Qwen3 family, IBM Granite 4, Mistral's Apache-2.0 December-2025 wave (Devstral Small 2, Ministral 3, Mistral Small 4), Microsoft Phi-4, Google Gemma 4 (Apache 2.0 since 2026-04-02), OpenAI gpt-oss, Allen AI OLMo 3, HuggingFace SmolLM3.
- **Excluded entirely:** Llama (any version — AUP "no unauthorized practice" clause collides directly with the legal/medical buyer profile), Gemma 1–3 (Gemma ToU with PUP flow-down), DeepSeek (custom OpenRAIL), Cohere (CC-BY-NC), Falcon 3/H1 (TII license, despite "Apache-based" marketing), Tencent Hunyuan, Baichuan, DBRX, StableLM, AI21 Jamba 1.5+, NVIDIA Nemotron, Liquid LFM, Apple OpenELM, Kimi K2 flagships (Modified MIT branding clause).
- **Entity-list-excluded despite clean license:** GLM-4.5 / 4.6 / 5 / 5.1 — Z.ai (Beijing Zhipu Huazhang) added to U.S. BIS Entity List 2025-01-16 (Federal Register 90 FR 4617). The license grant cannot override the export-control posture for U.S. defense/patent customers.

The license posture lives inside this ADR rather than as a separate one because it is a corollary of the pure-local + bundle-shippable decision: a model with restrictive terms cannot be bundled into a no-phone-home installer because the terms cannot be enforced or attributed at runtime without phoning home or surfacing legalese to end users in a way the strict pitch cannot tolerate.

## Alternatives considered

### A. Pure cloud (Claude API browser-app desktop wrapper)
Strongest model quality. Disqualified for both buyer classes (CMMC + Heppner). Reduces Jarvis to a Claude reskin. **Rejected.**

### B. Hybrid by default (local + cloud co-equal)
Was the recommendation in the earlier draft of [`SELF-CONTAINED-APP-REVIEW.md`](../../SELF-CONTAINED-APP-REVIEW.md) §1. The compliance trees do not distinguish between "hybrid default" and "cloud default" — any phone-home is in-scope. Loses the buyer profile. **Rejected.**

### C. Local with always-on Anthropic-key bring-your-own assist
Customer's key, customer's calls. Avoids vendor proxying but every call still goes to Anthropic, who sees it. Heppner's privacy-policy-as-waiver argument applies to Anthropic's privacy policy, not ours, but the legal buyer's exposure is unchanged. **Rejected as default.** Becomes the v1.1 toggle described above.

### D. Local with self-hosted vendor-cloud admin portal
Even a non-inference admin portal (seat management, telemetry dashboards) breaks the "your data never leaves your trust boundary" pitch and creates a vendor-side data store subject to subpoena. **Rejected.** A self-hostable seat-management appliance is the correct shape if firms want centralized admin, deferred to v1.5 ([ADR 006](006-offline-signed-license.md)).

## Consequences

### Positive
- Single, defensible product narrative for the buyer's compliance officer / general counsel.
- "Apache 2.0 / MIT, no AUP flow-down, no third-party data flow" closes a class of procurement review questions before they're asked.
- Hardware floor is honest and visible — customers can self-qualify before purchase.
- The strict-OSI license filter aligns with this decision (no licensing-tax procurement reviews).

### Negative
- Hardware floor excludes some buyers (no fallback to cloud for laptops that can't run a 14B model). Mitigated by smaller-model alternatives at install + the future memory-pressure auto-downgrade for in-session pressure, but unavoidable below Tier-A hardware.
- Loses access to frontier model capability. A 30B-A3B local stack is not GPT-5. The wedge is "good enough locally, sovereign," not "best in class."

### What this changes about existing code
- [`backend/routers/chat.py`](../../../backend/routers/chat.py) `_make_llm()` — current default is Claude. Default must invert to local. Cloud paths gated behind the (future) cloud-toggle and the existing privacy gate.
- [`backend/services/privacy.py`](../../../backend/services/privacy.py) `assert_provider_allowed()` — already exists; becomes the entry point for the toggle gate when v1.1 lands.
- [`docs/JARVIS-PLAN.md`](../../JARVIS-PLAN.md) — predates the pivot; should carry a banner noting that §4–§5 are superseded by this ADR for product shape.

## Open follow-ups (non-blocking)

1. Banner [`JARVIS-PLAN.md`](../../JARVIS-PLAN.md) at the top to point readers at this ADR for product shape.
2. The cloud-fallback toggle's audit-log shape is defined in [`product-direction-v1-v2.md`](../../research/product-direction-v1-v2.md) §6. When v1.1 schedules the toggle, file a follow-up ADR locking the audit-log schema (Trillian-style content hashing, append-only, exportable).
3. The [Kovel-workflow tagging](../../research/product-direction-v1-v2.md) hook (§6 of product-direction) is a separate compliance feature not gated by this ADR; recorded here only because both touch the audit-log surface.
