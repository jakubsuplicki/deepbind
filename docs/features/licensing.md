---
title: Licensing
status: scaffold
type: feature
sources:
	- backend/services/license_service.py
	- backend/scripts/sign_license.py
	- backend/tests/test_license_service.py
depends_on: []
last_reviewed: 2026-04-28
last_updated: 2026-04-28
---

# Licensing

Ed25519-signed offline license file ([ADR 006](../architecture/decisions/006-offline-signed-license.md)).

## Status: scaffold

Today's implementation is the **crypto layer only**. Per ADR 006 + ADR 004's "Buildable today" / "Blocked by upstream ADRs" split, the integration pieces that depend on Tauri ([ADR 003](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md)) are deliberately **not** in this scaffold:

| In scaffold (this module) | Deferred (Tauri-side after ADR 003) |
|---|---|
| Ed25519 sign/verify primitive | File-watch / load-from-disk at OS-specific paths |
| `LicenseClaims` schema (per ADR 006 §"Primitive") | macOS Keychain / Windows DPAPI / libsecret monotonic-state |
| `serialize_license` / wire format | Compile-time build-epoch floor |
| `now=` override on `verify_license` | `.deepfileslic` UTI registration + first-run paste-a-key UX |
| Round-trip + tamper-rejection tests | Service-layer entitlement gates on paid features |

The crypto layer is platform-independent — it works the same regardless of which shell ships the binary. The deferred pieces are exactly the things whose security guarantee changes once a Tauri-native side exists. Building them in pure Python first would create production-grade fragile platform code we'd have to maintain forever.

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

`now=` override: the verify primitive accepts an explicit clock so the future Tauri-side consumer can pass `max(system_now, build_epoch, keystore_last_seen)` to defeat clock-rollback attacks (per ADR 006 §"Clock-tampering defense"). The keystore integration itself is the Tauri `keyring` plugin's job; this scaffold pins the contract that the override exists and is honoured.

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
| [license_service.py](../../backend/services/license_service.py) | Crypto primitive: keypair generation, sign, verify, serialize, canonical-JSON |
| [scripts/sign_license.py](../../backend/scripts/sign_license.py) | Offline signing CLI: `generate-keypair` / `sign` / `verify` subcommands |
| [test_license_service.py](../../backend/tests/test_license_service.py) | 23 tests: round-trip, expiry, tamper-rejection, malformed-input, key-mismatch, canonical-JSON field-order independence, timezone enforcement, feature_flags typing |
| [test_sign_license.py](../../backend/tests/test_sign_license.py) | 17 tests covering the CLI: keypair generation (32-byte output, 0o600 perms, refuse-to-overwrite, --overwrite flag), sign (round-trip, missing file, malformed JSON, schema failure, wrong-length key, refuse-to-overwrite), verify exit codes (0/1/2/3 split for valid/tampered/expired/input-error) |

## Gotchas

- **Public-key bytes**, not PEM. `verify_license` takes 32 raw bytes (the Ed25519 public key). Production builds embed these directly; loading from a file would re-introduce on-disk surface area we don't need.
- **`now=` is UTC.** Tauri-side consumer passes timezone-aware UTC datetimes. `verify_license` reattaches `timezone.utc` to a naive `now=` (defensive), but consumers should be explicit.
- **Naive `expires_at` is rejected.** The signing service must produce `Z` or `+00:00`. A naive ISO timestamp returns `valid=False, reason="expires_at must be timezone-aware (UTC)"` rather than being silently fixed up.
- **Expiry returns `claims` but `valid=False`.** Consumers wanting a "renewal banner with customer name" pattern read `result.claims` whether `result.valid` is True or False; consumers wanting a hard gate read `result.valid` only.
- **Canonical JSON is constructed from explicit field references**, not `model_dump()`. Adding a field to `LicenseClaims` requires an explicit reviewed update to `_canonical_json` — this forecloses the failure mode where a pydantic upgrade silently changes serialization and existing licenses stop verifying. Sign-side and verify-side both call this single function.

## Related ADRs

- [ADR 006 — Ed25519-signed offline license](../architecture/decisions/006-offline-signed-license.md) — the architecture this scaffold implements one layer of.
- [ADR 002 — Pure local product shape](../architecture/decisions/002-pure-local-product-shape.md) — the no-cloud constraint that rules out a vendor licensing-as-a-service.
- [ADR 003 — Desktop distribution: Tauri shell + PyInstaller backend + Ollama sidecar](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md) — the prerequisite for the deferred integration pieces above.
- [ADR 005 — Profile-driven model stacks](../architecture/decisions/005-profile-driven-model-stacks.md) — the `allowed_profiles` field gates which `ProfilePack`s a customer can load.
