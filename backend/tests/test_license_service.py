"""Tests for the Ed25519 license-verification scaffold (ADR 006 crypto layer)."""

import base64
from datetime import datetime, timezone

import pytest

from services.license_service import (
    LicenseClaims,
    VerificationResult,
    generate_keypair,
    serialize_license,
    sign_license,
    verify_license,
)


def _make_claims(
    license_id: str = "lic_test_001",
    customer: str = "Acme Engineering Pty Ltd",
    seat_count: int = 20,
    issued_at: str = "2026-04-27T00:00:00Z",
    expires_at: str = "2027-05-27T00:00:00Z",
    allowed_profiles=None,
    feature_flags=None,
) -> LicenseClaims:
    return LicenseClaims(
        license_id=license_id,
        customer=customer,
        seat_count=seat_count,
        issued_at=issued_at,
        expires_at=expires_at,
        allowed_profiles=allowed_profiles,
        feature_flags=feature_flags or {},
    )


class TestKeypairGeneration:
    def test_keypair_is_32_bytes_each(self):
        priv, pub = generate_keypair()
        assert len(priv) == 32
        assert len(pub) == 32

    def test_each_call_produces_different_keypair(self):
        priv1, pub1 = generate_keypair()
        priv2, pub2 = generate_keypair()
        assert priv1 != priv2
        assert pub1 != pub2


class TestRoundTrip:
    def test_sign_then_verify_succeeds(self):
        priv, pub = generate_keypair()
        claims = _make_claims()
        sig = sign_license(claims, priv)
        text = serialize_license(claims, sig)

        result = verify_license(text, pub)

        assert result.valid is True
        assert result.expired is False
        assert result.claims is not None
        assert result.claims.customer == "Acme Engineering Pty Ltd"
        assert result.claims.seat_count == 20

    def test_round_trip_preserves_feature_flags(self):
        priv, pub = generate_keypair()
        claims = _make_claims(feature_flags={"duel": True, "jira_ingest": True})
        sig = sign_license(claims, priv)
        text = serialize_license(claims, sig)

        result = verify_license(text, pub)

        assert result.valid is True
        assert result.claims.feature_flags == {"duel": True, "jira_ingest": True}

    def test_round_trip_preserves_allowed_profiles(self):
        priv, pub = generate_keypair()
        claims = _make_claims(allowed_profiles=["patent_prosecutor", "litigation"])
        sig = sign_license(claims, priv)
        text = serialize_license(claims, sig)

        result = verify_license(text, pub)

        assert result.valid is True
        assert result.claims.allowed_profiles == ["patent_prosecutor", "litigation"]


class TestExpiry:
    def test_expired_license_rejected_with_claims_still_parsed(self):
        """An expired license fails verification but claims still parse — the UI
        needs the customer name and license_id to render a renewal prompt."""
        priv, pub = generate_keypair()
        claims = _make_claims(
            license_id="lic_expired",
            issued_at="2020-01-01T00:00:00Z",
            expires_at="2021-01-01T00:00:00Z",
        )
        sig = sign_license(claims, priv)
        text = serialize_license(claims, sig)

        result = verify_license(text, pub)

        assert result.valid is False
        assert result.expired is True
        assert result.claims is not None
        assert result.claims.license_id == "lic_expired"
        assert "expired" in (result.reason or "").lower()

    def test_now_override_supports_future_clock_rollback_defense(self):
        """The verify primitive accepts a `now` override so the future Tauri-side
        consumer can pass max(system_now, build_epoch, keystore_last_seen). This
        test doesn't build the keystore — it just pins the contract that the
        override exists and is honoured."""
        priv, pub = generate_keypair()
        claims = _make_claims(
            issued_at="2026-04-27T00:00:00Z",
            expires_at="2027-05-27T00:00:00Z",
        )
        sig = sign_license(claims, priv)
        text = serialize_license(claims, sig)

        future_now = datetime(2028, 1, 1, tzinfo=timezone.utc)
        result = verify_license(text, pub, now=future_now)

        assert result.valid is False
        assert result.expired is True


