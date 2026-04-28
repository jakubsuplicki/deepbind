# ADR 006 — Ed25519-signed offline license, no vendor admin portal

**Status:** Accepted
**Date:** 2026-04-27 (initial), amended 2026-04-28 (crypto-layer scaffold + signing CLI landed)
**Related:** [ADR 002](002-pure-local-product-shape.md) · [ADR 003](003-desktop-distribution-tauri-and-sidecars.md) · [`docs/research/deep-dive-licensing-architecture.md`](../../research/deep-dive-licensing-architecture.md) · [`docs/research/product-direction-v1-v2.md`](../../research/product-direction-v1-v2.md) §6, §11.4

## Implementation status (2026-04-28)

The **crypto layer** is implemented as a scaffold at [`backend/services/license_service.py`](../../../backend/services/license_service.py) — Ed25519 sign/verify, the `LicenseClaims` schema per §"Primitive" below, the `signature_b64.payload_b64` wire format, and a `now=` override on `verify_license` that pins the contract for the future Tauri-side keystore-backed clock-rollback defense. 23 tests cover round-trip, expiry-with-claims-still-parsed, signature/payload/key tampering, malformed input, canonical-JSON contract stability, timezone-awareness enforcement, and `feature_flags` typing. See the [licensing feature doc](../../features/licensing.md) for the API surface and gotchas.

**Code-review hardening (2026-04-28):**

- `feature_flags` narrowed from `dict` (anything goes) to `dict[str, bool]` matching this ADR's example shape. Pydantic rejects non-bool values at construction time so a buggy signing service can't ship licenses with garbage that consumers later have to handle defensively. If non-bool flags ever become required, expand the value type via an explicit union — do not relax to `dict[str, Any]`.
- `_canonical_json` constructed from explicit field references rather than `claims.model_dump()`. This forecloses the failure mode where a pydantic upgrade or a new optional field silently changes serialization and existing licenses stop verifying. Adding a field to `LicenseClaims` requires an explicit reviewed update to `_canonical_json` — a deliberate friction point on a load-bearing contract.
- Naive `expires_at` is rejected (`reason="expires_at must be timezone-aware (UTC)"`) rather than being silently fixed up to UTC. The signing service must always produce `Z` or `+00:00`; a naive timestamp surfaces the contract violation immediately instead of hiding it.
- `cryptography` dependency pinned to `==47.0.0` (was `>=41.0`). The crypto primitive is load-bearing enough that a future install drifting to a different version than CI tested is not acceptable.

**Signing CLI (2026-04-28):** [`backend/scripts/sign_license.py`](../../../backend/scripts/sign_license.py) is the offline signing tool that runs on the **private signing service** side — never bundled with the shipping app. Three subcommands: `generate-keypair` (writes 32-byte raw private/public to a directory with `0o600` permissions), `sign` (reads a `claims.json`, signs with the private key, emits the `signature_b64.payload_b64` wire format to stdout or `--out`), and `verify` (round-trip check before delivery). Refuses to overwrite existing key/license files unless `--overwrite` is passed — defaults to refusing rather than silently clobbering an in-use private key. Closes the loop on the crypto scaffold so a license can be produced end-to-end on the offline signing host without depending on Tauri-side packaging.

What is **deferred to post-[ADR 003](003-desktop-distribution-tauri-and-sidecars.md)** (Tauri shell):

- On-disk file path resolution at `~/Library/Application Support/Jarvis/license.json` (macOS) / `%APPDATA%\Jarvis\license.json` (Windows). Belongs to the Tauri shell that owns the app sandbox.
- OS-protected monotonic-state record per §"Clock-tampering defense" (macOS Keychain / Windows DPAPI / libsecret via the Tauri `keyring` plugin).
- Compile-time build-epoch floor — embedded at build time in the production binary.
- `.deepfileslic` UTI / file-extension association per §"Delivery UX" — registered by the platform installer.
- Service-layer entitlement gates per §"What this changes about existing code" — wired onto the verify primitive once the gated feature surfaces exist.

The crypto layer is platform-independent: Ed25519 sign/verify works identically regardless of which shell ships the binary. Building it before [ADR 003](003-desktop-distribution-tauri-and-sidecars.md) lands does not lock in any platform decision and gives the Tauri-side integration a typed primitive to consume. Building the deferred pieces in pure Python first would create production-grade fragile platform code we'd have to maintain forever (the same trap that applies to `vm_stat`-based memory probes on macOS — pure-Python today, native helper after Tauri).

