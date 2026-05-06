---
title: Licensing
status: current
type: feature
sources:
	- backend/services/license_service.py
	- backend/services/license_public_key.py
	- backend/services/build_epoch.py
	- backend/services/entitlements.py
	- backend/services/entitlement_gate.py
	- backend/routers/license.py
	- backend/scripts/sign_license.py
	- backend/tests/test_license_service.py
	- backend/tests/test_license_public_key.py
	- backend/tests/test_sign_license.py
	- backend/tests/test_entitlements.py
	- backend/tests/test_routers_license.py
	- backend/tests/test_entitlement_gate.py
	- backend/tests/test_clock_tampering.py
	- backend/tests/fixtures/license_dev_keys/
	- desktop/src-tauri/src/license.rs
	- desktop/src-tauri/src/lib.rs
	- desktop/src-tauri/Cargo.toml
	- desktop/src-tauri/capabilities/default.json
	- desktop/src-tauri/tauri.conf.json
	- desktop/scripts/build-sidecar.sh
	- frontend/app/composables/useLicenseState.ts
	- frontend/app/components/license/LicenseWall.vue
	- frontend/app/components/license/TrialBanner.vue
	- frontend/app/components/settings/LicenseSection.vue
	- frontend/app/layouts/default.vue
	- frontend/app/utils/apiUrl.ts
depends_on: []
last_reviewed: 2026-05-05
last_updated: 2026-05-05
---

# Licensing

Ed25519-signed offline license file ([ADR 006](../architecture/decisions/006-offline-signed-license.md)) with operational model per [ADR 019](../architecture/decisions/019-licensing-operational-model.md).

## Status: current — all 6 chunks landed

Implementation per [ADR 019 §"Implementation chunks"](../architecture/decisions/019-licensing-operational-model.md):

| Chunk | What | Status |
|---|---|---|
| 1 | Build-time public-key embedding (sidecar) | ✅ Landed 2026-05-05 |
| 2 | License load + verify at startup (Tauri ↔ sidecar), keychain trial-start | ✅ Landed 2026-05-05 |
| 3 | Entitlement state machine (`services/entitlements.py`) | ✅ Landed 2026-05-05 |
| 4 | First-run wall + Settings panel UI + service-layer gates | ✅ Landed 2026-05-06 |
| 5 | `.deepfileslic` file association | ✅ Landed 2026-05-06 |
| 6 | Clock-tampering defense (build-epoch + keychain monotonic state) | ✅ Landed 2026-05-06 |

End-to-end works: customer downloads app → 30-day trial auto-starts (keychain-tracked, survives reinstall) → trial-active banner with countdown → trial-expiring amber banner ≤3 days → trial-expired wall → paste a `.deepfileslic` (or double-click the email attachment) → license-active → 30-day grace post-expiry → past-grace read-only wall with "Open in Finder" affordance → renew license → license-active again. Clock rollback past `max(build_epoch, monotonic_floor)` triggers a separate `clock_invalid` wall pointing the user at OS date settings.

## How It Works

### Wire format

A serialized license is `signature_b64 + "." + payload_b64`, where `payload_b64` is the base64 of the canonical JSON serialization of a `LicenseClaims` (sorted keys, no whitespace, UTF-8). Both sign and verify must produce the same canonical bytes from the same claims or signatures won't match.

## How It Works

### Wire format

A serialized license is `signature_b64 + "." + payload_b64`, where `payload_b64` is the base64 of the canonical JSON serialization of a `LicenseClaims` (sorted keys, no whitespace, UTF-8). Both sign and verify must produce the same canonical bytes from the same claims or signatures won't match.

### `LicenseClaims`

JSON shape per [ADR 006 §"Primitive"](../architecture/decisions/006-offline-signed-license.md):

