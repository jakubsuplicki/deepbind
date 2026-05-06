"""Tests for the build-time-embedded public-key module (ADR 019, chunk 1).

Two responsibilities under test:

1. The dev-fallback path. When `_license_pubkey_baked` is absent (the
   normal pytest state), `LICENSE_PUBLIC_KEY` resolves to the constant
   committed at the top of `license_public_key.py`. The matching dev
   private key in `tests/fixtures/license_dev_keys/` must be able to sign
   a license that the embedded key verifies.

2. The production-injection path. When `_license_pubkey_baked` is present
   with a valid `LICENSE_PUBLIC_KEY_HEX` constant, the module loads from
   it. Malformed injections (wrong length, not hex, missing constant)
   raise rather than silently downgrading to the dev key — that downgrade
   would be a security regression.

The tests run `_resolve_public_key()` directly rather than re-importing
the module, because import-time module state is awkward to mutate
under pytest's import caching.
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services import license_public_key as lpk
from services.license_service import (
    LicenseClaims,
    serialize_license,
    sign_license,
    verify_license,
    verify_license_with_embedded_key,
)

DEV_KEYS_DIR = Path(__file__).parent / "fixtures" / "license_dev_keys"


def _dev_private() -> bytes:
    return DEV_KEYS_DIR.joinpath("private.key").read_bytes()


def _dev_public() -> bytes:
    return DEV_KEYS_DIR.joinpath("public.key").read_bytes()


def _trial_claims(expires_in: timedelta = timedelta(days=30)) -> LicenseClaims:
    now = datetime.now(timezone.utc)
    return LicenseClaims(
        license_id="lic_test",
        customer="Test Customer",
        seat_count=1,
        issued_at=now.isoformat().replace("+00:00", "Z"),
        expires_at=(now + expires_in).isoformat().replace("+00:00", "Z"),
        feature_flags={},
    )


# --- Dev fallback -----------------------------------------------------------


def test_dev_constant_matches_fixture():
    """The committed _DEV_PUBLIC_KEY_HEX must match tests/fixtures/.../public.key.

    This pins the keypair-vs-constant contract. If either side rotates
    without updating the other, signed test licenses stop verifying and
    every license-related test breaks.
    """
    constant_bytes = bytes.fromhex(lpk._DEV_PUBLIC_KEY_HEX)
    fixture_bytes = _dev_public()
    assert constant_bytes == fixture_bytes, (
        "_DEV_PUBLIC_KEY_HEX in services/license_public_key.py does not "
        "match tests/fixtures/license_dev_keys/public.key. Regenerate one "
        "or the other; see fixture README for the rotation procedure."
    )


def test_dev_keypair_round_trips():
    """A license signed with the dev private key verifies against the dev public key."""
    claims = _trial_claims()
    sig = sign_license(claims, _dev_private())
    license_text = serialize_license(claims, sig)
    result = verify_license(license_text, _dev_public())
    assert result.valid, result.reason


def test_embedded_key_resolves_to_dev_in_pytest():
    """In pytest, no _license_pubkey_baked exists, so LICENSE_PUBLIC_KEY is the dev key."""
    assert lpk.LICENSE_PUBLIC_KEY == _dev_public()
    assert lpk.IS_PRODUCTION_KEY is False


def test_verify_with_embedded_key_accepts_dev_signed_license():
    """The production entry point (verify_license_with_embedded_key) accepts dev-signed
    licenses in dev/test, where the embedded key IS the dev key."""
    claims = _trial_claims()
    sig = sign_license(claims, _dev_private())
    license_text = serialize_license(claims, sig)
    result = verify_license_with_embedded_key(license_text)
    assert result.valid, result.reason
    assert result.claims is not None
    assert result.claims.customer == "Test Customer"


def test_verify_with_embedded_key_rejects_foreign_signature():
    """A license signed by a *different* keypair must fail against the embedded key,
    even in dev. This is the negative of the round-trip test — confirms the
    embedded key is actually being used (not silently bypassed)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    rogue_priv = Ed25519PrivateKey.generate().private_bytes_raw()
    claims = _trial_claims()
    sig = sign_license(claims, rogue_priv)
    license_text = serialize_license(claims, sig)
    result = verify_license_with_embedded_key(license_text)
    assert result.valid is False
    assert result.reason is not None and "Signature" in result.reason


# --- Production-injection path ---------------------------------------------


def _install_baked_module(hex_value: str | None) -> None:
    """Install a synthetic services._license_pubkey_baked module for testing.

    Mutates sys.modules so the next call to _resolve_public_key() picks
    it up. Caller must clean up via _uninstall_baked_module().
    """
    if hex_value is None:
        # Inject a module with a missing constant — separate failure mode.
        module = type(sys)("services._license_pubkey_baked")
    else:
        module = type(sys)("services._license_pubkey_baked")
        module.LICENSE_PUBLIC_KEY_HEX = hex_value  # type: ignore[attr-defined]
    sys.modules["services._license_pubkey_baked"] = module


def _uninstall_baked_module() -> None:
    sys.modules.pop("services._license_pubkey_baked", None)


def test_production_injection_overrides_dev_key():
    """When _license_pubkey_baked is present with a valid hex constant, that
    key is used instead of the dev fallback."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    prod_priv = Ed25519PrivateKey.generate()
    prod_pub_hex = prod_priv.public_key().public_bytes_raw().hex()

    try:
        _install_baked_module(prod_pub_hex)
        key, is_prod = lpk._resolve_public_key()
        assert key == bytes.fromhex(prod_pub_hex)
        assert is_prod is True
    finally:
        _uninstall_baked_module()


def test_production_injection_rejects_wrong_length():
    """A 30-byte (60-char hex) key — wrong for Ed25519 — must raise, not fall back."""
    try:
        _install_baked_module("00" * 30)  # 60 hex chars, not 64
        with pytest.raises(RuntimeError, match="64-character hex"):
            lpk._resolve_public_key()
    finally:
        _uninstall_baked_module()


def test_production_injection_rejects_non_hex():
    """Non-hex input must raise, not fall back."""
    try:
        # 64 chars, not all hex
        _install_baked_module("Z" * 64)
        with pytest.raises(RuntimeError):
            lpk._resolve_public_key()
    finally:
        _uninstall_baked_module()


def test_production_injection_rejects_missing_constant():
    """A baked module without LICENSE_PUBLIC_KEY_HEX must raise, not fall back."""
    try:
        _install_baked_module(None)  # module with no constant
        with pytest.raises(RuntimeError, match="missing or not"):
            lpk._resolve_public_key()
    finally:
        _uninstall_baked_module()


# --- Regression: dev fallback emits a warning -------------------------------


def test_dev_fallback_emits_warning(monkeypatch):
    """The dev fallback must emit a warning so accidentally-shipped dev builds
    are loud about it. Suppression env var must work for noisy test environments."""
    _uninstall_baked_module()
    monkeypatch.delenv("JARVIS_LICENSE_DEV_WARNING_SUPPRESS", raising=False)

    with pytest.warns(UserWarning, match="DEV license public key"):
        lpk._resolve_public_key()


def test_dev_fallback_warning_can_be_suppressed(monkeypatch):
    """JARVIS_LICENSE_DEV_WARNING_SUPPRESS=1 silences the warning. Useful in CI
    where dev-key warnings are noise, not signal."""
    _uninstall_baked_module()
    monkeypatch.setenv("JARVIS_LICENSE_DEV_WARNING_SUPPRESS", "1")

    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("error")
        # If a UserWarning fires, simplefilter("error") would re-raise it.
        lpk._resolve_public_key()
