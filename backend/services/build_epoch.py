"""Build-time epoch — clock-tampering defense (ADR 019, chunk 6).

Each production sidecar binary embeds the UTC timestamp of its build.
The entitlement state machine refuses to accept a system clock earlier
than this floor (with a small tolerance window). Defeats the trivial
"set clock backward to extend an expired license" attack.

## Resolution order

Same shape as ``services/license_public_key.py``:

1. **Production:** if ``services/_build_epoch_baked.py`` exists with
   ``BUILD_EPOCH_ISO = "<ISO 8601 UTC>"``, that timestamp is used.
   ``desktop/scripts/build-sidecar.sh`` writes the file via
   ``date -u +"%Y-%m-%dT%H:%M:%SZ"`` before invoking PyInstaller and
   removes it post-build via the EXIT trap (alongside the public key
   file).
2. **Dev / pytest fallback:** a far-past constant — ``2020-01-01T00:00:00Z``.
   Effectively a no-op floor that doesn't interfere with development. A
   warning is emitted at import time so accidentally-shipped dev binaries
   are loud about it.

## Why this is separate from the public-key module

Both are build-time-injected, but they serve different purposes and have
different rotation cadences:

- The public key changes when we rotate signing keys (rare).
- The build epoch changes on every build (frequent).

Keeping them in separate modules makes the build-script logic clearer
(two distinct file writes, two distinct cleanup steps) and avoids
accidentally bumping the public key when only the epoch needs to update.
"""

from __future__ import annotations

import logging
import os
import warnings
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Far-past dev fallback. Anything before this date is impossible for a
# real binary (the project didn't exist), so it never blocks dev work.
_DEV_BUILD_EPOCH_ISO = "2020-01-01T00:00:00Z"


def _resolve_build_epoch() -> tuple[datetime, bool]:
    """Return (build_epoch_utc, is_production_epoch).

    Mirror of ``license_public_key._resolve_public_key`` — production
    injection wins, dev fallback emits a warning, malformed injection
    raises rather than silently downgrading.
    """
    try:
        from services import _build_epoch_baked  # type: ignore[attr-defined]
    except ImportError:
        if os.environ.get("JARVIS_LICENSE_DEV_WARNING_SUPPRESS") != "1":
            warnings.warn(
                "Using DEV build epoch (2020-01-01) — clock-rollback "
                "defense is effectively disabled. Expected in dev/test; "
                "if seen in a production build, the build-time epoch "
                "injection failed.",
                stacklevel=2,
            )
            logger.warning(
                "build_epoch: dev fallback in use (production builds "
                "inject _build_epoch_baked at build time)."
            )
        return _parse(_DEV_BUILD_EPOCH_ISO), False

    iso = getattr(_build_epoch_baked, "BUILD_EPOCH_ISO", None)
    if not isinstance(iso, str) or not iso:
        raise RuntimeError(
            "build_epoch: _build_epoch_baked.BUILD_EPOCH_ISO is missing "
            "or empty. Rebuild the sidecar with a valid build script."
        )
    try:
        parsed = _parse(iso)
    except ValueError as exc:
        raise RuntimeError(
            f"build_epoch: BUILD_EPOCH_ISO is not parseable: {exc}"
        ) from None
    logger.info("build_epoch: production epoch loaded: %s", iso)
    return parsed, True


def _parse(iso: str) -> datetime:
    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


BUILD_EPOCH, IS_PRODUCTION_EPOCH = _resolve_build_epoch()