class TestSignatureRejections:
    def test_tampered_payload_fails_signature_check(self):
        priv, pub = generate_keypair()
        claims = _make_claims(customer="Acme", seat_count=20)
        sig = sign_license(claims, priv)
        text = serialize_license(claims, sig)

        sig_b64, _ = text.split(".", 1)
        tampered_payload = base64.b64encode(
            b'{"customer":"EvilCorp","seat_count":99999,"license_id":"x",'
            b'"issued_at":"2026-01-01T00:00:00Z","expires_at":"2030-01-01T00:00:00Z",'
            b'"allowed_profiles":null,"feature_flags":{},"schema_version":1}'
        ).decode("ascii")
        tampered = sig_b64 + "." + tampered_payload

        result = verify_license(tampered, pub)

        assert result.valid is False
        assert result.claims is None
        assert "signature" in (result.reason or "").lower()

    def test_wrong_public_key_fails(self):
        priv_a, _ = generate_keypair()
        _, pub_b = generate_keypair()
        claims = _make_claims()
        sig = sign_license(claims, priv_a)
        text = serialize_license(claims, sig)

        result = verify_license(text, pub_b)

        assert result.valid is False
        assert "signature" in (result.reason or "").lower()

    def test_tampered_signature_fails(self):
        priv, pub = generate_keypair()
        claims = _make_claims()
        sig = sign_license(claims, priv)
        text = serialize_license(claims, sig)

        sig_b64, payload_b64 = text.split(".", 1)
        sig_bytes = base64.b64decode(sig_b64)
        tampered_sig = bytes([sig_bytes[0] ^ 0x01]) + sig_bytes[1:]
        tampered = base64.b64encode(tampered_sig).decode("ascii") + "." + payload_b64

        result = verify_license(tampered, pub)

        assert result.valid is False


class TestMalformedInput:
    def test_empty_string_rejected(self):
        _, pub = generate_keypair()
        result = verify_license("", pub)
        assert result.valid is False
        assert result.claims is None

    def test_no_dot_separator_rejected(self):
        _, pub = generate_keypair()
        result = verify_license("not-a-valid-license-text", pub)
        assert result.valid is False
        assert result.claims is None

    def test_non_base64_rejected(self):
        _, pub = generate_keypair()
        result = verify_license("@@@.@@@", pub)
        assert result.valid is False
        assert result.claims is None

    def test_invalid_json_payload_rejected(self):
        priv, pub = generate_keypair()
        # Sign garbage bytes instead of a real LicenseClaims payload
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        private_key = Ed25519PrivateKey.from_private_bytes(priv)
        garbage = b"this is not json"
        sig = private_key.sign(garbage)
        text = (
            base64.b64encode(sig).decode("ascii")
            + "."
            + base64.b64encode(garbage).decode("ascii")
        )

        result = verify_license(text, pub)

        assert result.valid is False
        assert result.claims is None
        assert "schema" in (result.reason or "").lower() or "json" in (result.reason or "").lower()

    def test_invalid_public_key_size_rejected(self):
        priv, _ = generate_keypair()
        claims = _make_claims()
        sig = sign_license(claims, priv)
        text = serialize_license(claims, sig)

        # Pass a too-short "public key" — primitive must not raise
        result = verify_license(text, b"not-32-bytes")

        assert result.valid is False
        assert result.claims is None


