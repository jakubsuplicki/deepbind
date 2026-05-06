"""Service-layer entitlement gate (ADR 019, chunk 4).

This is the single FastAPI dependency that gates write/inference
endpoints on the current entitlement state. The frontend wall is the
primary UX surface; this gate is **defence in depth** — it stops a
power user (or a buggy frontend) from hitting a gated endpoint
directly when the license/trial state would otherwise block them.

## What gets gated

Per ADR 019 §"Past-grace state" the rule is:

- **Read paths stay open**, always. Listing notes, viewing chats,
  searching, fetching catalog data, fetching license state itself —
  none of these are gated. Past-grace mode in particular needs read
  paths open so the customer can reach their data.
- **Write paths are gated** by the entitlement state. This dependency
  is added to POST / PATCH / DELETE endpoints that create or mutate
  user knowledge or run inference.

The gate calls ``entitlements.is_functional()`` which returns True for
``unlicensed_trial_active``, ``unlicensed_trial_expiring``,
``licensed_active``, and ``licensed_in_grace`` — and False for
``unlicensed_trial_expired``, ``licensed_invalid``, and
``licensed_past_grace``. Past-grace fails the gate even though
``is_read_only=True`` — the gate exists *to block writes*, the
read-allowance is realised by simply not having the gate on the read
endpoints.

## Response shape on block

Returns HTTP 403 with body::

    {
      "detail": "license_required",
      "state": <full EntitlementState dict>
    }

The frontend treats any 403 with ``detail == "license_required"`` as
"surface the wall" — it does not need to parse the state field for
the basic block decision (the wall content is rendered from the
``__JARVIS_LICENSE_STATE__`` global + a follow-up GET /api/license/state
poll). The state field is included so the frontend can show the
specific reason if it wants to.

## Dev-mode bypass

When ``JARVIS_LICENSE_GATE_BYPASS=1`` is set in the environment, the
gate becomes a no-op. Used by:

1. The pytest test suite — most tests don't care about license state.
2. The dev-mode shell launch (``desktop/scripts/dev.sh``) when a
   developer is iterating on a non-licensing feature and doesn't want
   to deal with paste-a-key flows on every restart.

Production builds **must not** set this env var. The recommended way
to disable it for release builds is to assert at startup that it is
unset when ``JARVIS_BUILD_PROFILE=production`` — but that's a future
ADR-019 chunk-7 hardening item and is not enforced today. Today the
honour-system pattern is "don't set this in prod"; the audit gate at
build time is the durable enforcement.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict

from fastapi import HTTPException, status

from services import entitlements

logger = logging.getLogger(__name__)


def _bypass_enabled() -> bool:
    return os.environ.get("JARVIS_LICENSE_GATE_BYPASS") == "1"


def require_functional() -> None:
    """FastAPI dependency: 403 unless the app is in a functional state.

    Used as ``Depends(require_functional)`` on write/inference endpoints.
    Read-only endpoints do not declare this dependency — past-grace
    mode allows them through naturally.
    """
    if _bypass_enabled():
        return

    state = entitlements.current_state()
    if state.is_functional:
        return

    logger.info(
        "entitlement gate blocked request: state=%s reason=%s",
        state.state, state.reason,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "detail": "license_required",
            "state": asdict(state),
        },
    )