```python
license_id: str               # opaque id, e.g. "lic_2026_04_27_xyz"
customer: str                 # embedded customer name (deterrent against bulk sharing)
seat_count: int               # honor-system; informational
issued_at: str                # ISO 8601 UTC, must be timezone-aware (Z or +00:00)
expires_at: str               # ISO 8601 UTC, must be timezone-aware (naive rejected)
allowed_profiles: list[str] | None  # null = any profile; or [...] for SKU-scoped
feature_flags: dict[str, bool]      # { "duel": true, "jira_ingest": true, ... }
schema_version: int = 1       # forward compat with v1.5 self-hostable appliance
```

`feature_flags` is intentionally narrowed to `dict[str, bool]` matching ADR 006's example. Pydantic rejects non-bool values at construction time. If non-bool flags ever become required (tier strings, quotas), expand the value type via an explicit union — do not relax to `dict[str, Any]`, which would accept arbitrary JSON and create signature-stability risks via the canonical-JSON path.

`expires_at` must be timezone-aware. The `Z` suffix and explicit `+00:00` offset are accepted; naive ISO timestamps are **rejected** so a signing-side contract violation surfaces immediately instead of being silently fixed up.

### `verify_license(license_text, public_key_bytes, *, now=None) -> VerificationResult`

Returns a `VerificationResult` with `valid: bool`, `claims: LicenseClaims | None`, `reason: str | None`, `expired: bool`. Never raises on bad input — malformed text, bad base64, schema mismatch, signature failure, and expiry all return a structured result.

`now=` override: the verify primitive accepts an explicit clock so the future Tauri-side consumer can pass `max(system_now, build_epoch, keystore_last_seen)` to defeat clock-rollback attacks (per ADR 006 §"Clock-tampering defense"). The keystore integration itself is the Tauri `keyring` plugin's job; the chunk-1 scaffold pins the contract that the override exists and is honoured.

### `verify_license_with_embedded_key(license_text, *, now=None) -> VerificationResult`

Production entry point — wraps `verify_license` with the build-time-embedded public key (`services.license_public_key.LICENSE_PUBLIC_KEY`). Centralises the trust root so callers cannot accidentally pass a different public key. All app-side license checks (Tauri ↔ sidecar, the chunk-2 work) call through this wrapper rather than `verify_license` directly.

### Service-layer entitlement gate (chunk 4, ADR 019)

`services/entitlement_gate.py` exposes a single FastAPI dependency:

```python
from services.entitlement_gate import require_functional

@router.post("/message", dependencies=[Depends(require_functional)])
async def http_chat_message(request: Request) -> dict:
    ...
```

When `entitlements.is_functional()` returns False the dependency raises HTTP 403 with body `{"detail": {"detail": "license_required", "state": <full state dict>}}`. The frontend treats any 403 with `detail.detail == "license_required"` as "show the wall" — though the wall is already up before the user can hit a gated endpoint, the gate is **defence in depth** for power-users hitting the API directly or for the brief window between state changes.

Read paths (GET, list, search) deliberately do not declare the gate, so past-grace mode (read-only) leaves them open.