class TestCanonicalJSON:
    """Round-trips with field-order changes still verify — `_canonical_json`
    must produce the same bytes regardless of dict iteration order."""

    def test_field_order_independence(self):
        priv, pub = generate_keypair()
        claims = _make_claims()
        sig = sign_license(claims, priv)
        text = serialize_license(claims, sig)

        # Re-parse and re-construct claims from a different field order, then
        # re-sign with a fresh canonical-JSON pass. Should produce identical bytes.
        from services.license_service import _canonical_json
        reordered = LicenseClaims(
            schema_version=claims.schema_version,
            feature_flags=claims.feature_flags,
            allowed_profiles=claims.allowed_profiles,
            expires_at=claims.expires_at,
            issued_at=claims.issued_at,
            seat_count=claims.seat_count,
            customer=claims.customer,
            license_id=claims.license_id,
        )
        assert _canonical_json(claims) == _canonical_json(reordered)
        # And the original signature still verifies the original text.
        assert verify_license(text, pub).valid is True

    def test_canonical_json_is_explicit_not_pydantic_dependent(self):
        """`_canonical_json` constructs its dict from explicit field references,
        not `model_dump()`. This test pins that contract so the function can't
        silently regress to a pydantic-dependent implementation that future
        pydantic upgrades could break."""
        import json
        from services.license_service import _canonical_json

        claims = _make_claims()
        canonical = _canonical_json(claims)
        decoded = json.loads(canonical.decode("utf-8"))

        # Exact set of keys the canonical-JSON path commits to. Any deviation
        # (added, removed, renamed) must come with an explicit code change to
        # `_canonical_json`, not be inferred from the schema.
        assert set(decoded.keys()) == {
            "license_id",
            "customer",
            "seat_count",
            "issued_at",
            "expires_at",
            "allowed_profiles",
            "feature_flags",
            "schema_version",
        }


class TestExpiresAtTimezone:
    """The signing service must produce timezone-aware UTC `expires_at`. Verify
    rejects naive timestamps so the contract violation surfaces immediately
    instead of being silently fixed up."""

    def test_naive_expires_at_rejected(self):
        priv, pub = generate_keypair()
        claims = _make_claims(expires_at="2027-05-27T00:00:00")  # no Z, no offset
        sig = sign_license(claims, priv)
        text = serialize_license(claims, sig)

        result = verify_license(text, pub)

        assert result.valid is False
        assert result.expired is False
        assert result.claims is not None  # claims still parse for diagnostics
        assert "timezone" in (result.reason or "").lower()

    def test_z_suffix_accepted(self):
        priv, pub = generate_keypair()
        claims = _make_claims(expires_at="2027-05-27T00:00:00Z")
        sig = sign_license(claims, priv)
        text = serialize_license(claims, sig)

        result = verify_license(text, pub)

        assert result.valid is True

    def test_explicit_offset_accepted(self):
        priv, pub = generate_keypair()
        claims = _make_claims(expires_at="2027-05-27T00:00:00+00:00")
        sig = sign_license(claims, priv)
        text = serialize_license(claims, sig)

        result = verify_license(text, pub)

        assert result.valid is True


class TestFeatureFlagsTyping:
    """`feature_flags` is intentionally narrowed to `dict[str, bool]` per ADR
    006. Pydantic must reject non-bool values at construction time so a buggy
    signing service can't ship licenses with garbage that consumers later have
    to handle defensively."""

    def test_bool_values_accepted(self):
        claims = LicenseClaims(
            license_id="lic_x",
            customer="Acme",
            seat_count=5,
            issued_at="2026-04-27T00:00:00Z",
            expires_at="2027-05-27T00:00:00Z",
            feature_flags={"duel": True, "jira_ingest": False},
        )
        assert claims.feature_flags == {"duel": True, "jira_ingest": False}

    def test_non_bool_value_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            LicenseClaims(
                license_id="lic_x",
                customer="Acme",
                seat_count=5,
                issued_at="2026-04-27T00:00:00Z",
                expires_at="2027-05-27T00:00:00Z",
                feature_flags={"tier": "enterprise"},  # str, not bool
            )

    def test_non_string_key_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            LicenseClaims(
                license_id="lic_x",
                customer="Acme",
                seat_count=5,
                issued_at="2026-04-27T00:00:00Z",
                expires_at="2027-05-27T00:00:00Z",
                feature_flags={1: True},  # int key
            )
