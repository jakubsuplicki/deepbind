#!/usr/bin/env python3
"""Offline license signing CLI (ADR 006).

This tool runs on the **private signing service** side — never bundled with
the shipping app. It generates Ed25519 keypairs, signs `LicenseClaims` JSON
into the `signature_b64.payload_b64` wire format, and verifies a serialized
license against a public key.

Operator workflow (one-time, then per-customer):

    # One-time keypair generation. Keep `private.key` offline.
    python -m scripts.sign_license generate-keypair --out-dir ./keys

    # Per-customer: write claims.json, sign it, hand the .lic to the customer.
    python -m scripts.sign_license sign \\
        --claims customer-acme.json \\
        --private-key ./keys/private.key \\
        --out customer-acme.lic

    # Round-trip check before delivery. Exit codes:
    #   0 — license valid
    #   1 — signature/key verification failed (invalid or tampered)
    #   2 — license expired (claims still parseable for renewal UI)
    #   3 — input error (missing file, malformed text, wrong-length key)
    python -m scripts.sign_license verify \\
        --license customer-acme.lic \\
        --public-key ./keys/public.key

`claims.json` shape (matches `LicenseClaims` in services/license_service.py):

    {
      "license_id": "lic_2026_04_28_acme",
      "customer": "Acme Patent LLP",
      "seat_count": 12,
      "issued_at": "2026-04-28T12:00:00Z",
      "expires_at": "2027-05-28T12:00:00Z",
      "allowed_profiles": ["patent-prosecutor"],
      "feature_flags": {"duel": true, "jira_ingest": false},
      "schema_version": 1
    }

Per ADR 006 §"Activation flow", the public key bytes (32 raw) are embedded
in the production binary at compile time — `public.key` from this CLI is the
input to that embedding step. The private key never leaves the signing
service host.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

# Ensure the parent `backend/` is on path when running as a script.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from services.license_service import (  # noqa: E402
    LicenseClaims,
    generate_keypair,
    serialize_license,
    sign_license,
    verify_license,
)


def _write_keyfile(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write a key file with restrictive permissions (best-effort on POSIX).

    Defends against the casual mistake of `cat private.key` showing up in a
    world-readable location on a shared signing host. Doesn't help against a
    compromised host — the right defence there is offline storage.
    """
    path.write_bytes(data)
    try:
        os.chmod(path, mode)
    except OSError:
        # Windows or unusual filesystem — silently skip rather than fail
        # the keygen flow over a permission detail.
        pass