Wired onto: `chat.py` POST `/message`; `memory.py` POST/PATCH/DELETE `/notes/*` + POST `/ingest` + POST `/ingest-url` + POST `/enrich/*`; `jira.py` POST `/import`; `connections.py` POST `/run/*` + `/dismiss` + `/promote` + `/backfill` + `/promote-bulk`; `enrichment.py` POST `/rerun` + `/sharpen-all`. The `license/state` endpoints are explicitly never gated (otherwise the frontend couldn't recover from `licensed_invalid`).

`JARVIS_LICENSE_GATE_BYPASS=1` env var disables the gate (no-op). Set automatically by `tests/conftest.py` so existing tests don't have to wire license context. Production builds must not set this var; `JARVIS_BUILD_PROFILE=production` enforcement is a future hardening item.

### Frontend UI (chunk 4, ADR 019)

Three layers driven by the `useLicenseState` composable (which seeds from the `__JARVIS_LICENSE_STATE__` boot global, then refreshes via the Tauri `license_get_state` command):

- `TrialBanner.vue` — visible during `unlicensed_trial_active`, `unlicensed_trial_expiring`, `licensed_in_grace`. Slim top-of-view countdown + CTA to Settings → License.
- `LicenseWall.vue` with three variants:
  - `activation` — trial-expired or licensed-invalid. Full-screen modal with paste-a-key form + "Buy a license" link.
  - `past-grace` — license expired past 30-day grace. Same paste-a-key + a prominent "Open my data folder" button (the data is plaintext Markdown on disk; ADR 019 §"Past-grace state").
  - `clock-invalid` — diagnostic UI pointing at OS date settings; paste-a-key kept available for the rare dead-CMOS recovery case (per ADR 006 §"Failure UX").
- `LicenseSection.vue` — always visible in Settings → License. Shows current state pill, customer, expiry, license_id, paste-a-key form, "Reset license" button.

The wall, banner, and Settings section all share one composable so any state change reflects everywhere immediately (paste a key in Settings → wall vanishes; clear license → wall reappears).

The layout (`layouts/default.vue`) listens for `license:file_opened` Tauri events and pipes them into the same `installFromText` flow as the paste-a-key form — chunk 5 (`.deepfileslic` association) just emits an event and reuses chunk-4 plumbing.

### `.deepfileslic` file association (chunk 5, ADR 019)

`tauri.conf.json` declares the file association:

```json
"fileAssociations": [{
  "ext": ["deepfileslic"],
  "name": "DeepFilesAI License",
  "description": "DeepFilesAI license file (Ed25519-signed, ADR 006)",
  "role": "Editor",
  "mimeType": "application/x-deepfilesai-license"
}]
```

Tauri's bundler turns this into the platform-native registration: `CFBundleDocumentTypes` + `UTExportedTypeDeclarations` in macOS `Info.plist`, registry entries in the Windows MSI/MSIX. Double-clicking a `.deepfileslic` attachment in Mail / Outlook / Finder routes the file to the running (or freshly-launched) DeepFilesAI app.

The `RunEvent::Opened { urls }` handler in `src/lib.rs` reads the file content synchronously and emits a `license:file_opened` event with the raw text; the layout listens, calls `license.installFromText(text)`, and the existing wall/banner reactions take it from there. No new endpoint, no new validation surface — same flow as the paste-a-key form.

### Clock-tampering defense (chunk 6, ADR 019 / ADR 006 §"Clock-tampering defense")

Two layers protect against "set the clock backward to extend an expired license":

**1. Build epoch.** `desktop/scripts/build-sidecar.sh` writes `services/_build_epoch_baked.py` containing `BUILD_EPOCH_ISO = "<utc-now-at-build>"` before invoking PyInstaller and removes it post-build via the EXIT trap. `services/build_epoch.py` exposes `BUILD_EPOCH: datetime` as the constant the entitlement state machine reads. Dev builds fall back to `2020-01-01T00:00:00Z` (a no-op floor) and emit a warning at import.

**2. Monotonic state.** The OS keychain stores a `monotonic_floor` ISO timestamp (separate keyring key from `trial_started_at`, same service `com.deepfilesai.desktop`). The Tauri shell reads it on every refresh, posts it alongside the license + trial inputs, and writes back the response's `effective_now` if it advances. Survives app reinstall — same protection as the trial-start mechanism.

`entitlements.effective_now(*, monotonic_floor, system_now)` returns `max(system_now, BUILD_EPOCH, monotonic_floor)` — used as the clock by every entitlement computation.

`entitlements.is_clock_rolled_back(...)` returns True when `system_now` is more than `CLOCK_ROLLBACK_TOLERANCE` (5 minutes) behind the floor. When True, the state machine returns `clock_invalid` regardless of license/trial — a wall state that surfaces a diagnostic UI ("Your system clock appears to be set incorrectly") with a path back to the OS date settings + a paste-a-key form for the rare CMOS-battery-died recovery case (ADR 006 §"Failure UX (not a hard refuse)").

### Entitlement state machine (chunk 3, ADR 019)

`services/entitlements.py` is the single decision point for license-aware features. Seven mutually-exclusive states per ADR 019:

| State | When | App functional? |
|---|---|---|
| `unlicensed_trial_active` | No license; trial within 30d | yes |
| `unlicensed_trial_expiring` | Trial active, ≤3 days remain | yes |
| `unlicensed_trial_expired` | No license; trial >30d ago | no (wall) |
| `licensed_active` | License valid, not expired | yes |
| `licensed_in_grace` | License valid, expired ≤30d ago | yes |
| `licensed_past_grace` | License valid, expired >30d ago | read-only |
| `licensed_invalid` | License present but signature/format bad | no (wall) |

The module is **stateless except for a tiny in-process input cache**: the Tauri shell pushes the latest `(license_text, trial_started_at)` via `POST /api/license/state`, and `entitlements` recomputes the state on every call. This means clock-rollover (e.g., trial expiring at midnight) is naturally reflected on the next call without a Tauri-side push.

API:

- `compute_state(license_text, trial_started_at, *, now=None) -> EntitlementState` — pure function, no side effects.
- `set_inputs(*, license_text, trial_started_at)` — push from the Tauri shell.
- `current_state(*, now=None) -> EntitlementState` — recompute from cached inputs.
- `is_functional(*, now=None) -> bool` — chunk-4 service-layer gates call this.
- `is_read_only(*, now=None) -> bool` — distinguishes past-grace (read paths allowed) from wall states.

Constants: `TRIAL_DAYS = 30`, `TRIAL_EXPIRING_THRESHOLD_DAYS = 3`, `GRACE_DAYS = 30`. Per ADR 019 the trial duration is 30 days (not 14) — knowledge-product evals need real time.

### License HTTP API (chunk 2, ADR 019)

`routers/license.py` exposes two endpoints, both consumed by the Tauri shell:

- **`POST /api/license/state`** — body `{license_text?: str, trial_started_at?: ISO8601}`. Both fields nullable. Updates the in-process cache + returns the computed state. The shell calls this on launch, on file-change events, and when the user pastes a key.
- **`GET /api/license/state`** — recomputes from the cached inputs, returns the current state. Cheap; the frontend may poll if needed.

The backend never reads the license file from disk and never reads the OS keychain — those are the Tauri shell's responsibilities. This keeps the FastAPI process pure-data-in / pure-state-out.

`trial_started_at` validation enforces UTC-aware ISO 8601 (`Z` or `+00:00`); naive timestamps are rejected with HTTP 422 to match the same contract `LicenseClaims.expires_at` enforces. Mirrors the signing-side discipline.

### Tauri shell adapter (chunk 2, ADR 019)

`desktop/src-tauri/src/license.rs` owns the two storage surfaces the sidecar deliberately doesn't:

- **License file** at Tauri's `app_data_dir()/license.json` — read on launch + on every refresh, written when the user pastes a key (atomic write via tmp-file + rename).
- **Trial-start timestamp** in the OS keychain (macOS Keychain / Windows Credential Manager / Linux Secret Service) under service `com.deepfilesai.desktop`, key `trial_started_at`. Survives app reinstall — the whole reason for using the keychain over a plain file (ADR 019 §"Trial state must persist across reinstalls").

Four frontend-callable Tauri commands:

| Command | Purpose |
|---|---|
| `license_get_state()` | Read current state, refreshing from disk + keychain. Used on App.vue boot. |
| `license_install_text(text)` | Paste-a-key flow. Validates with sidecar; if valid, atomically writes to disk and returns the new state. If invalid, returns the diagnostic without touching disk. |
| `license_clear()` | Settings → "Reset license". Deletes the file, returns the recomputed state (typically drops back to trial-active or trial-expired depending on keychain history). |
| `license_open_data_folder(path)` | Past-grace "Open in Finder/Explorer" affordance. The path is provided by the caller — the shell stays dumb about workspace location. |

A boot-time probe (`boot_state_blocking`) reads the state once before window creation and inlines it into the WebView via `window.__JARVIS_LICENSE_STATE__` so the first paint can decide between trial-banner / wall / settings without a network round-trip. Errors fall back to `null` so the frontend can boot into a "state unknown" mode rather than failing to launch.

The keyring crate is added at `desktop/src-tauri/Cargo.toml` with `apple-native` / `windows-native` / `linux-native-sync-persistent` features — native backends only, no in-memory fallback (the in-memory backend would defeat reinstall-survival).

`shell:allow-open` capability added to `capabilities/default.json` for the data-folder open command.

### Build-time public-key embedding (chunk 1, ADR 019)

`services/license_public_key.py` exposes `LICENSE_PUBLIC_KEY: bytes` — the 32-byte Ed25519 public key against which production-issued licenses verify. Resolution order:

1. **Production:** if `services/_license_pubkey_baked.py` exists with `LICENSE_PUBLIC_KEY_HEX = "<64 hex chars>"`, that key is used. The build script writes this file from the `JARVIS_LICENSE_PUBKEY_HEX` env var before invoking PyInstaller and removes it post-build (see [`desktop/scripts/build-sidecar.sh`](../../desktop/scripts/build-sidecar.sh)).
2. **Dev / pytest fallback:** if the baked module is absent, the constant `_DEV_PUBLIC_KEY_HEX` in `license_public_key.py` is used. The matching dev *private* key lives at [`backend/tests/fixtures/license_dev_keys/private.key`](../../backend/tests/fixtures/license_dev_keys/) for test signing — explicitly NOT a production key. A license signed with the dev private key verifies in dev/pytest and *fails* in production builds (different embedded public key).

Production builds with `JARVIS_BUILD_PROFILE=production` set but no `JARVIS_LICENSE_PUBKEY_HEX` env var **abort** — the build script refuses to ship a "production" sidecar with the dev fallback key. Dev builds without the env var emit a warning at sidecar startup so accidentally-shipped dev binaries are loud about it.

Malformed injections (wrong length, not hex, missing constant) **raise** at sidecar startup rather than silently downgrading to the dev key — the silent downgrade would be a security regression.

### `sign_license` and `generate_keypair`

`sign_license(claims, private_key_bytes) -> bytes` is invoked by the **private signing service** ([ADR 006 §"Activation flow"](../architecture/decisions/006-offline-signed-license.md)) — never at app runtime. Production builds embed only the public key. `generate_keypair() -> (private, public)` is exposed for tests and for the (out-of-scope) signing-service tooling.

### Behaviour on expired licenses

An expired license fails `valid` but **still parses claims**. The UI consumer needs the customer name and `license_id` to render a renewal prompt — losing the claims on expiry would force a "blank renewal screen" UX. `result.expired` is set so the consumer can distinguish expiry from signature failure.

### Signing CLI

[`scripts/sign_license.py`](../../backend/scripts/sign_license.py) is the offline signing tool that runs on the **private signing service** side — never bundled with the shipping app. Three subcommands:

```bash
# One-time keypair generation (keep private.key offline).
python -m scripts.sign_license generate-keypair --out-dir ./keys

# Per-customer: sign a claims.json into a serialized license.
python -m scripts.sign_license sign \
    --claims customer-acme.json \
    --private-key ./keys/private.key \
    --out customer-acme.lic

# Round-trip check before delivery.
python -m scripts.sign_license verify \
    --license customer-acme.lic \
    --public-key ./keys/public.key
```

Per ADR 006 §"Activation flow", the public key bytes (32 raw) are embedded in the production binary at compile time — `public.key` from this CLI is the input to that embedding step. The private key never leaves the signing service host.

Defensive details:
- Key files are written with `0o600` permissions on POSIX (best-effort; silently skipped on Windows).
- `--overwrite` is required to replace existing keypair / license files. Defaults to refusing rather than silently clobbering an in-use private key.
- Distinct exit codes per code-review hardening (2026-04-28): `0` valid · `1` signature/tampered · `2` expired (claims still parseable for renewal UI) · `3` input error (missing files, malformed JSON, wrong-length keys, refused overwrite). Operators scripting renewal flows can branch on the difference between "expired" and "tampered" without parsing stderr.

## Key Files

| File | Purpose |
|------|---------|
| [license_service.py](../../backend/services/license_service.py) | Crypto primitive: keypair generation, sign, verify, serialize, canonical-JSON. `verify_license_with_embedded_key` is the production entry point. |
| [license_public_key.py](../../backend/services/license_public_key.py) | Single trust root: exposes `LICENSE_PUBLIC_KEY: bytes`. Production-injected via `_license_pubkey_baked` (build-script-written), dev fallback otherwise. |
| [scripts/sign_license.py](../../backend/scripts/sign_license.py) | Offline signing CLI: `generate-keypair` / `sign` / `verify` subcommands |
| [build-sidecar.sh](../../desktop/scripts/build-sidecar.sh) | Build script. Reads `JARVIS_LICENSE_PUBKEY_HEX` env var, writes `_license_pubkey_baked.py` before PyInstaller, removes it post-build via EXIT trap. |
| [test_license_service.py](../../backend/tests/test_license_service.py) | 23 tests: round-trip, expiry, tamper-rejection, malformed-input, key-mismatch, canonical-JSON field-order independence, timezone enforcement, feature_flags typing |
| [test_license_public_key.py](../../backend/tests/test_license_public_key.py) | 11 tests: dev-constant-vs-fixture consistency, dev keypair round-trip, embedded-key resolution, foreign-signature rejection, production-injection override, malformed-injection rejection (wrong length / non-hex / missing constant), dev-fallback warning emission + suppression |
| [test_sign_license.py](../../backend/tests/test_sign_license.py) | 17 tests covering the CLI: keypair generation (32-byte output, 0o600 perms, refuse-to-overwrite, --overwrite flag), sign (round-trip, missing file, malformed JSON, schema failure, wrong-length key, refuse-to-overwrite), verify exit codes (0/1/2/3 split for valid/tampered/expired/input-error) |
| [tests/fixtures/license_dev_keys/](../../backend/tests/fixtures/license_dev_keys/) | Dev keypair (private + public, 32 raw bytes each) for test signing. Matching public key is hardcoded in `license_public_key.py`; consistency asserted by `test_dev_constant_matches_fixture`. NOT a production secret. |

## Gotchas

- **Public-key bytes**, not PEM. `verify_license` takes 32 raw bytes (the Ed25519 public key). Production builds embed these directly; loading from a file would re-introduce on-disk surface area we don't need.
- **`now=` is UTC.** Tauri-side consumer passes timezone-aware UTC datetimes. `verify_license` reattaches `timezone.utc` to a naive `now=` (defensive), but consumers should be explicit.
- **Naive `expires_at` is rejected.** The signing service must produce `Z` or `+00:00`. A naive ISO timestamp returns `valid=False, reason="expires_at must be timezone-aware (UTC)"` rather than being silently fixed up.
- **Expiry returns `claims` but `valid=False`.** Consumers wanting a "renewal banner with customer name" pattern read `result.claims` whether `result.valid` is True or False; consumers wanting a hard gate read `result.valid` only.
- **Canonical JSON is constructed from explicit field references**, not `model_dump()`. Adding a field to `LicenseClaims` requires an explicit reviewed update to `_canonical_json` — this forecloses the failure mode where a pydantic upgrade silently changes serialization and existing licenses stop verifying. Sign-side and verify-side both call this single function.

## Related ADRs

- [ADR 006 — Ed25519-signed offline license](../architecture/decisions/006-offline-signed-license.md) — the underlying crypto architecture.
- [ADR 019 — Licensing operational model](../architecture/decisions/019-licensing-operational-model.md) — auto-trial + manual annual renewal + read-only past-grace + sidecar-only public key + build-time injection. Drives chunks 1–6 of this feature.
- [ADR 002 — Pure local product shape](../architecture/decisions/002-pure-local-product-shape.md) — the no-cloud constraint that rules out a vendor licensing-as-a-service.
- [ADR 003 — Desktop distribution: Tauri shell + PyInstaller backend + Ollama sidecar](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md) — the prerequisite for chunks 2–6.