## Context

V1 is sold per-seat into two opposite buyer archetypes: mid-market engineering firms (20–200 seats, some air-gapped, ITAR / CMMC-aware) and legal solos / boutiques (1–15 seats, post-Heppner privilege-conscious). The licensing-architecture deep dive ([`deep-dive-licensing-architecture.md`](../../research/deep-dive-licensing-architecture.md)) found these buyers want opposite things on almost every licensing surface — admin portal vs none, PO billing vs credit card, SCCM deployment vs DMG, SAML vs paste-a-key.

The single primitive that serves both, per the research §11.4, is the **Ed25519-signed offline license file**. It is verified locally, requires zero network I/O at runtime, and can be delivered in any wrapper (paste-a-key for solos, admin-uploaded into a self-hostable seat appliance for firms).

The architecture must also satisfy the no-cloud constraint of [ADR 002](002-pure-local-product-shape.md). A vendor-cloud licensing-as-a-service (Keygen.sh SaaS, LicenseSpring, Cryptolens) places a vendor domain in the customer trust path. That breaks the "your data never leaves your trust boundary" pitch even if no business data passes through it.

This pattern is also what the buyer's existing engineering software already does. The CAD/CAE incumbents that preserved offline workflows — Siemens NX on-prem (FlexNet), MATLAB (File Installation Key + Network Concurrent), Ansys on-prem, PTC Creo on-prem, classic SolidWorks SNL — all license via signed file refreshed annually via controlled media. The vendors that retreated from offline (Autodesk named-user, 3DEXPERIENCE, Creo+) lost DIB and ITAR share and are the licensing deep-dive's cautionary tale. Asking a defense-sub or IP-sensitive engineering firm to license the AI tool the same way they already license their FEA solver is a procurement non-event.

The licensing posture is a **corollary of [ADR 002](002-pure-local-product-shape.md), not an independent decision.** Once the inference path is committed to zero phone-home (because it touches the highest-sensitivity content in the firm — exactly the content the *Heppner* fact pattern turned on), allowing the licensing path to phone home buys nothing real. Real-time revocation is a comfort blanket against an abuse pattern (bulk over-deployment inside a legitimate customer) that no licensing model actually prevents without runtime check-in, and it costs both the architectural consistency of "zero outbound calls" and a vendor CDN log of "Firm X verified license on date Y" that a *Heppner*-style discovery motion would subpoena. Therefore: licensing verification is purely local too.

## Decision drivers

1. **Zero network I/O at license check.** The product must run for arbitrary periods offline. Periodic phone-home is unacceptable.
2. **No vendor admin portal.** A cloud admin surface knowing who has seats violates the no-third-party-data-flow promise and creates a subpoena target.
3. **Cross-buyer common primitive.** Whatever the wrapper, the verification primitive must be identical for engineering and legal buyers.
4. **Tamper resistance proportional to threat.** Bulk sharing inside a legitimate customer is the realistic leak; cracking is not. The deterrent is the embedded customer name and revocation through expiry, not anti-RE.
5. **Forward-compatible with a self-hostable seat appliance.** Firms wanting centralized control should be able to add it later without re-cutting existing licenses.
6. **Forward-compatible with per-vertical SKUs.** The license file must be able to scope which install presets / model bundles the customer is entitled to, even though v1 doesn't use this. The `allowed_profiles` field is preserved in `LicenseClaims` for whatever install-footprint scoping mechanism replaces v1's "single-bundle" default.

## Decision

### Primitive

The license is a **signed JSON file**, signed by us with an Ed25519 private key. The product embeds the corresponding public key. Verification is a local signature check; no network call.

```
{
  "license_id":          "lic_2026_04_27_xyz",       # opaque id
  "customer":            "Acme Engineering Pty Ltd", # embedded customer name (deterrent)
  "seat_count":          20,                          # honor-system; informational
  "issued_at":           "2026-04-27T00:00:00Z",
  "expires_at":          "2027-05-27T00:00:00Z",     # 13-month window
  "allowed_profiles":    null,                        # null = any profile; or [...] for SKU-scoped
  "feature_flags":       { "duel": true, "jira_ingest": true, ... },
  "schema_version":      1
}
+ Ed25519 signature
```

