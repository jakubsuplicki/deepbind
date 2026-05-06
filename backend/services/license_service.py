"""License verification — Ed25519-signed offline license file (ADR 006).

Scaffold scope (2026-04-28). Per ADR 006, this module implements the *crypto layer*:
Ed25519 sign/verify and the LicenseClaims schema. The crypto layer is
platform-independent — it works the same whether the app ships under a Tauri
shell or a dev-mode browser, so it can land before ADR 003 without locking in
fragile platform code.

What is intentionally NOT in this scaffold (each is Tauri-side or higher-level
integration that depends on prerequisites that haven't shipped):

- File-watch / load-from-disk at `~/Library/Application Support/Jarvis/license.json`
  (macOS) or `%APPDATA%\\Jarvis\\license.json` (Windows). Path resolution belongs
  to the Tauri shell that owns the app sandbox.
- Clock-rollback defense via OS-protected monotonic-state record (macOS Keychain /
  Windows DPAPI / libsecret). The verify primitive accepts a `now=` override so
  the future consumer can pass `max(system_now, build_epoch, last_seen_keystore)` —
  but the keystore integration itself is the Tauri `keyring` plugin's job.
- Compile-time build epoch floor. The production binary embeds its build timestamp;
  this scaffold runs in dev where a hard floor would just block local testing.
- Service-layer entitlement gates on paid features. Wires onto `license_service.verify_license`
  once the feature surfaces it gates exist; today's gate is "is the license valid
  and unexpired" — finer-grained `feature_flags` checks are added per surface.
- License file format on disk (`.deepfileslic` UTI registration, paste-a-key
  first-run UX). Tauri-side packaging concern.

The wire format is `signature_b64 + "." + payload_b64` where `payload_b64` is
canonical-JSON of the `LicenseClaims` (sorted keys, no whitespace). Both sign
and verify must canonicalise the same way or signatures won't match.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, Field, ValidationError


class LicenseClaims(BaseModel):
    """JSON shape per ADR 006 §"Primitive". `schema_version` enables forward
    compatibility with the v1.5 self-hostable seat-management appliance.

    `feature_flags` is intentionally narrowed to `dict[str, bool]` matching the
    ADR's example shape (`{"duel": true, "jira_ingest": true}`). If non-bool
    flags ever become required (tier strings, quotas), expand the value type
    via an explicit union — do not relax to `dict[str, Any]`, which would
    accept arbitrary JSON and create signature-stability risks via the
    canonical-JSON path.
    """
    license_id: str
    customer: str
    seat_count: int
    issued_at: str
    expires_at: str
    allowed_profiles: Optional[List[str]] = None
    feature_flags: Dict[str, bool] = Field(default_factory=dict)
    schema_version: int = 1


@dataclass(frozen=True)
class VerificationResult:
    valid: bool
    claims: Optional[LicenseClaims]
    reason: Optional[str]
    expired: bool


def generate_keypair() -> Tuple[bytes, bytes]:
    """Generate a fresh Ed25519 keypair. Returns (private, public), each 32 raw bytes.

    Used by the private signing service and by tests. Never invoked at app
    runtime — production builds embed only the public key.
    """
    private_key = Ed25519PrivateKey.generate()
    return (
        private_key.private_bytes_raw(),
        private_key.public_key().public_bytes_raw(),
    )


def sign_license(claims: LicenseClaims, private_key_bytes: bytes) -> bytes:
    """Sign a LicenseClaims payload with the private key. Returns raw signature bytes."""
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    return private_key.sign(_canonical_json(claims))


def serialize_license(claims: LicenseClaims, signature: bytes) -> str:
    """Pack to the `signature_b64.payload_b64` wire format."""
    payload = _canonical_json(claims)
    return (
        base64.b64encode(signature).decode("ascii")
        + "."
        + base64.b64encode(payload).decode("ascii")
    )


def verify_license_with_embedded_key(
    license_text: str,
    *,
    now: Optional[datetime] = None,
) -> VerificationResult:
    """Verify a license against the build-time-embedded public key.

    Production entry point. Centralises the trust root so callers cannot
    accidentally pass a different public key. The embedded key is the
    constant exposed by `services.license_public_key` (production-injected
    via the build script per ADR 019, dev fallback otherwise).
    """
    # Imported here rather than at module top to keep the crypto layer's
    # dependency surface tight — license_public_key has its own import-
    # time warning logic and we don't want to trigger it just because
    # someone imports license_service for the schema types.
    from services.license_public_key import LICENSE_PUBLIC_KEY

    return verify_license(license_text, LICENSE_PUBLIC_KEY, now=now)


def verify_license(
    license_text: str,
    public_key_bytes: bytes,
    *,
    now: Optional[datetime] = None,
) -> VerificationResult:
    """Verify a serialized license against the embedded public key.

    Returns a VerificationResult; never raises on bad input. The crypto layer
    only — keystore-backed clock-rollback defense, file path resolution, and
    feature_flags entitlement gates layer on top.

    `now` defaults to the system clock. Future Tauri-side consumer passes
    max(system_now, build_epoch, keystore_last_seen) to defeat clock rollback.
    """
    parsed = _split_wire(license_text)
    if parsed is None:
        return VerificationResult(False, None, "Malformed license text", False)
    signature, payload = parsed

    try:
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature, payload)
    except InvalidSignature:
        return VerificationResult(False, None, "Signature verification failed", False)
    except ValueError as exc:
        return VerificationResult(False, None, f"Public key invalid: {exc}", False)

    try:
        claims_dict = json.loads(payload.decode("utf-8"))
        claims = LicenseClaims(**claims_dict)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError) as exc:
        return VerificationResult(False, None, f"Payload schema invalid: {exc}", False)

    try:
        expires_at = datetime.fromisoformat(claims.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return VerificationResult(False, claims, "expires_at is not ISO 8601", False)
    # Reject naive timestamps. Silently attaching UTC would hide a
    # signing-side contract violation; the signing service must always
    # produce timezone-aware UTC (`...Z` or `...+00:00`).
    if expires_at.tzinfo is None:
        return VerificationResult(False, claims, "expires_at must be timezone-aware (UTC)", False)

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    if expires_at < current:
        return VerificationResult(False, claims, "License expired", True)

    return VerificationResult(True, claims, None, False)


def _canonical_json(claims: LicenseClaims) -> bytes:
    """Canonical JSON for signing — sorted keys, no whitespace, UTF-8 bytes.

    Constructed from explicit field references rather than `claims.model_dump()`
    so that adding a field to `LicenseClaims` requires an explicit reviewed
    update here. This forecloses the failure mode where a pydantic upgrade or a
    new optional field silently changes serialization and existing licenses
    stop verifying. Sign-side and verify-side both call this function — the
    only knob is the explicit dict below.
    """
    payload = {
        "license_id": claims.license_id,
        "customer": claims.customer,
        "seat_count": claims.seat_count,
        "issued_at": claims.issued_at,
        "expires_at": claims.expires_at,
        "allowed_profiles": claims.allowed_profiles,
        "feature_flags": claims.feature_flags,
        "schema_version": claims.schema_version,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _split_wire(license_text: str) -> Optional[Tuple[bytes, bytes]]:
    """Decode the `sig_b64.payload_b64` wire format. Returns None on any malformation."""
    if not license_text:
        return None
    parts = license_text.strip().split(".", 1)
    if len(parts) != 2:
        return None
    sig_b64, payload_b64 = parts
    try:
        signature = base64.b64decode(sig_b64, validate=True)
        payload = base64.b64decode(payload_b64, validate=True)
    except (ValueError, binascii.Error):
        return None
    return signature, payload
