# ADR 006 — Ed25519-signed offline license, no vendor server

**Status:** Accepted
**Date:** 2026-04-27 (initial), amended 2026-04-28 (crypto-layer scaffold + signing CLI landed)
**Last updated:** 2026-06-14
**Related:** [ADR 002](002-pure-local-product-shape.md) · [ADR 003](003-desktop-distribution-tauri-and-sidecars.md)

> **Reference-implementation note.** The licensing subsystem described here is kept in the repository as a **reference implementation of offline, server-free license verification** — Ed25519-signed license files, local verification, no phone-home. It is not a live commercial product. This ADR records the *technical mechanism and decision rationale* only; it deliberately omits all commercial and go-to-market concerns.

## Implementation status (2026-04-28)

The **crypto layer** is implemented as a scaffold at [`backend/services/license_service.py`](../../../backend/services/license_service.py) — Ed25519 sign/verify, the `LicenseClaims` schema per §"Primitive" below, the `signature_b64.payload_b64` wire format, and a `now=` override on `verify_license` that pins the contract for the future Tauri-side keystore-backed clock-rollback defense. 23 tests cover round-trip, expiry-with-claims-still-parsed, signature/payload/key tampering, malformed input, canonical-JSON contract stability, timezone-awareness enforcement, and `feature_flags` typing. See the [licensing feature doc](../../features/licensing.md) for the API surface and gotchas.

**Code-review hardening (2026-04-28):**

- `feature_flags` narrowed from `dict` (anything goes) to `dict[str, bool]` matching this ADR's example shape. Pydantic rejects non-bool values at construction time so a buggy signing service can't produce licenses with garbage that consumers later have to handle defensively. If non-bool flags ever become required, expand the value type via an explicit union — do not relax to `dict[str, Any]`.
- `_canonical_json` constructed from explicit field references rather than `claims.model_dump()`. This forecloses the failure mode where a pydantic upgrade or a new optional field silently changes serialization and existing licenses stop verifying. Adding a field to `LicenseClaims` requires an explicit reviewed update to `_canonical_json` — a deliberate friction point on a load-bearing contract.
- Naive `expires_at` is rejected (`reason="expires_at must be timezone-aware (UTC)"`) rather than being silently fixed up to UTC. The signing service must always produce `Z` or `+00:00`; a naive timestamp surfaces the contract violation immediately instead of hiding it.
- `cryptography` dependency pinned to `==47.0.0` (was `>=41.0`). The crypto primitive is load-bearing enough that a future install drifting to a different version than CI tested is not acceptable.

**Signing CLI (2026-04-28):** [`backend/scripts/sign_license.py`](../../../backend/scripts/sign_license.py) is the offline signing tool that runs on the **private signing host** side — never bundled with the shipping app. Three subcommands: `generate-keypair` (writes 32-byte raw private/public to a directory with `0o600` permissions), `sign` (reads a `claims.json`, signs with the private key, emits the `signature_b64.payload_b64` wire format to stdout or `--out`), and `verify` (round-trip check before delivery). Refuses to overwrite existing key/license files unless `--overwrite` is passed — defaults to refusing rather than silently clobbering an in-use private key. Closes the loop on the crypto scaffold so a license can be produced end-to-end on the offline signing host without depending on Tauri-side packaging.

What is **deferred to post-[ADR 003](003-desktop-distribution-tauri-and-sidecars.md)** (Tauri shell):

- On-disk file path resolution at `~/Library/Application Support/Jarvis/license.json` (macOS) / `%APPDATA%\Jarvis\license.json` (Windows). Belongs to the Tauri shell that owns the app sandbox.
- OS-protected monotonic-state record per §"Clock-tampering defense" (macOS Keychain / Windows DPAPI / libsecret via the Tauri `keyring` plugin).
- Compile-time build-epoch floor — embedded at build time in the production binary.
- `.deepfileslic` UTI / file-extension association per §"Delivery UX" — registered by the platform installer.
- Service-layer entitlement gates per §"What this changes about existing code" — wired onto the verify primitive once the gated feature surfaces exist.

The crypto layer is platform-independent: Ed25519 sign/verify works identically regardless of which shell ships the binary. Building it before [ADR 003](003-desktop-distribution-tauri-and-sidecars.md) lands does not lock in any platform decision and gives the Tauri-side integration a typed primitive to consume. Building the deferred pieces in pure Python first would create production-grade fragile platform code we'd have to maintain forever (the same trap that applies to `vm_stat`-based memory probes on macOS — pure-Python today, native helper after Tauri).

