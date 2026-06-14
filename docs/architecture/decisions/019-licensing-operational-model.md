# ADR 019 — Licensing operational model: auto-trial, manual renewal, read-only past-grace

**Status:** Accepted
**Date:** 2026-05-05
**Last updated:** 2026-06-14
**Related:** [ADR 002](002-pure-local-product-shape.md) · [ADR 003](003-desktop-distribution-tauri-and-sidecars.md) · [ADR 006](006-offline-signed-license.md)
**Amends:** [ADR 006 §"Activation flow"](006-offline-signed-license.md), [ADR 006 §"Expiry and renewal (technical behavior)"](006-offline-signed-license.md)

> **Reference-implementation note.** Like [ADR 006](006-offline-signed-license.md), this ADR documents the licensing subsystem as a **reference implementation of offline, server-free license verification**, not a live commercial product. It records the *operational state machine and its technical rationale* — trial/active/grace/past-grace behavior, the entitlement states, public-key embedding, and clock-tampering defense. It deliberately omits all commercial and go-to-market concerns.

## Context

ADR 006 fixed the cryptographic primitive (Ed25519-signed offline license file, no phone-home, no vendor activation server) and the high-level activation flow ("paste a key, double-click a file, or IT pre-deploys"). It did **not** fix the operational state machine around that primitive — specifically:

- How an install **starts functioning** before it has a license. ADR 006 implicitly assumed every install arrives with a key.
- Whether renewal involves **extending** a license or **replacing** it (the offline-signed primitive only supports replacement).
- What "past grace" means for a knowledge product where the canonical data store is already Markdown on disk per the project's source-of-truth doctrine.
- Where the **public key gets embedded** at build time (sidecar only? Tauri + sidecar?).
- How **trial state survives reinstall** without a server round-trip — the obvious "trial timestamp in app data" pattern lets the trial be reset by reinstalling.

These decisions were left as "open follow-ups" or "post-ADR-003 integration" in ADR 006. With the crypto layer landed and ADR 003 (Tauri shell) done, the operational shape now has to be picked.

## Decision

### Trial: 30 days, auto-starting, keychain-tracked

A fresh install with no `license.json` present is **automatically in trial mode for 30 days from first launch**. No signup, no card, no email gate — install the app, open it, use it. This is the Sublime / Beyond Compare / JetBrains pattern for offline desktop tools.

The trial-start timestamp is written to the OS keychain (macOS Keychain / Windows Credential Manager / libsecret via the Tauri `keyring` plugin) under the same blob that ADR 006 §"Clock-tampering defense" already requires for the monotonic-state record. Reinstalling the app does **not** wipe the keychain entry, so reinstall does not reset the trial. A determined user can wipe their keychain to reset the trial, but doing so destroys all their saved passwords across every app they use — a high enough cost that it deters normal users. This matches ADR 006's "anti-tamper realism" doctrine: deter casual reset, accept that determined bypass is possible.

**Trial duration: 30 days, not 14.** A knowledge product needs the user to ingest their own content and live with it before deciding. Industry comparables for offline desktop tools (JetBrains, BBEdit, Beyond Compare, Sublime) sit at 30 days or longer. 14 days is too short to evaluate the product against real content. 30 days is the right default; an extension license can be issued out-of-band for genuine extended-evaluation cases (a manual signing step, near-zero cost).

**Trial vs licensed feature parity.** A trial is *not* a crippled version. It runs the same feature set as a licensed install — the only difference is the expiry date. Crippled trials are a SaaS pattern; for an offline desktop product where the evaluation happens against the user's own data, the trial must be the real product or the evaluation doesn't transfer. This is also the simplest implementation: no `is_trial: bool` flag in `LicenseClaims`, no per-feature gate forking, no surprise behaviour at conversion.

**Trial-mode internal representation.** No special "trial state" type — internally the trial is treated as if a hypothetical license existed with `customer="Trial"`, `seat_count=1`, `expires_at=trial_start + 30d`. The entitlement state machine reads this synthesized claim through the same code path that handles real licenses. Less code, single contract.

### Renewal is replacement, not extension

The license file commits to absolute timestamps at sign time, so there is no "extend the existing license" primitive — a new validity window means a new signed `.deepfileslic` file, delivered out-of-band per ADR 006 §"Expiry and renewal", dropped into the app to replace the old one. The license is valid for 13 months (12 months entitlement + 30-day grace per ADR 006).

There is no automatic in-app renewal mechanism and no stored credential of any kind: activation is always the explicit, local act of placing a new signed file. This is the technical consequence of the offline-signed model — the architecture is shaped like discrete, explicitly-applied license files, and any auto-extension layer would fight the underlying primitive rather than work with it. It is also the data-sovereignty-consistent shape: the app holds no recurring relationship to any server.