def cmd_generate_keypair(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    private_key, public_key = generate_keypair()

    private_path = out_dir / "private.key"
    public_path = out_dir / "public.key"

    if private_path.exists() and not args.overwrite:
        print(f"refusing to overwrite existing {private_path} (use --overwrite)", file=sys.stderr)
        return 3
    if public_path.exists() and not args.overwrite:
        print(f"refusing to overwrite existing {public_path} (use --overwrite)", file=sys.stderr)
        return 3

    _write_keyfile(private_path, private_key)
    _write_keyfile(public_path, public_key, mode=0o644)

    print(f"wrote private key: {private_path} ({len(private_key)} bytes)")
    print(f"wrote public key:  {public_path} ({len(public_key)} bytes)")
    print()
    print("NEXT: embed public.key bytes in the production binary build (ADR 006).")
    print("      Keep private.key offline. Rotate by issuing a new keypair and")
    print("      shipping a build with the new public key — old licenses fail")
    print("      verification by design.")
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    claims_path = Path(args.claims).resolve()
    private_key_path = Path(args.private_key).resolve()

    if not claims_path.exists():
        print(f"claims file not found: {claims_path}", file=sys.stderr)
        return 3
    if not private_key_path.exists():
        print(f"private key not found: {private_key_path}", file=sys.stderr)
        return 3

    try:
        claims_dict = json.loads(claims_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"claims file is not valid JSON: {exc}", file=sys.stderr)
        return 3

    try:
        claims = LicenseClaims(**claims_dict)
    except Exception as exc:
        print(f"claims schema validation failed: {exc}", file=sys.stderr)
        return 3

    private_key = private_key_path.read_bytes()
    if len(private_key) != 32:
        print(f"private key must be 32 raw bytes (got {len(private_key)})", file=sys.stderr)
        return 3

    signature = sign_license(claims, private_key)
    license_text = serialize_license(claims, signature)

    if args.out:
        out_path = Path(args.out).resolve()
        if out_path.exists() and not args.overwrite:
            print(f"refusing to overwrite existing {out_path} (use --overwrite)", file=sys.stderr)
            return 3
        out_path.write_text(license_text, encoding="utf-8")
        print(f"wrote license: {out_path}")
    else:
        # No --out: emit to stdout. Useful for piping into a packaging step.
        sys.stdout.write(license_text)
        sys.stdout.write("\n")

    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify a serialized license. Distinct exit codes:

      0 — valid
      1 — signature/invalid
      2 — expired (claims still parseable; renewal-prompt UX has the data)
      3 — input error (missing files, wrong-length key)
    """
    license_path = Path(args.license).resolve()
    public_key_path = Path(args.public_key).resolve()

    if not license_path.exists():
        print(f"license file not found: {license_path}", file=sys.stderr)
        return 3
    if not public_key_path.exists():
        print(f"public key not found: {public_key_path}", file=sys.stderr)
        return 3

    license_text = license_path.read_text(encoding="utf-8").strip()
    public_key = public_key_path.read_bytes()
    if len(public_key) != 32:
        print(f"public key must be 32 raw bytes (got {len(public_key)})", file=sys.stderr)
        return 3

    result = verify_license(license_text, public_key)

    if result.valid:
        assert result.claims is not None
        print(f"OK  license is valid")
        print(f"    license_id: {result.claims.license_id}")
        print(f"    customer:   {result.claims.customer}")
        print(f"    seats:      {result.claims.seat_count}")
        print(f"    expires:    {result.claims.expires_at}")
        if result.claims.allowed_profiles:
            print(f"    profiles:   {', '.join(result.claims.allowed_profiles)}")
        return 0

    if result.expired and result.claims is not None:
        # Expired-but-parseable: we still print claims since the consumer-side
        # contract is "renewal banner uses these fields". `verify_license`
        # returns claims on expiry for exactly this reason. Exit code 2 lets
        # operators distinguish "expired" from "tampered" in shell scripts.
        print(f"EXPIRED  license has expired (claims still parseable for renewal UI)", file=sys.stderr)
        print(f"         license_id: {result.claims.license_id}", file=sys.stderr)
        print(f"         customer:   {result.claims.customer}", file=sys.stderr)
        print(f"         expired_at: {result.claims.expires_at}", file=sys.stderr)
        return 2

    print(f"FAIL  license verification failed: {result.reason}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sign_license",
        description="Offline license signing tool (ADR 006).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_kg = sub.add_parser("generate-keypair", help="Generate a fresh Ed25519 keypair.")
    p_kg.add_argument("--out-dir", required=True, help="Directory to write private.key and public.key.")
    p_kg.add_argument("--overwrite", action="store_true", help="Overwrite existing key files.")
    p_kg.set_defaults(func=cmd_generate_keypair)

    p_sign = sub.add_parser("sign", help="Sign a claims.json into a serialized license.")
    p_sign.add_argument("--claims", required=True, help="Path to claims.json.")
    p_sign.add_argument("--private-key", required=True, help="Path to 32-byte Ed25519 private key.")
    p_sign.add_argument("--out", help="Write license to this path (default: stdout).")
    p_sign.add_argument("--overwrite", action="store_true", help="Overwrite existing license file.")
    p_sign.set_defaults(func=cmd_sign)

    p_ver = sub.add_parser("verify", help="Verify a serialized license.")
    p_ver.add_argument("--license", required=True, help="Path to the .lic file.")
    p_ver.add_argument("--public-key", required=True, help="Path to 32-byte Ed25519 public key.")
    p_ver.set_defaults(func=cmd_verify)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