### Activation flow

1. Sales closes a deal. We run the signing service (a private Rails or FastAPI app on our infrastructure — never customer-touched) and produce a signed license file.
2. The file is delivered out-of-band: email attachment, admin upload to the firm's seat-management appliance, or pre-staged on a managed-deployment image. We never operate a customer-facing activation server.
3. Customer activates via one of three paths (see *Delivery UX* below): (a) opens the app and pastes the license block into the first-run screen, (b) double-clicks the `.deepfileslic` attachment from the welcome email — the installer registered the file association so the app handles writing to disk, (c) IT pre-deploys the file at `~/Library/Application Support/Jarvis/license.json` (Mac) / `%APPDATA%\Jarvis\license.json` (Windows) via Jamf / Intune / SCCM.
4. App verifies the signature against the embedded public key. If valid and not expired, all entitled features unlock at the **service layer** — not the UI layer. UI gates are bypassable; service-layer gates survive a debugger console.

### Delivery UX

The buyer profile is split between firms with no IT (legal solos, boutique engineering under 20 people) and firms with IT (mid-market engineering 20–200 seats). The licensing UX must not assume IT.

**For the no-IT buyer (default):**

1. **Email delivery.** When billing clears, the signing service emails the customer two artifacts: a `.deepfileslic` attachment and a paste-able text block (Sublime-style `BEGIN LICENSE … END LICENSE`).
2. **File-extension association.** The installer registers `.deepfileslic` as a Jarvis-handled file type (UTI on macOS, registry on Windows). Double-clicking the attachment launches the app and installs the license. The customer never navigates to `~/Library/Application Support/`.
3. **First-run paste-a-key screen.** On an unlicensed install, the first-run screen accepts the text block via paste. Same effect as the file-association path.
4. **Self-service resend page.** If the buyer loses the file or installs on a new machine, a static page on our site accepts email + customer name and re-mails the file. This is **not** a customer-facing activation server in the [ADR 002](002-pure-local-product-shape.md) sense — the *app* never talks to it; it is a one-shot lookup against the issuance log. The distinction matters because the no-vendor-admin-portal rule forbids the former, not the latter.
5. **Buy-from-app affordance.** The first-run screen carries a "Buy a license" button that opens a browser to checkout. On payment success the welcome email arrives. This is the Sketch / Sublime entry pattern.

**For the IT-managed buyer (20+ seats):** the same `.deepfileslic` file is delivered to the firm's IT contact, who scripts deployment to the standard path via their existing endpoint-management tool. The cryptographic primitive is identical; only the wrapper differs.

### Renewal

A new signed file is issued and delivered. Same delivery channel. The customer drops the new file in place; the old one is replaced. No conversation between the app and any vendor server.

The 13-month window provides a 30-day grace cushion past the typical 12-month renewal cycle; missed renewals fail soft (read-only mode for ~30 days, then hard read-only) rather than hard-stop the customer mid-deadline.

### Billing cadence

Billing is annual. The license file is the contract artifact: its expiry is the contract end, not a billing-cycle marker. Quoted as `$X/seat/year (~$Y/seat/month equivalent)` to ease comparison against monthly competitors (Copilot, Lexis), but the actual transaction is annual.

Pure month-to-month billing is not offered. The trilemma — monthly billing, offline-signed file, working revocation — is unsolvable without phone-home. With offline files there is no mechanism to stop a license issued for 13 months when the second monthly payment fails, so month-to-month billing would deliver up to a year of access for one month of payment. This is enforced by the architecture, not by a pricing preference.

For buyers who need monthly cash-flow on the engineering-firm side, multi-year prepay (2-year / 3-year) at standard CAD/CAE-procurement discounts (-10% / -15%) is offered. PO and NET-30 are offered at the engineering-firm tier; credit-card auto-renew is offered at the legal-solo tier. Both produce the same annual signing event and the same annual file.

### Revocation

There is no real-time revocation. "Revoke" means "do not issue the next renewal." A stolen license remains valid until expiry. This is the explicit trade — the cost of zero phone-home is revocation latency. The research record accepts this trade for both buyer profiles.