### Activation: paste key, drop file, or first-launch wall after trial expiry

Three activation surfaces, all locally verified:

1. **Settings → License panel:** paste the `BEGIN LICENSE … END LICENSE` text block at any time during the trial to convert from trial-mode to licensed-mode immediately.
2. **`.deepfileslic` file association:** double-click the file; the OS routes it to the app, which writes it to the platform license path and re-verifies. Per ADR 006 §"Delivery UX".
3. **Trial-expired wall:** when the trial expires with no license present, the app shows a full-screen wall: "Your trial ended. Activate to continue." with a paste-key field and an [Open my data folder] affordance (so the user can reach their Markdown files even without a license).

No background license refresh, no phone-home check at activation. Verification is a local Ed25519 signature check against the embedded public key.

### Past-grace state: read-only access to data already on disk

When a license expires AND the 30-day grace window also elapses with no renewal, the app enters **past-grace mode**:

- Inference path is gated (no chat, no specialists, no retrieval, no ingest).
- Memory write path is gated (no new notes, no new memories, no memory mutations).
- **Read paths remain open.** The user can browse existing chats, view existing notes, search via Spotlight on `~/DeepBind/memory/` (the data is already on disk as Markdown per the source-of-truth doctrine). No "export" button is needed because nothing was ever trapped in a proprietary format — the canonical store is plaintext Markdown readable with any text editor.
- An [Open in Finder / Explorer] affordance is surfaced prominently on the past-grace wall, pointing at `~/DeepBind/memory/`.

This is the kindest answer compatible with the architecture, and it makes the data-sovereignty posture provable: at no point can license expiry hold the user's data hostage, because the knowledge is plaintext Markdown on disk that can be read at any time. A separate "structured export" feature can be added later if migration-to-another-tool requests appear, but it's not load-bearing for past-grace UX — the data is already exportable by virtue of being on disk.

The implementation is small because the gate model only has to gate the **active write/inference paths**, not the read paths. Read-only mode is what the read paths already do; we just block the write/inference paths.

### Entitlement state machine

Mutually-exclusive states. Computed once per launch, recomputed when a license file changes on disk or is pasted.

| State | Trigger | App behaviour | UI surface |
|---|---|---|---|
| `unlicensed_trial_active` | No license file; keychain trial-start within 30d | Full functionality | Tiny banner top-right: "Trial: N days left · [Activate]" |
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

- The sidecar already owns all crypto in this product (signature verification, the `cryptography` package, the `LicenseClaims` schema). Doubling the trust root into the Tauri binary doubles the surface for keypair rotation and re-embedding without meaningful security gain — an attacker who can forge an Ed25519 signature against the key has already won at both layers.
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

### Private-key custody (interim — HSM later)

ADR 006 §"Open follow-ups" #1 calls for HSM/YubiKey-backed key custody before any production signing. This ADR records the **interim** model: the signing host holds the Ed25519 private key on disk in its secrets store. The `signing/` shared module (a Python package — same code as `backend/scripts/sign_license.py`) is imported by the signing host and invoked to produce a license file.

This means: signing-host compromise = every license forgeable. The mitigation is HSM migration before any production signing reaches scale. Until then, the threshold for compromise is "attacker breaches the signing host," which is the same threshold for the issuance log and other host-side secrets and is not a uniquely catastrophic increment.

YubiKey 5 with PIV is the cheap migration path; cloud HSM (AWS CloudHSM / GCP Cloud HSM) is the operational migration path. Decision deferred to a future ADR.

## Trade-offs

| Choice | Benefit | Cost |
|---|---|---|
| Auto-trial without signup | Lowest-friction install path; matches Sublime/JetBrains UX | No data on who tried the product (no email captured) |
| Trial state in keychain | Survives reinstall; no server dependency | Determined users can reset by wiping keychain (acceptable per anti-tamper realism) |
| Renewal is replacement, no auto-renew | Stronger data-sovereignty posture; simpler implementation; no stored credential | A lapsed license is re-activated by applying a new file, not automatically |
| Past-grace read-only (no export) | Smallest implementation; data already on disk; provable data sovereignty | Users might expect a one-click "export everything" affordance |
| Sidecar-only public key | Single trust root; no Rust crypto port needed | If sidecar is bypassed (attacker patches the Tauri binary to skip the verify call), no second-layer check catches it |
| Signing host holds private key (interim) | Implementable now; HSM is migration, not gate | Signing-host compromise = forgeable licenses until HSM migration |

## Alternatives considered

