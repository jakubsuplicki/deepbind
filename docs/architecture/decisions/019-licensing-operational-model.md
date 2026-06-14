# ADR 019 — Licensing operational model: auto-trial, manual annual renewal, read-only past-grace

**Status:** Accepted
**Date:** 2026-05-05
**Related:** [ADR 002](002-pure-local-product-shape.md) · [ADR 003](003-desktop-distribution-tauri-and-sidecars.md) · [ADR 006](006-offline-signed-license.md)
**Amends:** [ADR 006 §"Activation flow"](006-offline-signed-license.md), [ADR 006 §"Renewal"](006-offline-signed-license.md), [ADR 006 §"Billing cadence"](006-offline-signed-license.md)

## Context

ADR 006 fixed the cryptographic primitive (Ed25519-signed offline license file, no phone-home, no vendor activation server) and the high-level activation flow ("paste a key, double-click an attachment, or IT pre-deploys"). It did **not** fix the operational model around that primitive — specifically:

- How a customer **starts using the app** before they have a license. ADR 006 implicitly assumed every customer arrives with a key.
- Whether the annual renewal is **auto-charging** (subscription model) or **manual** (one-shot purchase).
- What "past grace" means for a knowledge product where the canonical data store is already Markdown on disk per the project's source-of-truth doctrine.
- Where the **public key gets embedded** at build time (sidecar only? Tauri + sidecar?).
- How **trial state survives reinstall** without a server round-trip — the obvious "trial timestamp in app data" pattern lets a customer reset the trial by reinstalling.

These decisions were left as "open follow-ups" or "post-ADR-003 integration" in ADR 006. With the crypto layer landed and ADR 003 (Tauri shell) done, the operational shape now has to be picked.

## Decision

### Trial: 30 days, auto-starting, keychain-tracked

A fresh install with no `license.json` present is **automatically in trial mode for 30 days from first launch**. No signup, no card, no email gate — install the app, open it, use it. This is the Sublime / Beyond Compare / JetBrains pattern for offline desktop tools.

The trial-start timestamp is written to the OS keychain (macOS Keychain / Windows Credential Manager / libsecret via the Tauri `keyring` plugin) under the same blob that ADR 006 §"Clock-tampering defense" already requires for the monotonic-state record. Reinstalling the app does **not** wipe the keychain entry, so reinstall does not reset the trial. A determined user can wipe their keychain to reset the trial, but doing so destroys all their saved passwords across every app they use — a high enough cost that it deters normal users. This matches ADR 006's "anti-tamper realism" doctrine: deter casual reset, accept that determined bypass is possible.

**Trial duration: 30 days, not 14.** A knowledge product needs the customer to ingest their own content and live with it before deciding. Industry comparables for offline desktop tools (JetBrains, BBEdit, Beyond Compare, Sublime) sit at 30 days or longer. For the mid-market engineering operator (20–200 seats), 14 days expires before legal/procurement review even completes. For the legal solo operator, 14 days isn't long enough to test it on a real client matter. 30 days is the right default; an extension license can be issued out-of-band for genuine enterprise eval cycles (white-glove move, near-zero cost).

**Trial vs paid feature parity.** A trial is *not* a crippled version. It runs the same feature set as a paid license — the only difference is the expiry date. Crippled trials are a SaaS pattern; for an offline desktop product where the customer has to evaluate against their own data, the trial must be the real product or the eval doesn't transfer. This is also the simplest implementation: no `is_trial: bool` flag in `LicenseClaims`, no per-feature gate forking, no surprise behaviour at conversion.

**Trial-mode internal representation.** No special "trial state" type — internally the trial is treated as if a hypothetical license existed with `customer="Trial"`, `seat_count=1`, `expires_at=trial_start + 30d`. The entitlement state machine reads this synthesized claim through the same code path that handles real licenses. Less code, single contract.

### One-shot annual purchase, no subscription

When the customer is ready to convert, they go to the web-app, pay once via Stripe Checkout (one-time charge, not a subscription), receive the `.deepfileslic` file via email, and drop it into the app. The license is valid for 13 months (12 months entitlement + 30-day grace per ADR 006 §"Renewal").