### Seat enforcement

Seats are honor-system + customer-name embedding. The license file says "Acme, 20 seats" — the buyer cannot easily re-share that file outside Acme without the recipient's machine displaying "Acme" as the licensed entity. Bulk over-deployment inside the customer is the realistic abuse pattern, and the licensing research notes it is also the dominant abuse pattern across every desktop ISV in this space; no SaaS license server prevents it without active phone-home.

**The growing-customer case (between v1 and v1.5).** A customer purchases 20 seats in January and grows to 100 deployments by July. The signed file says "Acme, 20 seats" and continues to verify on every machine — there is no runtime check that fails. Three layers handle this gap:

1. **Contract.** The MSA / EULA states that seat count is the contractual entitlement and over-deployment is a breach. This is the same contract layer Sublime, Beyond Compare, and Obsidian rely on, and it is durable in B2B because procurement audits its own software inventory.
2. **Customer-name visibility.** Every machine running Jarvis displays the licensed entity in Settings. A 100-machine firm cannot silently re-share a 20-seat file without 80 machines surfacing "Acme" to their users.
3. **Renewal true-up.** At annual renewal, sales asks the customer for current deployment count and re-issues at the right size. Customers who self-correct are charged the delta and re-issued; customers who under-report repeatedly are flagged.

This is not airtight against a determined bad actor inside the customer. The licensing research is explicit that no licensing model — phone-home or otherwise — prevents this without active runtime check-in. The v1.5 self-hostable seat-management appliance (below) closes the gap for firms that *want* it closed; v1 accepts the gap as the price of zero phone-home.

### Anti-tamper realism

The verification primitive is locally checked. A sufficiently determined user with a debugger can patch the verification out. The goal is to deter casual sharing and over-deployment, not to prevent piracy. Engineering effort spent on obfuscation is wasted; the deterrent is contractual and reputational, not cryptographic-against-the-end-user.

### Clock-tampering defense

Offline-only verification trusts the system clock to compare against `expires_at`. A customer who sets the clock backward to extend an expired license is a real attack vector. Two cheap layers, ~50 lines of code total:

1. **Compile-time epoch.** Each binary release embeds its build timestamp. App refuses to run if `current_time < build_epoch` — a 2027 binary cannot legitimately think the date is 2025.
2. **Monotonic state in OS-protected storage.** A signed record (Ed25519, same keypair as the license) of the highest timestamp the app has ever observed — stored in **macOS Keychain** on Apple platforms, **Windows DPAPI / Credential Manager** on Windows, and **libsecret / Secret Service** on Linux. This avoids the trivial "delete the file in `~/Library/Application Support/`" bypass that a plain on-disk state file would have. The Rust `keyring` crate (and adjacent Tauri plugins) abstracts the platform differences (~200 lines for the integration, not the underlying logic). On every launch, the app checks `current_time ≥ max(build_epoch, last_seen_time)`.

The signed record cannot be forged without our private key. Combined with the build-epoch floor and OS-keystore residence, this catches the realistic clock-rollback attack with one Ed25519 verify and two integer comparisons per launch.

**Failure UX (not a hard refuse).** When the clock check fails, the app does not silently exit or show a generic error. It shows a diagnostic screen: *"Your system clock appears to be set incorrectly — current: `$now`, expected: ≥ `$floor`."* A **Check my clock** button opens the OS date/time settings. A **Re-paste my license** button regenerates the state record from a fresh valid license, recovering legitimate fresh-install / hardware-replacement cases. Cost: one screen + two buttons. The alternative (hard refuse, no diagnostic) creates support tickets and reads as user-hostile to a customer whose CMOS battery genuinely died.

**Defeated only by:** evicting the OS keystore entry *and* running an older binary. Both obviously dishonest actions, both leave traces. Matches the *Anti-tamper realism* doctrine above — deter casual rollback, accept that determined bypass is possible. The deterrent against the determined bypass is contractual, not technical.

**Legitimate-use safe.** Dead CMOS battery, fresh OS reinstall, significant timezone travel, system-clock drift — none of these lock the customer out. The state file regenerates on first launch with a current-time clock, and the build-epoch floor is permissive enough that a year-old binary still runs fine. No support intervention required for normal clock drift.

