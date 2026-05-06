"""License + entitlement HTTP API (ADR 019, chunk 2).

Two endpoints, both consumed by the Tauri shell:

- ``POST /api/license/state`` — shell pushes the latest known
  ``license_text`` (file contents) and ``trial_started_at`` (keychain
  value). Backend caches them, computes the entitlement state, returns
  it. Called on app launch, on file-change events, and when the user
  pastes a license key.

- ``GET /api/license/state`` — re-computes from the cached inputs (so
  day-rollover and grace-period boundaries are reflected without
  another shell push) and returns the current state. Cheap; the
  frontend may poll if needed.

There is intentionally NO endpoint that writes the license file to
disk — that's the shell's responsibility. The backend only validates
and reports state. Same for the keychain trial-start record.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator

from services import entitlements

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/license", tags=["license"])


class LicenseStateRequest(BaseModel):
    """Body for ``POST /api/license/state``.

    All three fields are nullable: ``license_text=None`` means the shell
    read the platform license path and found no file;
    ``trial_started_at=None`` means the keychain blob was not yet
    initialized (very first launch only — the shell writes ``utcnow()``
    immediately after this call so subsequent calls always have a real
    value); ``monotonic_floor=None`` means the keychain has no monotonic-
    state record yet (first-launch case again).

    ``trial_started_at`` and ``monotonic_floor`` are parsed as ISO 8601
    with mandatory timezone offset. Naive timestamps are rejected to
    match the same contract as ``LicenseClaims.expires_at`` — the shell
    must always pass UTC-aware values, never silently fixed-up locals.
    """
    license_text: Optional[str] = Field(default=None)
    trial_started_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 UTC timestamp; null on the first-ever launch.",
    )
    monotonic_floor: Optional[str] = Field(
        default=None,
        description=(
            "Highest ISO 8601 UTC timestamp the keychain has ever recorded. "
            "Used by the clock-rollback defense (ADR 019 chunk 6); null on "
            "the first-ever launch before the keychain is initialised."
        ),
    )

    @field_validator("trial_started_at", "monotonic_floor")
    @classmethod
    def _validate_tz_aware(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"timestamp not ISO 8601: {exc}") from None
        if parsed.tzinfo is None:
            raise ValueError("trial_started_at must be timezone-aware (UTC)")
        return value


def _serialize(state: entitlements.EntitlementState) -> dict:
    """asdict + ensure ``state`` is a plain string (Literal types JSON
    fine, but be explicit)."""
    return asdict(state)


def _parse_iso_optional(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _augment_with_effective_now(payload: dict) -> dict:
    """Add the computed effective_now to the response so the Tauri shell
    can write it back to the keychain monotonic-state record (ADR 019
    chunk 6). The keychain entry is updated to ``max(prev, effective_now)``
    on each refresh, monotonically increasing."""
    payload["effective_now"] = entitlements._to_iso(  # type: ignore[attr-defined]
        entitlements.effective_now(monotonic_floor=entitlements.get_cached_inputs()[2])
    )
    return payload


@router.post("/state")
async def push_state(req: LicenseStateRequest) -> dict:
    """Update cached inputs from the shell, compute, return the new state."""
    entitlements.set_inputs(
        license_text=req.license_text,
        trial_started_at=_parse_iso_optional(req.trial_started_at),
        monotonic_floor=_parse_iso_optional(req.monotonic_floor),
    )
    state = entitlements.current_state()
    logger.info(
        "license/state push: state=%s functional=%s days_remaining=%s",
        state.state, state.is_functional, state.days_remaining,
    )
    return _augment_with_effective_now(_serialize(state))


@router.get("/state")
async def get_state() -> dict:
    """Return the current entitlement state, recomputed from cached inputs.

    Recomputation is cheap (one Ed25519 verify if a license is cached,
    a few timedelta arithmetic operations otherwise). Called by the
    frontend to refresh the banner/wall, and by the chunk-4 service-
    layer gates to decide whether to allow a write/inference operation.
    """
    state = entitlements.current_state()
    return _augment_with_effective_now(_serialize(state))