**No auto-renewal. No stored payment method. No recurring billing.** At month 11 the web-app emails a renewal reminder; the customer chooses to repurchase or not. This is a deliberate departure from ADR 006's earlier "billing is annual" framing (which left ambiguous whether annual meant auto-renew or one-shot).

Why one-shot:

1. **Compliance posture is strictly stronger.** No stored card means no PCI scope creep. The compliance pitch becomes provable: *"We don't store your payment method. You renew explicitly each year."*
2. **The customer is in control.** No subscription trap, no surprise charges, no "we forgot to cancel and got billed for another year" complaints.
3. **Architectural simplicity.** No Stripe subscription primitive, no recurring webhook handling, no "your card was declined" failure mode, no dunning emails, no payment-method-update flow. Single `checkout.session.completed` webhook handler in the web-app, one path.
4. **Matches the offline-signed model honestly.** The Ed25519 license file commits to absolute timestamps at sign time — there's no "extend the existing license" primitive without re-issuing. So the architecture is already shaped like one-shot annual purchases; auto-renewal would be a layer on top that fights the underlying primitive.

Trade-off accepted: lower lifetime value because some customers will forget to renew. The architecture's data-sovereignty pitch is worth more than the LTV delta — a forgotten renewal is just a customer who walks back when they need the tool again.

### Activation: paste key, drop file, or first-launch wall after trial expiry

Three activation surfaces, all locally verified:

1. **Settings → License panel:** paste the `BEGIN LICENSE … END LICENSE` text block at any time during the trial to convert from trial-mode to paid-mode immediately.
2. **`.deepfileslic` file association:** double-click the attachment from the welcome email; the OS routes it to the app, which writes it to the platform license path and re-verifies. Per ADR 006 §"Delivery UX".
3. **Trial-expired wall:** when the trial expires with no license present, the app shows a full-screen wall: "Your trial ended. Activate to continue." with a paste-key field, a [Buy a license] button (opens browser to web-app), and an [Open my data folder] affordance (so the customer can reach their Markdown files even without a license).

No background license refresh, no phone-home check at activation. Verification is local Ed25519 signature check against the embedded public key.

### Past-grace state: read-only access to data already on disk

When a paid license expires AND the 30-day grace window also elapses with no renewal, the app enters **past-grace mode**:

- Inference path is gated (no chat, no specialists, no retrieval, no ingest).
- Memory write path is gated (no new notes, no new memories, no memory mutations).
- **Read paths remain open.** The customer can browse existing chats, view existing notes, search via Spotlight on `~/DeepFilesAI/memory/` (the data is already on disk as Markdown per the source-of-truth doctrine). No "export" button is needed because nothing was ever trapped in our format — the canonical store is plaintext Markdown the customer can read with any text editor.
- An [Open in Finder / Explorer] affordance is surfaced prominently on the past-grace wall, pointing at `~/DeepFilesAI/memory/`.

This is the kindest answer compatible with the architecture. The data sovereignty pitch becomes provable: *"At no point can our license expiry hold your data hostage. Your knowledge is plaintext Markdown on your disk; quit at any time."* A separate "structured export" feature can be added later if migration-to-another-tool requests appear, but it's not load-bearing for past-grace UX — the data is already exportable by virtue of being on disk.

The implementation is small because the gate model only has to gate the **active write/inference paths**, not the read paths. Read-only mode is what the read paths already do; we just block the write/inference paths.

### Entitlement state machine

Five mutually-exclusive states. Computed once per launch, recomputed when a license file changes on disk or is pasted.

| State | Trigger | App behaviour | UI surface |
|---|---|---|---|
| `unlicensed_trial_active` | No license file; keychain trial-start within 30d | Full functionality | Tiny banner top-right: "Trial: N days left · [Get a license]" |
| `unlicensed_trial_expiring` | Trial-active and ≤3 days remain | Full functionality | Banner upgrades to amber: "Trial ends in N days" |
| `unlicensed_trial_expired` | No license file; keychain trial-start ≥30d ago | Hard wall — refuses to function | Full-screen activation wall |
| `licensed_active` | License file present, signature valid, not expired | Full functionality | License info visible in Settings only |
| `licensed_in_grace` | License file present, signature valid, expired ≤30d ago | Full functionality | Banner: "License expired; functioning until DATE. Renew." |
| `licensed_past_grace` | License file present, signature valid, expired >30d ago | Read-only (see above) | Past-grace wall + [Open in Finder] |
| `licensed_invalid` | License file present, signature invalid OR malformed | Hard wall — same as `unlicensed_trial_expired` | Activation wall + diagnostic ("license file invalid — re-paste") |