**Out of scope for v1:** OS-trusted-time APIs (macOS `kSecTrustedTimestamp`, Windows update-channel timestamps from the signed Microsoft Store / winget feed), Roughtime / RFC 3161 anchored time, TPM / Secure Enclave monotonic counters, hardware-dongle real-time clocks (Thales Sentinel HL Time). All viable; none necessary at the v1 threat model. Revisit in v1.5+ only if abuse data warrants the added complexity.

### What the app reports back to us

**Nothing.** No machine fingerprinting, no usage telemetry, no error reports, no last-seen timestamp. The app's only outbound calls are the ones enumerated in [ADR 002](002-pure-local-product-shape.md): one-time first-launch model fetch (signed manifest, SHA-pinned URLs) and OS-level update channels. The license-verification path is purely local.

If we ever need machine binding (per Keygen's air-gapped pattern), the fingerprint is computed locally and embedded in the signed file at issue time — never transmitted at runtime. v1 does not bind to machines.

### Self-hostable seat-management appliance (v1.5)

A future Docker container customers run inside their own tenant. Speaks Keycloak / OIDC / SAML for the firms that want centralized identity. Cryptomator Hub / Bitwarden on-prem pattern. Issues delegated license tokens scoped by the parent license. Out of scope for v1; recorded here so the v1 license format ships forward-compatible with it (same Ed25519 keypair, same JSON schema, additional optional fields).

### Migration path from v1 license to v1.5 appliance

Customers running v1 paste-a-key receive a signed file. v1.5 appliance issues delegated tokens against the same parent key. Existing v1 license files remain valid; the appliance does not invalidate or replace them, only adds delegation. Forward-compatibility is preserved by the schema's `schema_version` field.

## Alternatives considered

### A. Keygen.sh SaaS (or LicenseSpring, Cryptolens)
Mature, well-documented, supports offline tokens. Places a vendor domain in the customer's trust path. **Rejected.** Keygen *CE* (self-hosted Rails app) is the version that's compatible with this ADR's posture; we may migrate to it post-v1 if operational pain demands richer license issuing. Keygen.sh-as-a-service is not.

### B. Online-first activation with periodic refresh
Industry default for SaaS-desktop hybrids (JetBrains, 1Password, Figma). Refreshes every N days; revokes near-real-time. Requires phone-home; incompatible with [ADR 002](002-pure-local-product-shape.md). **Rejected.**

### C. Hardware dongles (Thales Sentinel HL)
Bulletproof for true air-gap; lossy for mobile users; cost-per-unit hits margin. Niche fit (specialty CAM / EDA / CFD); over-engineered for this buyer. **Rejected as default.** Could be added as a premium SKU for ITAR-classified work post-revenue.

### D. Honor-system, no enforcement (Obsidian-style)
Works for low-piracy buyer bases. Engineering and legal procurement *expect* a license file as a paper trail; no-enforcement leaves enterprise revenue on the table by failing the procurement-paper-trail requirement. **Rejected** — the file is the artifact, even if enforcement is light.

### E. Smart contracts / blockchain
**Rejected** without further comment.

## Consequences

### Positive
- Zero phone-home satisfies CMMC L2 / Heppner / strict ITAR posture out of the box.
- Cross-buyer-portable: same primitive serves the 5-seat law boutique pasting a key and the 200-seat engineering firm uploading via their self-hostable appliance.
- Renewal is "we email a new file" — simple, predictable, low-tech.
- No vendor-side data store of "who has seats" → no subpoena target → no breach surface.
- Forward-compatible with the v1.5 self-hostable appliance and the per-vertical SKU mechanic without re-cutting the v1 license shape.

### Negative
- Revocation latency = grace window (~30 days). Mitigated by the customer relationship, not technology.
- No usage telemetry of any kind. We don't know which customers are active, which features they use, when they renew. Pricing and product decisions inform from sales conversations, not dashboards.
- Bulk-sharing inside a customer is undetectable. Mitigated by embedded customer-name visibility and the buyer's own internal procurement controls (which exist in both buyer profiles).
- The signing service is our infrastructure — small, but real (private VPS, key custody, audit log of issued licenses). One operational thing to own.

### What this changes about existing code
- New `backend/services/license_service.py` — Ed25519 signature verification, file-watch on the license path, expiry warnings.
- Service-layer entitlement gates (not UI gates) on every paid feature surface.
- New `frontend/app/pages/license.vue` (or settings section) — paste-a-key UX, license expiry banner, customer-name display.
- `app/config.json` does **not** carry the license — license file is its own artifact, separately backed up and version-controlled by the customer if they wish.
- Onboarding flow gains a license-paste step ahead of model fetch. Existing dev / pre-license installs continue to work via a development-build flag that the production-signed builds do not honor.

## Open follow-ups (non-blocking)

1. **Signing-service implementation.** Small Rails or FastAPI app on private infrastructure; key custody is the load-bearing operational concern, not the code. Hardware-backed key storage (YubiKey / HSM) before the first paid customer ships.
2. **License-issuing UI for ourselves.** We need a sales-facing tool to issue licenses without hand-running a CLI per customer. Lightweight admin form on the signing service.
3. **Apple notarization and Authenticode signing keys.** Adjacent to license-signing keys but distinct; ensure both key custody and the per-platform signing pipeline land before v1 ships.
4. **Schema for v1.5 delegated tokens.** When the self-hostable appliance lands, the parent-license-to-delegated-token relationship needs an exact schema. Out of scope for v1 but worth sketching during the v1 license format work to confirm forward-compatibility.
5. **Audit-log for the issuing side.** We should be able to answer "when did we issue this license, against what customer, with what scope" from our own records. Append-only log on the signing service.
6. **Customer-side license backup story.** If a customer loses the license file, can they re-request it? Yes — see *Delivery UX* above for the self-service resend pattern. Identity-verification strictness is the open sub-question (item 8 below).
7. **File-extension registration (`.deepfileslic`) per platform.** macOS: UTI + `CFBundleDocumentTypes` in the bundled `.app` `Info.plist`, plus an open-file event handler. Windows: registry entries via the MSI/MSIX installer (`HKEY_CLASSES_ROOT\.deepfileslic` + ProgID + default icon). Linux (if/when shipped): `xdg-mime` registration via the `.desktop` file. Tauri supports cross-platform file associations but each target needs explicit config; ensure install / update / uninstall paths all behave (don't orphan registrations on uninstall, don't break association on auto-update). Adjacent to [ADR 003](003-desktop-distribution-tauri-and-sidecars.md).
8. **Self-service resend page — identity-verification strictness.** Static page accepting email + customer name, looking up against the issuance log, re-mailing the file. Open: how strict is verification? Email-only is weak (a compromised mailbox gets the license). Email + customer-name + last-4 of payment is stronger. Email + signed challenge to a previously-registered admin contact is strongest. Pick the strictness that doesn't generate support volume.
9. **MSA / EULA seat-entitlement language.** Explicit clause stating that seat count in the license file is the contractual entitlement and over-deployment is a breach (the contract layer of the three-layer enforcement above). Lawyer review at MSA finalization, not v1 engineering.
10. **Welcome-email pipeline.** Billing webhook (Stripe / Paddle) → signing service → file generation → transactional mail (Postmark / SES / Resend). Template content (paste-able license block + `.deepfileslic` attachment + plain-language renewal-and-resend instructions) is the artifact the no-IT activation UX depends on; treat as a real product surface, not a script. Test deliverability across Outlook, Gmail, Apple Mail.
11. **License transferability policy.** The license is not machine-bound — the same file works on a replacement laptop, a new firm laptop, or an admin's reinstall. Explicit policy still needed for: (a) a customer firm that gets acquired (do their licenses transfer to the parent? merge with the acquirer's licenses?), (b) seat-reassignment within a firm when an employee leaves, (c) device replacement (laptop refresh cycle, broken machine). Lives in the MSA / EULA, not in code; flagged here so it doesn't get missed at MSA finalization.
12. **Soft-revocation via embedded CRL in binary updates (alternative, not for v1).** A revocation list could be embedded in each binary release — a customer's next Homebrew / winget / Microsoft Store update pulls a build that refuses to load revoked license IDs. Provides revocation without phone-home; latency = update cadence. **Currently rejected for v1** as build-pipeline complexity for a problem (post-fraud revocation) that we do not yet have. Recorded as an option for v1.5+ if the fraud-revocation use case becomes real; the schema's `license_id` field is already sufficient as the revocation key.