## Context

The product must run fully offline — including for deployments that are entirely air-gapped — and verify its license without any network round-trip. Any verification path that touches a remote server fails that requirement outright.

The single primitive that satisfies this is the **Ed25519-signed offline license file**. It is verified locally, requires zero network I/O at runtime, and can be delivered in any wrapper (a pasted text block, a file dropped into the app's data directory, or a file pre-staged on a managed-deployment image).

The architecture must also satisfy the no-cloud constraint of [ADR 002](002-pure-local-product-shape.md). A vendor-cloud licensing-as-a-service places a vendor domain in the customer trust path. That breaks the "your data never leaves your trust boundary" posture even if no business data passes through it.

This is also the long-standing pattern for offline-first engineering software, which licenses via a signed file refreshed periodically through controlled media rather than a runtime check-in. Licensing a local-first tool the same way an air-gapped environment already licenses its other software is a deliberate goal.

The licensing posture is a **corollary of [ADR 002](002-pure-local-product-shape.md), not an independent decision.** Once the inference path is committed to zero phone-home (because it touches the highest-sensitivity content the product holds), allowing the licensing path to phone home buys nothing real. Real-time revocation is a comfort blanket against an abuse pattern (bulk over-deployment inside a legitimate install) that no licensing model actually prevents without runtime check-in, and it costs both the architectural consistency of "zero outbound calls" and a vendor-side log of "deployment X verified license on date Y" that a discovery motion could subpoena. Therefore: licensing verification is purely local too.

## Decision drivers

1. **Zero network I/O at license check.** The product must run for arbitrary periods offline. Periodic phone-home is unacceptable.
2. **No vendor admin portal.** A cloud admin surface knowing who has seats violates the no-third-party-data-flow posture and creates a subpoena target.
3. **Single common primitive across deployments.** Whatever the delivery wrapper, the verification primitive must be identical.
4. **Tamper resistance proportional to threat.** Bulk sharing inside a legitimate install is the realistic leak; cracking is not. The deterrent is the embedded entity name and expiry, not anti-RE.
5. **Forward-compatible with a self-hostable seat appliance.** A future centralized-control deployment should be addable later without re-cutting existing licenses.
6. **Forward-compatible with scoped entitlements.** The license file must be able to scope which install presets / model bundles a license is entitled to, even though v1 doesn't use this. The `allowed_profiles` field is preserved in `LicenseClaims` for whatever install-footprint scoping mechanism replaces v1's "single-bundle" default.

## Decision

### Primitive

The license is a **signed JSON file**, signed with an Ed25519 private key. The product embeds the corresponding public key. Verification is a local signature check; no network call.

```
{
  "license_id":          "lic_2026_04_27_xyz",       # opaque id
  "customer":            "Acme Engineering Pty Ltd", # embedded entity name (deterrent)
  "seat_count":          20,                          # honor-system; informational
  "issued_at":           "2026-04-27T00:00:00Z",
  "expires_at":          "2027-05-27T00:00:00Z",     # 13-month window
  "allowed_profiles":    null,                        # null = any profile; or [...] for scoped entitlement
  "feature_flags":       { "duel": true, "jira_ingest": true, ... },
  "schema_version":      1
}
+ Ed25519 signature
```

### Activation flow

1. A signed license file is produced offline on a private signing host — never customer-touched. There is no customer-facing activation server.
2. The file is delivered out-of-band: as a file attachment, an admin upload into a self-hostable seat appliance, or pre-staged on a managed-deployment image.
3. The license is activated via one of three paths (see *Delivery UX* below): (a) opening the app and pasting the license block into the first-run screen, (b) double-clicking the `.deepfileslic` file — the installer registered the file association so the app handles writing to disk, (c) IT pre-deploys the file at `~/Library/Application Support/Jarvis/license.json` (Mac) / `%APPDATA%\Jarvis\license.json` (Windows) via Jamf / Intune / SCCM.
4. The app verifies the signature against the embedded public key. If valid and not expired, all entitled features unlock at the **service layer** — not the UI layer. UI gates are bypassable; service-layer gates survive a debugger console.

### Delivery UX

The activation UX must not assume a customer has IT support. It supports both a no-IT install and a managed deployment with the same underlying file.

**For the no-IT install (default):**

1. **Two artifacts.** The license is delivered as a `.deepfileslic` file plus a paste-able text block (Sublime-style `BEGIN LICENSE … END LICENSE`).
2. **File-extension association.** The installer registers `.deepfileslic` as a Jarvis-handled file type (UTI on macOS, registry on Windows). Double-clicking the file launches the app and installs the license. The user never navigates to `~/Library/Application Support/`.
3. **First-run paste-a-key screen.** On an unlicensed install, the first-run screen accepts the text block via paste. Same effect as the file-association path.
4. **Self-service re-delivery.** If the license file is lost or the app is installed on a new machine, the file can be re-issued from the issuance log. This is **not** a customer-facing activation server in the [ADR 002](002-pure-local-product-shape.md) sense — the *app* never talks to it; it is a one-shot offline lookup. The distinction matters because the no-vendor-admin-portal rule forbids the former, not the latter.

**For the managed deployment:** the same `.deepfileslic` file is delivered to the IT contact, who scripts deployment to the standard path via an existing endpoint-management tool. The cryptographic primitive is identical; only the wrapper differs.

### Expiry and renewal (technical behavior)

A license carries an absolute `expires_at` timestamp. Renewal is mechanically just **a new signed file delivered through the same channel**; the customer drops the new file in place and it replaces the old one. There is no conversation between the app and any server, and there is no "extend the existing license" operation — the Ed25519 signature commits to absolute timestamps at sign time, so a new validity window means a freshly signed file.

The `expires_at` window is set ~30 days beyond the nominal entitlement period to provide a **grace cushion**: an expired license fails *soft* (read-only mode for ~30 days, then hard read-only) rather than hard-stopping the moment the timestamp passes. The exact state transitions are defined in [ADR 019](019-licensing-operational-model.md).

### Revocation

There is no real-time revocation. "Revoke" means "do not issue the next renewal." A stolen license remains valid until expiry. This is the explicit trade — the cost of zero phone-home is revocation latency. The design accepts this trade.

### Seat enforcement

Seats are honor-system + entity-name embedding. The license file says "Acme, 20 seats" — the file cannot easily be re-shared outside Acme without the recipient's machine displaying "Acme" as the licensed entity. Bulk over-deployment inside a single install is the realistic abuse pattern, and it is also the dominant abuse pattern across desktop software in this space; no SaaS license server prevents it without active phone-home.

The signed file says "Acme, 20 seats" and continues to verify on every machine — there is no runtime check that fails if more machines run it than the stated seat count. Two technical layers narrow the gap:

1. **Entity-name visibility.** Every machine running Jarvis displays the licensed entity in Settings. A 100-machine install cannot silently re-share a 20-seat file without 80 machines surfacing "Acme" to their users.
2. **Schema field for the count.** The `seat_count` field records the entitled count and is available to any future seat-management surface; v1 treats it as informational.

This is not airtight against a determined bad actor inside the install. No licensing model — phone-home or otherwise — prevents this without active runtime check-in. The v1.5 self-hostable seat-management appliance (below) closes the gap for deployments that *want* it closed; v1 accepts the gap as the price of zero phone-home.

### Anti-tamper realism

The verification primitive is locally checked. A sufficiently determined user with a debugger can patch the verification out. The goal is to deter casual sharing and over-deployment, not to prevent piracy. Engineering effort spent on obfuscation is wasted; the deterrent is contractual and reputational, not cryptographic-against-the-end-user.

### Clock-tampering defense

Offline-only verification trusts the system clock to compare against `expires_at`. A user who sets the clock backward to extend an expired license is a real attack vector. Two cheap layers, ~50 lines of code total:

1. **Compile-time epoch.** Each binary release embeds its build timestamp. App refuses to run if `current_time < build_epoch` — a 2027 binary cannot legitimately think the date is 2025.
2. **Monotonic state in OS-protected storage.** A signed record (Ed25519, same keypair as the license) of the highest timestamp the app has ever observed — stored in **macOS Keychain** on Apple platforms, **Windows DPAPI / Credential Manager** on Windows, and **libsecret / Secret Service** on Linux. This avoids the trivial "delete the file in `~/Library/Application Support/`" bypass that a plain on-disk state file would have. The Rust `keyring` crate (and adjacent Tauri plugins) abstracts the platform differences (~200 lines for the integration, not the underlying logic). On every launch, the app checks `current_time ≥ max(build_epoch, last_seen_time)`.

The signed record cannot be forged without the private key. Combined with the build-epoch floor and OS-keystore residence, this catches the realistic clock-rollback attack with one Ed25519 verify and two integer comparisons per launch.

**Failure UX (not a hard refuse).** When the clock check fails, the app does not silently exit or show a generic error. It shows a diagnostic screen: *"Your system clock appears to be set incorrectly — current: `$now`, expected: ≥ `$floor`."* A **Check my clock** button opens the OS date/time settings. A **Re-paste my license** button regenerates the state record from a fresh valid license, recovering legitimate fresh-install / hardware-replacement cases. Cost: one screen + two buttons. The alternative (hard refuse, no diagnostic) creates support friction and reads as user-hostile to someone whose CMOS battery genuinely died.

**Defeated only by:** evicting the OS keystore entry *and* running an older binary. Both obviously dishonest actions, both leave traces. Matches the *Anti-tamper realism* doctrine above — deter casual rollback, accept that determined bypass is possible. The deterrent against the determined bypass is contractual, not technical.

**Legitimate-use safe.** Dead CMOS battery, fresh OS reinstall, significant timezone travel, system-clock drift — none of these lock the user out. The state record regenerates on first launch with a current-time clock, and the build-epoch floor is permissive enough that a year-old binary still runs fine. No intervention required for normal clock drift.

**Out of scope for v1:** OS-trusted-time APIs (macOS `kSecTrustedTimestamp`, Windows update-channel timestamps from a signed feed), Roughtime / RFC 3161 anchored time, TPM / Secure Enclave monotonic counters, hardware-dongle real-time clocks. All viable; none necessary at the v1 threat model. Revisit in v1.5+ only if abuse data warrants the added complexity.

### What the app reports back

**Nothing.** No machine fingerprinting, no usage telemetry, no error reports, no last-seen timestamp. The app's only outbound calls are the ones enumerated in [ADR 002](002-pure-local-product-shape.md): one-time first-launch model fetch (signed manifest, SHA-pinned URLs) and OS-level update channels. The license-verification path is purely local.

If machine binding is ever needed (per the air-gapped-token pattern), the fingerprint is computed locally and embedded in the signed file at issue time — never transmitted at runtime. v1 does not bind to machines.

### Self-hostable seat-management appliance (v1.5)

A future Docker container run inside the deployment's own tenant. Speaks Keycloak / OIDC / SAML for environments that want centralized identity. Issues delegated license tokens scoped by the parent license. Out of scope for v1; recorded here so the v1 license format ships forward-compatible with it (same Ed25519 keypair, same JSON schema, additional optional fields).

### Migration path from v1 license to v1.5 appliance

v1 paste-a-key deployments receive a signed file. A v1.5 appliance issues delegated tokens against the same parent key. Existing v1 license files remain valid; the appliance does not invalidate or replace them, only adds delegation. Forward-compatibility is preserved by the schema's `schema_version` field.

## Alternatives considered

### A. Vendor licensing-as-a-service (Keygen.sh SaaS, LicenseSpring, Cryptolens)
Mature, well-documented, supports offline tokens. Places a vendor domain in the customer's trust path. **Rejected.** A self-hosted equivalent (e.g. Keygen CE, a self-hosted Rails app) is compatible with this ADR's posture; the hosted-as-a-service variant is not.

### B. Online-first activation with periodic refresh
Industry default for SaaS-desktop hybrids (JetBrains, 1Password, Figma). Refreshes every N days; revokes near-real-time. Requires phone-home; incompatible with [ADR 002](002-pure-local-product-shape.md). **Rejected.**

### C. Hardware dongles (e.g. Thales Sentinel HL)
Bulletproof for true air-gap; lossy for mobile users; cost-per-unit and logistics overhead. Niche fit (specialty CAM / EDA / CFD); over-engineered for this product's threat model. **Rejected as default.** Could be added as an optional path for classified-work deployments later.

### D. Honor-system, no enforcement (Obsidian-style)
Works for low-piracy contexts. The signed file is still wanted as a verifiable artifact and paper trail even where enforcement is light. **Rejected** in favor of "the file is the artifact, even if enforcement is light."

### E. Smart contracts / blockchain
**Rejected** without further comment.

## Consequences

### Positive
- Zero phone-home satisfies a strict air-gapped / data-sovereignty posture out of the box.
- Single portable primitive: the same mechanism serves a 5-seat paste-a-key install and a 200-seat managed deployment uploading via a self-hostable appliance.
- Renewal is "deliver a new file" — simple, predictable, low-tech.
- No vendor-side data store of "who has seats" → no subpoena target → no breach surface.
- Forward-compatible with the v1.5 self-hostable appliance and scoped-entitlement mechanics without re-cutting the v1 license shape.

### Negative
- Revocation latency = grace window (~30 days). Mitigated by the relationship, not technology.
- No usage telemetry of any kind. There is no data on which installs are active or which features are used.
- Bulk-sharing inside a single install is undetectable. Mitigated by embedded entity-name visibility and the deployment's own internal controls.
- The signing host is real infrastructure — small, but real (private host, key custody, audit log of issued licenses). One operational thing to own.

### What this changes about existing code
- New `backend/services/license_service.py` — Ed25519 signature verification, file-watch on the license path, expiry warnings.
- Service-layer entitlement gates (not UI gates) on every gated feature surface.
- New `frontend/app/pages/license.vue` (or settings section) — paste-a-key UX, license expiry banner, entity-name display.
- `app/config.json` does **not** carry the license — the license file is its own artifact, separately backed up and version-controlled by the customer if they wish.
- Onboarding flow gains a license-paste step ahead of model fetch. Existing dev / pre-license installs continue to work via a development-build flag that the production-signed builds do not honor.

## Open follow-ups (non-blocking)

1. **Signing-host implementation.** Small offline app on private infrastructure; key custody is the load-bearing operational concern, not the code. Hardware-backed key storage (YubiKey / HSM) before any production signing.
2. **License-issuing UI.** A simple internal tool to issue licenses without hand-running a CLI per license. Lightweight admin form on the signing host.
3. **Apple notarization and Authenticode signing keys.** Adjacent to license-signing keys but distinct; ensure both key custody and the per-platform signing pipeline land before v1 ships.
4. **Schema for v1.5 delegated tokens.** When the self-hostable appliance lands, the parent-license-to-delegated-token relationship needs an exact schema. Out of scope for v1 but worth sketching during the v1 license-format work to confirm forward-compatibility.
5. **Audit-log for the issuing side.** The issuing side should be able to answer "when was this license issued, against what entity, with what scope" from its own records. Append-only log on the signing host.
6. **License re-delivery story.** A lost license file can be re-requested — see *Delivery UX* above for the self-service re-delivery pattern. Identity-verification strictness is the open sub-question (item 7 below).
7. **Self-service re-delivery — identity-verification strictness.** An offline lookup against the issuance log that re-delivers the file. Open: how strict is verification? An email-only check is weak (a compromised mailbox gets the license); an email + signed challenge to a previously-registered admin contact is strongest. Pick the strictness that doesn't generate operational volume.
8. **File-extension registration (`.deepfileslic`) per platform.** macOS: UTI + `CFBundleDocumentTypes` in the bundled `.app` `Info.plist`, plus an open-file event handler. Windows: registry entries via the MSI/MSIX installer (`HKEY_CLASSES_ROOT\.deepfileslic` + ProgID + default icon). Linux (if/when shipped): `xdg-mime` registration via the `.desktop` file. Tauri supports cross-platform file associations but each target needs explicit config; ensure install / update / uninstall paths all behave (don't orphan registrations on uninstall, don't break association on auto-update). Adjacent to [ADR 003](003-desktop-distribution-tauri-and-sidecars.md).
9. **License transferability policy.** The license is not machine-bound — the same file works on a replacement laptop or a reinstall. An explicit transfer policy (entity rename/merge, seat reassignment, device replacement) is a documentation/contract concern, not a code concern; flagged here so it doesn't get missed.
10. **Soft-revocation via embedded CRL in binary updates (alternative, not for v1).** A revocation list could be embedded in each binary release — a customer's next update pulls a build that refuses to load revoked license IDs. Provides revocation without phone-home; latency = update cadence. **Currently rejected for v1** as build-pipeline complexity for a problem (post-fraud revocation) that does not yet exist. Recorded as an option for v1.5+ if the fraud-revocation use case becomes real; the schema's `license_id` field is already sufficient as the revocation key.