Any other shape (e.g. license file deleted while in `licensed_active`) recomputes to the corresponding unlicensed state.

The state machine is owned by a new `services/entitlements.py` module. Every entitled feature surface calls `entitlements.check(...)` rather than parsing license claims itself — single decision point.

### Public key embedded in sidecar only (single trust root)

The 32-byte Ed25519 public key is baked into the **Python sidecar binary** at PyInstaller build time, not into the Tauri Rust binary. Verification flows: Tauri shell reads the license file from disk, sends the text to the sidecar's `/api/license/verify` endpoint, sidecar runs `verify_license(license_text, EMBEDDED_PUBLIC_KEY)`, returns the result.

Single trust root rationale:

- The sidecar already owns all crypto in this product (signature verification, the `cryptography` package, the `LicenseClaims` schema). Doubling the trust root into the Tauri binary doubles the surface for keypair rotation and re-embedding without meaningful security gain — an attacker who can forge an Ed25519 signature against your key has already won at both layers.
- Tauri-side public-key embedding would require a Rust port of the verification primitive, doubling the test matrix on a security-critical path.
- The sidecar boundary is the existing process boundary for crypto; keep it that way.

The PyInstaller spec injects the production public key via the `JARVIS_LICENSE_PUBKEY_HEX` environment variable at build time. Production builds fail with a clear error if the env var is unset. Dev builds fall back to a committed dev keypair (whose private key is committed to the repo for test signing — explicitly NOT a production key). The dev keypair lives in `backend/services/license_dev_keys/` and exists only so tests can sign and verify end-to-end without a build-time injection step.

### Build-time injection mechanism

`desktop/scripts/build-sidecar.sh` reads `JARVIS_LICENSE_PUBKEY_HEX` (64-character hex of 32 raw bytes) before invoking PyInstaller. If set:

1. The script writes `backend/services/_license_pubkey_baked.py` containing `LICENSE_PUBLIC_KEY_HEX = "<value>"`.
2. PyInstaller picks up the file naturally via `collect_submodules("services")`.
3. After PyInstaller completes, the script removes `_license_pubkey_baked.py` so dev iteration doesn't accidentally embed a stale key.
4. `_license_pubkey_baked.py` is git-ignored.

If `JARVIS_LICENSE_PUBKEY_HEX` is **not** set:

- For dev builds (default behaviour), the script warns loudly and proceeds with the dev key.
- For builds tagged as production (`JARVIS_BUILD_PROFILE=production`), the script aborts with an error.

`services/license_public_key.py` exposes a single `LICENSE_PUBLIC_KEY: bytes` constant. Implementation:

```python
try:
    from services._license_pubkey_baked import LICENSE_PUBLIC_KEY_HEX
    LICENSE_PUBLIC_KEY = bytes.fromhex(LICENSE_PUBLIC_KEY_HEX)
except ImportError:
    # Dev fallback — matches the committed dev keypair.
    LICENSE_PUBLIC_KEY = bytes.fromhex(_DEV_PUBLIC_KEY_HEX)
```

This pattern keeps the dev fallback explicit (no silent "license verification just always returns valid in dev") and makes the production injection a real environmental promotion, not a code change.

### Web-app server holds the private key (interim — HSM later)

ADR 006 §"Open follow-ups" #1 calls for HSM/YubiKey-backed key custody before the first paid customer. This ADR records the **interim** model: the web-app server (`web-app/` folder once it exists) holds the Ed25519 private key on disk in its secrets store. The `signing/` shared module (a Python package — same code as `backend/scripts/sign_license.py`) is imported by the web-app's webhook handler and invoked synchronously when a Stripe payment succeeds.

This means: web-app server compromise = every license forgeable. The mitigation is HSM migration before the customer base passes ~50 paying customers, OR before the first customer with a contract value above ~$50k/year, whichever comes first. Until then, the threshold for compromise is "attacker breaches the web-app server" which is already the threshold for many other security failures (e.g., billing data, customer email addresses) and is not a uniquely catastrophic increment.