### A. Crippled trial (limited features, no Jira ingest, max 1 workspace, etc.)
Common SaaS pattern; wrong fit for an offline knowledge product where the user has to evaluate against their own data and content. A crippled trial that doesn't show real performance can't demonstrate the product to someone who needs to see real performance. Rejected.

### B. Auto-extending / always-current license (in-app refresh with stored credential)
Would require the app to hold a recurring credential and a path to refresh the license window automatically. Strictly worse posture (stored credential, recurring relationship) for a product whose entire premise is privacy and explicit consent — the same reasoning that drove ADR 002's no-cloud constraint applies here. It also fights the offline-signed primitive, which has no extend operation. Rejected.

### C. Public key embedded in both sidecar and Tauri binary
Defense in depth. Cost: Rust port of the verification primitive (Ed25519 verify is small but not free), doubled test matrix, doubled keypair-rotation surface. Marginal benefit: only catches the case where an attacker patches the sidecar to skip verification — but a patched sidecar can also patch the Tauri-side check. Rejected as added surface without meaningful gain.

### D. Online activation server (one-time check at activation, then offline)
Considered briefly. Even a one-shot activation call adds a subpoena target ("entity X activated their license from IP Y on date Z") and an availability dependency (server down = installs can't activate). The cryptographic proof in the signed file is *strictly stronger* than a server response (signed math vs. trusted server). Rejected — the offline-signed model is already optimal for this trust posture; adding a server call is pure downside.

### E. Bundled "structured export" tool for past-grace
A button that walks the workspace and emits a clean ZIP / JSON / portable format. Possibly worth building eventually (migration-to-another-tool stories), but not load-bearing for past-grace UX because the data is already exportable by virtue of being Markdown on disk. Rejected as v1 scope; recorded as a future feature if a real migration request appears.

## Consequences

### Positive

- **Auto-trial means the install path has no signup wall.** The product can be evaluated before giving any information; activation happens when the user is ready.
- **Single state-machine module** owns all entitlement decisions; every feature surface calls into one function. No scattered license checks.
- **Public key is a single constant** in one file with a clean dev/prod separation; key rotation is a build-config change, not a code change.
- **Past-grace is humane.** Users never lose access to their data, only to the active write/inference paths. The data-sovereignty posture is provable from the architecture.
- **No recurring-credential machinery** in the app — activation is always a discrete, local, explicitly-applied signed file.

### Negative

- **No telemetry on trial usage.** There is no data on which installs tried the product, how long, or what they did. Acceptable per ADR 002's no-telemetry stance.
- **Keychain reset is a real attack vector.** Determined users can wipe the keychain to reset the trial. Mitigated by the destructive cost of doing so (loses all saved passwords across all apps), not by technical defense.
- **Lapsed licenses require an explicit new file** to re-activate. Acceptable trade for the simpler architecture and stronger posture.
- **Signing-host compromise = forgeable licenses** until HSM migration. Recorded as a known risk with a migration trigger; not architecturally clean but not blocking.

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
3. **First-run wall + Settings panel** — Vue pages for the activation wall (post-trial-expiry, post-license-expiry) and the Settings license panel (paste-a-key during trial, view license info, [Open in Finder] for past-grace).
4. **Service-layer entitlement gates** — wire `entitlements.check(...)` into every gated surface (chat send, ingest, memory write). Read paths stay open.
5. **`.deepfileslic` file association** — Tauri config + Info.plist + Windows registry. Double-click the file installs.
6. **Clock-tampering defense** — build-epoch floor + OS-keychain monotonic-state record. Per ADR 006 §"Clock-tampering defense".

Signing-host work happens in a `signing/` module (or separate repo), out of scope for this ADR's implementation chunks but architecturally specified above.

## Open follow-ups (non-blocking)

1. **HSM migration trigger.** A future ADR picks the specific HSM solution (YubiKey 5 PIV vs cloud HSM) and migration mechanics before any production signing reaches scale.
2. **Trial-extension license issuance.** A manual CLI flow exists today (run `sign_license.py` with a 60-day expiry). Worth folding into an internal issuing tool once one exists.
3. **MSA/EULA seat-entitlement clauses.** Per ADR 006 §"Open follow-ups" #9 — a documentation/contract concern, not engineering.
4. **Renewal reminder mechanism.** If a renewal-reminder surface is built, it lives outside the app (the app holds no recurring relationship to any server). Empirical decision later.
5. **Dev-keypair compromise blast radius.** The dev private key is committed to the repo for test signing. If a contributor accidentally signs a "real" license with the dev key, that license verifies in dev builds but fails in production builds (different embedded public key). This is by design — production builds explicitly reject dev-keypair-signed licenses. Worth a comment in the dev keypair file to make the intent explicit.
