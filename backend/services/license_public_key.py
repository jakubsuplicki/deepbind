"""Embedded Ed25519 public key for license verification (ADR 019).

This module exposes the single trust root for license verification:
``LICENSE_PUBLIC_KEY: bytes`` — 32 raw bytes of the Ed25519 public key
that production builds use to verify ``.deepfileslic`` files.

## Dev vs production

The constant resolves in two steps per ADR 019 §"Build-time injection mechanism":

1. Try to import ``services._license_pubkey_baked``. The PyInstaller build
   script (``desktop/scripts/build-sidecar.sh``) writes that module from
   the ``JARVIS_LICENSE_PUBKEY_HEX`` env var at build time and removes it
   after PyInstaller completes. If the import succeeds, the production
   public key is in use.

2. If the import fails (dev / test / pytest), fall back to the committed
   ``_DEV_PUBLIC_KEY_HEX`` constant below. The matching dev *private* key
   lives at ``backend/tests/fixtures/license_dev_keys/private.key`` for
   test signing — it is NOT a production secret and licenses signed with
   it will fail verification in production builds (which embed a different
   public key).

The dev fallback always emits a one-time warning at import so an
accidentally-shipped dev build is loud about it.

## Why hex-in-source for the dev key

The dev public key is a hardcoded constant rather than read from a file
because file I/O at import time is fragile inside PyInstaller's frozen
runtime. The constant is ~64 ASCII characters and lives in version
control alongside its matching private-key fixture, so the keypair
provenance is auditable from a single git log.

## Production-injection contract

The build script writes ``_license_pubkey_baked.py`` containing exactly:

    LICENSE_PUBLIC_KEY_HEX = "<64 hex chars>"

Anything else (different variable name, byte string, multi-line) breaks
this module's import path. The build script is the only writer; do not
hand-edit ``_license_pubkey_baked.py``.
"""

from __future__ import annotations

import logging
import os
import warnings

logger = logging.getLogger(__name__)

# Dev keypair — committed so tests can sign + verify end-to-end without
# any build-time injection step. The matching private key is at
# backend/tests/fixtures/license_dev_keys/private.key. Both must change
# together; the test suite asserts they match.
#
# This is NOT a production key. Production builds inject a different
# public key via JARVIS_LICENSE_PUBKEY_HEX → _license_pubkey_baked.py
# (see module docstring). A license signed with this dev private key
# verifies in dev builds and FAILS in production builds — by design.
_DEV_PUBLIC_KEY_HEX = "bea45da3098ccd3de2d129730741ebee7f012c53797cb474da950b40dfb243f0"


def _resolve_public_key() -> tuple[bytes, bool]:
    """Return (public_key_bytes, is_production_key).

    Tries the production-injected module first; falls back to the dev
    constant. Emits a single warning when falling back so dev builds are
    loud about it (and a production build that fails injection doesn't
    silently downgrade to the dev key).
    """
    try:
        from services import _license_pubkey_baked  # type: ignore[attr-defined]
    except ImportError:
        # Dev path. Emit one-time visibility — pytest captures this; a
        # frozen production build that somehow loses _license_pubkey_baked
        # will surface this warning at startup.
        if os.environ.get("JARVIS_LICENSE_DEV_WARNING_SUPPRESS") != "1":
            warnings.warn(
                "Using DEV license public key (no _license_pubkey_baked "
                "module found). This is expected in dev/test; if seen in "
                "a production build, the build-time key injection failed.",
                stacklevel=2,
            )
            logger.warning(
                "license_public_key: dev fallback in use (production "
                "builds inject _license_pubkey_baked at build time)."
            )
        return bytes.fromhex(_DEV_PUBLIC_KEY_HEX), False

    hex_value = getattr(_license_pubkey_baked, "LICENSE_PUBLIC_KEY_HEX", None)
    if not isinstance(hex_value, str) or len(hex_value) != 64:
        # Malformed injection — refuse to silently fall back to the dev
        # key; that would be a security downgrade. Crash loudly instead.
        raise RuntimeError(
            "license_public_key: _license_pubkey_baked.LICENSE_PUBLIC_KEY_HEX "
            "is missing or not a 64-character hex string. Rebuild the "
            "sidecar with a valid JARVIS_LICENSE_PUBKEY_HEX env var."
        )
    try:
        baked = bytes.fromhex(hex_value)
    except ValueError as exc:
        raise RuntimeError(
            f"license_public_key: _license_pubkey_baked.LICENSE_PUBLIC_KEY_HEX "
            f"is not valid hex: {exc}"
        ) from None
    if len(baked) != 32:
        raise RuntimeError(
            f"license_public_key: embedded public key is {len(baked)} bytes; "
            "must be exactly 32 (Ed25519 public-key length)."
        )
    logger.info("license_public_key: production key loaded from build-time injection.")
    return baked, True


LICENSE_PUBLIC_KEY, IS_PRODUCTION_KEY = _resolve_public_key()