YubiKey 5 with PIV is the cheap migration path; cloud HSM (AWS CloudHSM / GCP Cloud HSM) is the operational migration path. Decision deferred to a future ADR when the customer threshold approaches.

## Trade-offs

| Choice | Benefit | Cost |
|---|---|---|
| Auto-trial without signup | Lowest-friction install path; matches Sublime/JetBrains UX | We have zero data on who tried the product (no email captured) until they convert |
| Trial state in keychain | Survives reinstall; no server dependency | Determined users can reset by wiping keychain (acceptable per anti-tamper realism) |
| One-shot annual, no auto-renew | Stronger compliance pitch; simpler implementation; customer trust | Lower LTV from forgotten renewals |
| Past-grace read-only (no export) | Smallest implementation; data already on disk; provable data sovereignty | Customers might expect a one-click "export everything" affordance |
| Sidecar-only public key | Single trust root; no Rust crypto port needed | If sidecar is bypassed (attacker patches the Tauri binary to skip the verify call), no second-layer check catches it |
| Web-app server holds private key (interim) | Implementable now; HSM is migration, not gate | Web-app server compromise = forgeable licenses until HSM migration |

## Alternatives considered

### A. Card-required trial (Stripe SetupIntent at signup, auto-charge at day 30)
Higher trial→paid conversion (~60-70% vs ~10-20% for no-card trials), but adds the entire subscription primitive (Stripe subscriptions, recurring webhooks, dunning, payment-method-update flow) and stores card data we don't otherwise need. Rejected as too much architectural cost for the conversion delta. Auto-trial without card matches the offline-desktop-tool norm.

### B. Crippled trial (limited features, no Jira ingest, max 1 workspace, etc.)
Common SaaS pattern; wrong fit for an offline knowledge product where the customer has to evaluate against their own data and content. A crippled trial that doesn't show real performance can't convert an operator who needs to see real performance. Rejected.

### C. Auto-renewal with stored card
Industry default for SaaS-desktop hybrids. Strictly worse compliance posture (stored card, recurring billing relationship) for a customer base that explicitly values privacy and explicit consent. Rejected — the same reasoning that drove ADR 002's no-cloud constraint applies to billing.

### D. Public key embedded in both sidecar and Tauri binary
Defense in depth. Cost: Rust port of the verification primitive (Ed25519 verify is small but not free), doubled test matrix, doubled keypair-rotation surface. Marginal benefit: only catches the case where an attacker patches the sidecar to skip verification — but a patched sidecar can also patch the Tauri-side check. Rejected as added surface without meaningful gain.

