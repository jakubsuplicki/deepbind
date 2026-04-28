"""Tests for the offline license signing CLI (scripts/sign_license.py).

Covers the full operator-facing surface: keypair generation, signing a
claims.json into the wire format, verifying valid/invalid/expired licenses,
exit codes, refusal-to-overwrite, and input-error paths.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.sign_license import main


# ── Helpers ──────────────────────────────────────────────────────────────────


def _claims_dict(*, expires_at: str = None, **overrides) -> dict:
    """Default-good claims dict that the signing CLI accepts."""
    if expires_at is None:
        # Default: a year from now, timezone-aware as the schema requires.
        expires_at = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat().replace("+00:00", "Z")
    base = {
        "license_id": "lic_test_001",
        "customer": "Test Co",
        "seat_count": 3,
        "issued_at": "2026-04-28T00:00:00Z",
        "expires_at": expires_at,
        "allowed_profiles": ["developer-devops"],
        "feature_flags": {"duel": True},
        "schema_version": 1,
    }
    base.update(overrides)
    return base


def _generate_keys(tmp_path: Path) -> tuple:
    """Run the keypair generator and return (private_path, public_path)."""
    rc = main(["generate-keypair", "--out-dir", str(tmp_path / "keys")])
    assert rc == 0
    return (tmp_path / "keys" / "private.key", tmp_path / "keys" / "public.key")


def _sign(tmp_path: Path, private_key: Path, claims: dict) -> Path:
    """Write claims to disk, sign, return the .lic path."""
    claims_path = tmp_path / "claims.json"
    claims_path.write_text(json.dumps(claims))
    lic_path = tmp_path / "license.lic"
    rc = main([
        "sign",
        "--claims", str(claims_path),
        "--private-key", str(private_key),
        "--out", str(lic_path),
    ])
    assert rc == 0
    return lic_path


# ── Keypair generation ──────────────────────────────────────────────────────


class TestGenerateKeypair:
    def test_writes_32_byte_private_and_public(self, tmp_path):
        priv, pub = _generate_keys(tmp_path)
        assert priv.read_bytes().__len__() == 32
        assert pub.read_bytes().__len__() == 32

    def test_private_key_has_restrictive_perms(self, tmp_path):
        # Best-effort POSIX 0o600 — Windows is silently skipped per design.
        import platform
        if platform.system() == "Windows":
            pytest.skip("Windows file perms differ; the chmod is best-effort")
        priv, _ = _generate_keys(tmp_path)
        mode = priv.stat().st_mode & 0o777
        assert mode == 0o600, f"private key should be 0o600, got {oct(mode)}"

    def test_refuses_to_overwrite_without_flag(self, tmp_path):
        _generate_keys(tmp_path)
        # Second call without --overwrite must refuse — exit 3 (input error).
        rc = main(["generate-keypair", "--out-dir", str(tmp_path / "keys")])
        assert rc == 3

    def test_overwrite_flag_replaces(self, tmp_path):
        priv1, _ = _generate_keys(tmp_path)
        original = priv1.read_bytes()
        rc = main(["generate-keypair", "--out-dir", str(tmp_path / "keys"), "--overwrite"])
        assert rc == 0
        # New key generated — not the same bytes.
        assert priv1.read_bytes() != original


# ── Signing ─────────────────────────────────────────────────────────────────


class TestSign:
    def test_round_trip_valid(self, tmp_path):
        priv, pub = _generate_keys(tmp_path)
        lic = _sign(tmp_path, priv, _claims_dict())
        rc = main(["verify", "--license", str(lic), "--public-key", str(pub)])
        assert rc == 0

    def test_missing_claims_file(self, tmp_path):
        priv, _ = _generate_keys(tmp_path)
        rc = main([
            "sign",
            "--claims", str(tmp_path / "nonexistent.json"),
            "--private-key", str(priv),
            "--out", str(tmp_path / "out.lic"),
        ])
        assert rc == 3

    def test_malformed_claims_json(self, tmp_path):
        priv, _ = _generate_keys(tmp_path)
        claims_path = tmp_path / "claims.json"
        claims_path.write_text("{not valid json")
        rc = main([
            "sign",
            "--claims", str(claims_path),
            "--private-key", str(priv),
            "--out", str(tmp_path / "out.lic"),
        ])
        assert rc == 3

    def test_schema_validation_failure(self, tmp_path):
        priv, _ = _generate_keys(tmp_path)
        bad = _claims_dict()
        del bad["customer"]  # required field missing
        claims_path = tmp_path / "claims.json"
        claims_path.write_text(json.dumps(bad))
        rc = main([
            "sign",
            "--claims", str(claims_path),
            "--private-key", str(priv),
            "--out", str(tmp_path / "out.lic"),
        ])
        assert rc == 3

    def test_wrong_length_private_key(self, tmp_path):
        bad_priv = tmp_path / "bad.key"
        bad_priv.write_bytes(b"too short")
        claims_path = tmp_path / "claims.json"
        claims_path.write_text(json.dumps(_claims_dict()))
        rc = main([
            "sign",
            "--claims", str(claims_path),
            "--private-key", str(bad_priv),
            "--out", str(tmp_path / "out.lic"),
        ])
        assert rc == 3

    def test_refuses_to_overwrite_existing_license(self, tmp_path):
        priv, _ = _generate_keys(tmp_path)
        lic = _sign(tmp_path, priv, _claims_dict())
        # Second sign to the same out path without --overwrite must refuse.
        claims_path = tmp_path / "claims.json"  # already written by _sign
        rc = main([
            "sign",
            "--claims", str(claims_path),
            "--private-key", str(priv),
            "--out", str(lic),
        ])
        assert rc == 3


# ── Verify exit codes (the load-bearing operator contract) ───────────────────


class TestVerifyExitCodes:
    def test_valid_returns_zero(self, tmp_path):
        priv, pub = _generate_keys(tmp_path)
        lic = _sign(tmp_path, priv, _claims_dict())
        rc = main(["verify", "--license", str(lic), "--public-key", str(pub)])
        assert rc == 0

    def test_expired_returns_two(self, tmp_path):
        # Exit code 2 distinguishes "expired" from "tampered" so renewal-flow
        # shell scripts can branch on the difference.
        priv, pub = _generate_keys(tmp_path)
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        lic = _sign(tmp_path, priv, _claims_dict(expires_at=past))
        rc = main(["verify", "--license", str(lic), "--public-key", str(pub)])
        assert rc == 2

    def test_signature_failure_returns_one(self, tmp_path):
        # Sign with one keypair, verify with a different public key.
        priv, _ = _generate_keys(tmp_path)
        lic = _sign(tmp_path, priv, _claims_dict())
        # New keypair in a separate dir.
        other_dir = tmp_path / "other"
        rc = main(["generate-keypair", "--out-dir", str(other_dir)])
        assert rc == 0
        wrong_pub = other_dir / "public.key"
        rc = main(["verify", "--license", str(lic), "--public-key", str(wrong_pub)])
        assert rc == 1

    def test_missing_license_file_returns_three(self, tmp_path):
        _, pub = _generate_keys(tmp_path)
        rc = main([
            "verify",
            "--license", str(tmp_path / "nonexistent.lic"),
            "--public-key", str(pub),
        ])
        assert rc == 3

    def test_missing_public_key_returns_three(self, tmp_path):
        priv, _ = _generate_keys(tmp_path)
        lic = _sign(tmp_path, priv, _claims_dict())
        rc = main([
            "verify",
            "--license", str(lic),
            "--public-key", str(tmp_path / "nonexistent.key"),
        ])
        assert rc == 3

    def test_wrong_length_public_key_returns_three(self, tmp_path):
        priv, _ = _generate_keys(tmp_path)
        lic = _sign(tmp_path, priv, _claims_dict())
        bad_pub = tmp_path / "bad_pub.key"
        bad_pub.write_bytes(b"not 32 bytes")
        rc = main(["verify", "--license", str(lic), "--public-key", str(bad_pub)])
        assert rc == 3

    def test_malformed_license_text_returns_one(self, tmp_path):
        # `verify_license` returns valid=False on garbled wire format. The CLI
        # surfaces that as exit code 1 (verification failed) rather than 3
        # (input error) — the file exists and is readable, the content just
        # doesn't verify.
        _, pub = _generate_keys(tmp_path)
        lic = tmp_path / "garbage.lic"
        lic.write_text("not.a.real.license")
        rc = main(["verify", "--license", str(lic), "--public-key", str(pub)])
        assert rc == 1