### E. Online activation server (one-time check at activation, then offline)
Considered briefly. Even a one-shot activation call adds a subpoena target ("Customer X activated their license from IP Y on date Z") and an availability dependency (server down = customers can't activate). The cryptographic proof in the signed file is *strictly stronger* than a server response (signed math vs. trusted server). Rejected — the offline-signed model is already optimal for this trust posture; adding a server call is pure downside.

### F. Bundled "structured export" tool for past-grace
A button that walks the workspace and emits a clean ZIP / JSON / portable format. Possibly worth building eventually (migration-to-another-tool stories), but not load-bearing for past-grace UX because the data is already exportable by virtue of being Markdown on disk. Rejected as v1 scope; recorded as a future feature if a real migration request appears.

## Consequences

### Positive

- **Auto-trial means the install funnel doesn't have a signup wall.** Customers can evaluate the product before giving any information; conversion happens when they're ready, not when we make them sign up.
- **Single state-machine module** owns all entitlement decisions; every feature surface calls into one function. No scattered license checks.
- **Public key is a single constant** in one file with a clean dev/prod separation; key rotation is a build-config change, not a code change.
- **Past-grace is humane.** Customers never lose access to their data, only to the active write/inference paths. The data sovereignty pitch is provable from the architecture.
- **No subscription primitive** means the web-app is a one-page Stripe Checkout integration plus a webhook handler, not a full billing system.

### Negative

- **No telemetry on trial usage.** We don't know which customers tried the product, how long they used it, or what they did during the trial. Conversion analysis is blind. Acceptable per ADR 002's no-telemetry stance — sales conversations and renewal patterns are the data source.
- **Keychain reset is a real attack vector.** Determined users can wipe the keychain to reset the trial. Mitigated by the destructive cost of doing so (loses all saved passwords across all apps), not by technical defense.
- **Forgotten renewals = lost LTV.** Some annual customers will let their license lapse and not return. Acceptable trade for the simpler architecture and stronger compliance pitch.
- **Web-app server compromise = forgeable licenses** until HSM migration. Recorded as a known risk with a migration trigger; not architecturally clean but not blocking.

### What this changes about existing code

- New `backend/services/license_public_key.py` — exposes `LICENSE_PUBLIC_KEY` constant with prod-injection / dev-fallback pattern.
- New `backend/services/entitlements.py` — the entitlement state machine.
- New `backend/services/license_dev_keys/` — dev keypair (private + public) committed for test signing.
- Updated `desktop/scripts/build-sidecar.sh` — handles `JARVIS_LICENSE_PUBKEY_HEX` env var, writes/cleans `_license_pubkey_baked.py`.
- Updated `desktop/sidecar/jarvis-sidecar.spec` — picks up `_license_pubkey_baked.py` via the existing `collect_submodules("services")` (no spec-level change needed beyond confirming inclusion).
- New Tauri command + new sidecar endpoint `/api/license/state` that returns the entitlement state for the frontend.
- New Vue page `frontend/app/pages/license.vue` — paste-a-key wall, plus settings panel for license info.
- New Tauri Rust module — file-watch on the platform license path, reads to memory, calls sidecar verify endpoint.
- `.deepfileslic` UTI registration in `desktop/src-tauri/tauri.conf.json` + macOS `Info.plist` + Windows registry.
- Build-epoch floor + keychain monotonic-state record per ADR 006 §"Clock-tampering defense" (chunk 6 of the implementation sequence).

## Implementation chunks (sequencing aid)

Per the project's engineering principles, each chunk is architecturally complete on its own — no "passive surface now / active behavior later" splits.

1. **Build-time public-key embedding** — sidecar embeds the public key, dev fallback, build-script injection. Tests cover both paths. (No UI yet — this is the trust root.)
2. **License load + verify at startup** — Tauri reads platform license path, calls sidecar verify endpoint, exposes entitlement state to frontend. New `services/entitlements.py` owns the state machine. Includes the keychain trial-start mechanism.
3. **First-run wall + Settings panel** — Vue pages for the activation wall (post-trial-expiry, post-paid-expiry) and the Settings license panel (paste-a-key during trial, view license info, [Open in Finder] for past-grace).
4. **Service-layer entitlement gates** — wire `entitlements.check(...)` into every gated surface (chat send, ingest, memory write). Read paths stay open.
5. **`.deepfileslic` file association** — Tauri config + Info.plist + Windows registry. Double-click the attachment installs.
6. **Clock-tampering defense** — build-epoch floor + OS-keychain monotonic-state record. Per ADR 006 §"Clock-tampering defense".

Web-app and signing-service work happens in `web-app/` and `signing/` folders (or separate repos), out of scope for this ADR's implementation chunks but architecturally specified above.

## Open follow-ups (non-blocking)

1. **HSM migration trigger.** Defined above as "before ~50 paying customers OR first ≥$50k/year contract, whichever first." Future ADR picks the specific HSM solution (YubiKey 5 PIV vs cloud HSM) and migration mechanics.
2. **Trial-extension license issuance.** Manual CLI flow exists today (run `sign_license.py` with a 60-day expiry). Worth folding into the sales-facing UI in the web-app once that exists.
3. **Pricing.** `$X/seat/year` not set. Affects checkout copy and email templates, not the licensing primitive. Decision-maker: business-side, not architecture.
4. **MSA/EULA seat-entitlement clauses.** Per ADR 006 §"Open follow-ups" #9 — lawyer engagement at MSA finalization.
5. **Renewal reminder cadence.** Month-11 single email is the default; multi-touch sequence (T-30, T-7, T-day) may convert better. Empirical decision after first cohort renews.
6. **Dev-keypair compromise blast radius.** The dev private key is committed to the repo for test signing. If a contributor accidentally signs a "real" license with the dev key and ships it to a customer, that license verifies in dev builds but fails in production builds (different embedded public key). This is by design — production builds explicitly reject dev-keypair-signed licenses. Worth a comment in the dev keypair file to make the intent explicit.
