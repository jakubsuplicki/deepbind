"""Entitlement state machine — single decision point for license-aware features.

ADR 019 §"Entitlement state machine" defines six mutually-exclusive states
the app can be in at any moment. This module is the only place that
computes them and the only thing service-layer gates (chunk 4) call.

## Inputs (pushed in by the Tauri shell)

The shell owns the *storage* for both inputs — the backend doesn't read
disk or keychain itself:

- ``license_text`` — contents of the license file at the platform path
  (``~/Library/Application Support/Jarvis/license.json`` on macOS,
  ``%APPDATA%\\Jarvis\\license.json`` on Windows). ``None`` if the file
  is absent.
- ``trial_started_at`` — ISO 8601 UTC timestamp from the OS keychain
  (Tauri ``keyring`` plugin). ``None`` only on the very first call before
  the keychain has been initialized; the shell writes ``utcnow()`` then
  re-passes that value in subsequent calls.

The shell pushes these via ``POST /api/license/state`` on launch, on
file-change events, and when the user pastes a key. The backend caches
them in-process; ``GET /api/license/state`` and ``is_functional()``
re-compute from the cache (so a clock advance — e.g. trial day-rollover
between two requests — is reflected without a Tauri-side push).

## States (per ADR 019)

| State                          | When                                   | App functional? |
|--------------------------------|----------------------------------------|-----------------|
| ``unlicensed_trial_active``    | No license; trial within 30d           | yes             |
| ``unlicensed_trial_expiring``  | Trial active, ≤3 days remain           | yes             |
| ``unlicensed_trial_expired``   | No license; trial >30d ago             | no (wall)       |
| ``licensed_active``            | License valid, not expired             | yes             |
| ``licensed_in_grace``          | License valid, expired ≤30d ago        | yes             |
| ``licensed_past_grace``        | License valid, expired >30d ago        | read-only       |
| ``licensed_invalid``           | License present but signature/format bad | no (wall)     |

Read-only past-grace per ADR 019 — read paths stay open, write/inference
paths are gated. The data is already Markdown on disk per the project's
source-of-truth doctrine, so the customer is never trapped.

## Why the backend caches + re-computes rather than storing the state

State is a function of (license_text, trial_started_at, now). Storing
the *state* would require Tauri to re-push on every clock-relevant
boundary (midnight in user's timezone? plus their grace-period boundary?).
Storing the *inputs* and re-computing on demand makes day-rollover
trivial — the next ``is_functional()`` call sees the new ``now`` and
returns the right answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Literal, Optional

from services.build_epoch import BUILD_EPOCH
from services.license_service import (
    LicenseClaims,
    VerificationResult,
    verify_license_with_embedded_key,
)

logger = logging.getLogger(__name__)

# Trial duration per ADR 019. 30 days, not 14 — knowledge-product evals
# need real time to ingest content and live with it. Buyer ICP includes
# enterprise procurement which often takes 2-3 weeks alone.
TRIAL_DAYS = 30

# Threshold below which the trial UI shifts from "info banner" to
# "amber expiring" framing per ADR 019 §"Updated activation/trial UX".
TRIAL_EXPIRING_THRESHOLD_DAYS = 3

# Grace-period length post-expiry per ADR 006 §"Renewal" — read-only mode
# kicks in once this elapses.
GRACE_DAYS = 30


EntitlementStateName = Literal[
    "unlicensed_trial_active",
    "unlicensed_trial_expiring",
    "unlicensed_trial_expired",
    "licensed_active",
    "licensed_in_grace",
    "licensed_past_grace",
    "licensed_invalid",
    # ADR 019 chunk 6 — clock-tampering defense. System clock is more than
    # `CLOCK_ROLLBACK_TOLERANCE` behind max(build_epoch, monotonic_floor).
    # This is a wall state with a different diagnostic UI ("Your system
    # clock appears to be set incorrectly") that points the user at OS
    # date settings rather than at activation flow.
    "clock_invalid",
]


# State buckets for the gate model. ``functional`` = app fully usable.
# ``read_only`` = read paths stay open, write/inference gated. ``wall`` =
# app refuses to function until activation. The chunk-4 service-layer
# gates branch on these.
_FUNCTIONAL_STATES: frozenset[EntitlementStateName] = frozenset({
    "unlicensed_trial_active",
    "unlicensed_trial_expiring",
    "licensed_active",
    "licensed_in_grace",
})
_READ_ONLY_STATES: frozenset[EntitlementStateName] = frozenset({
    "licensed_past_grace",
})
_WALL_STATES: frozenset[EntitlementStateName] = frozenset({
    "unlicensed_trial_expired",
    "licensed_invalid",
    "clock_invalid",
})

# Tolerance window for clock-rollback. A user's clock can legitimately
# drift by a few minutes (NTP sync delay, CMOS battery dying gradually,
# timezone travel mid-flight). We only flag rollbacks larger than 5
# minutes — anything smaller is silently absorbed by max-ing with the
# floor instead. ADR 006 §"Failure UX (not a hard refuse)" — the wall
# is meant for genuinely-broken-clock cases, not normal drift.
CLOCK_ROLLBACK_TOLERANCE = timedelta(minutes=5)


@dataclass(frozen=True)
class EntitlementState:
    """Computed view of the app's entitlement at a moment in time.

    All fields are JSON-serializable so the dataclass can be returned
    directly from the FastAPI router (FastAPI handles dataclass
    serialization since pydantic v2).
    """
    state: EntitlementStateName
    is_functional: bool        # app fully usable (active trial / active license / grace)
    is_read_only: bool         # read paths only (past-grace)
    claims: Optional[dict] = None              # license claims as plain dict (for UI display)
    days_remaining: int = 0    # trial days left, or license days until expiry
    trial_started_at: Optional[str] = None     # ISO; echoed back when in trial state
    expires_at: Optional[str] = None           # ISO; only when license is present
    customer: Optional[str] = None             # only when license is present
    reason: Optional[str] = None               # human-readable, used for diagnostic
    license_id: Optional[str] = None           # only when license is present


# --- Cached inputs (in-process singleton) -----------------------------------
#
# The Tauri shell owns the canonical license_text (a file on disk) and
# trial_started_at (a keychain entry). The backend keeps the most-
# recently-pushed values cached in process so internal callers don't need
# to round-trip through Tauri to check entitlement.

@dataclass
class _CachedInputs:
    license_text: Optional[str] = None
    trial_started_at: Optional[datetime] = None
    monotonic_floor: Optional[datetime] = None


_lock = RLock()
_cache: _CachedInputs = _CachedInputs()


def set_inputs(
    *,
    license_text: Optional[str],
    trial_started_at: Optional[datetime],
    monotonic_floor: Optional[datetime] = None,
) -> None:
    """Push the latest known inputs from the Tauri shell.

    Replaces all three fields atomically — the shell always knows the
    full current state of every surface (it just read them) so partial
    updates are never the right shape.

    ``monotonic_floor`` is the highest UTC timestamp the keychain has
    ever recorded for this install (ADR 019 chunk 6). The state machine
    refuses to compute against a system clock more than
    ``CLOCK_ROLLBACK_TOLERANCE`` behind ``max(build_epoch, monotonic_floor)``.
    """
    with _lock:
        _cache.license_text = license_text
        _cache.trial_started_at = trial_started_at
        _cache.monotonic_floor = monotonic_floor


def get_cached_inputs() -> tuple[Optional[str], Optional[datetime], Optional[datetime]]:
    """Return the most recent (license_text, trial_started_at, monotonic_floor)
    the shell pushed. Used by the GET endpoint and by tests."""
    with _lock:
        return _cache.license_text, _cache.trial_started_at, _cache.monotonic_floor


def reset_cache() -> None:
    """Clear cached inputs. For tests only — production code should never
    call this; the cache is updated by the Tauri shell pushing new values."""
    with _lock:
        _cache.license_text = None
        _cache.trial_started_at = None
        _cache.monotonic_floor = None


# --- State computation -----------------------------------------------------


def effective_now(
    *,
    monotonic_floor: Optional[datetime] = None,
    system_now: Optional[datetime] = None,
) -> datetime:
    """Clock-rollback-resistant "now". ADR 019 chunk 6 / ADR 006 §"Clock-tampering defense".

    Returns ``max(system_now, build_epoch, monotonic_floor)``. The build
    epoch ensures a 2027 binary cannot legitimately think the date is
    2025; the monotonic floor ensures a binary that has already observed
    a later timestamp cannot be tricked by a clock rollback.

    Tests pass ``system_now=`` to pin the clock; production code passes
    only ``monotonic_floor`` (the cache's most recent value pushed by
    the shell) and the system clock is read fresh.
    """
    sys_now = _coerce_utc(system_now or datetime.now(timezone.utc))
    floor = _coerce_utc(monotonic_floor) if monotonic_floor else None
    candidates = [sys_now, BUILD_EPOCH]
    if floor is not None:
        candidates.append(floor)
    return max(candidates)


def is_clock_rolled_back(
    *,
    monotonic_floor: Optional[datetime] = None,
    system_now: Optional[datetime] = None,
) -> bool:
    """True when the system clock is more than ``CLOCK_ROLLBACK_TOLERANCE``
    behind the floor — the diagnostic-wall trigger condition."""
    sys_now = _coerce_utc(system_now or datetime.now(timezone.utc))
    floor = max(BUILD_EPOCH, _coerce_utc(monotonic_floor)) if monotonic_floor else BUILD_EPOCH
    return floor - sys_now > CLOCK_ROLLBACK_TOLERANCE


def compute_state(
    license_text: Optional[str],
    trial_started_at: Optional[datetime],
    *,
    now: Optional[datetime] = None,
    monotonic_floor: Optional[datetime] = None,
) -> EntitlementState:
    """Pure function: inputs + clock → state. No side effects.

    The ``now`` override is kept for testability. ``monotonic_floor`` is
    the highest UTC timestamp the keychain has ever recorded — see
    ADR 019 chunk 6. When the system clock is more than
    ``CLOCK_ROLLBACK_TOLERANCE`` behind the floor, the state is
    ``clock_invalid`` regardless of license/trial.
    """
    sys_now_raw = _coerce_utc(now or datetime.now(timezone.utc))

    if is_clock_rolled_back(monotonic_floor=monotonic_floor, system_now=sys_now_raw):
        return _clock_invalid_state(monotonic_floor, sys_now_raw)

    current = effective_now(
        monotonic_floor=monotonic_floor,
        system_now=sys_now_raw,
    )

    if license_text is None:
        return _trial_state(trial_started_at, current)

    return _licensed_state(license_text, current)


def _clock_invalid_state(
    monotonic_floor: Optional[datetime],
    sys_now: datetime,
) -> EntitlementState:
    floor = max(BUILD_EPOCH, _coerce_utc(monotonic_floor)) if monotonic_floor else BUILD_EPOCH
    return EntitlementState(
        state="clock_invalid",
        is_functional=False,
        is_read_only=False,
        claims=None,
        days_remaining=0,
        trial_started_at=None,
        reason=(
            f"Your system clock appears to be set incorrectly — "
            f"current: {_to_iso(sys_now)}, expected: ≥ {_to_iso(floor)}. "
            "Check your OS date/time settings."
        ),
    )


def _trial_state(
    trial_started_at: Optional[datetime],
    current: datetime,
) -> EntitlementState:
    if trial_started_at is None:
        # The Tauri shell should always initialize the keychain before
        # calling, so this is the "first ever launch, shell hasn't
        # written keychain yet" case. Treat as just-started — the shell
        # writes `current` to keychain, then re-pushes.
        trial_started_at = current
    trial_started_at = _coerce_utc(trial_started_at)

    elapsed = current - trial_started_at
    expires_at = trial_started_at + timedelta(days=TRIAL_DAYS)
    days_remaining = max(0, (expires_at - current).days)
    started_iso = _to_iso(trial_started_at)

    if elapsed >= timedelta(days=TRIAL_DAYS):
        return EntitlementState(
            state="unlicensed_trial_expired",
            is_functional=False,
            is_read_only=False,
            claims=None,
            days_remaining=0,
            trial_started_at=started_iso,
            reason="Trial period (30 days) has ended. Activate a license to continue.",
        )
    if days_remaining <= TRIAL_EXPIRING_THRESHOLD_DAYS:
        return EntitlementState(
            state="unlicensed_trial_expiring",
            is_functional=True,
            is_read_only=False,
            claims=None,
            days_remaining=days_remaining,
            trial_started_at=started_iso,
            reason=None,
        )
    return EntitlementState(
        state="unlicensed_trial_active",
        is_functional=True,
        is_read_only=False,
        claims=None,
        days_remaining=days_remaining,
        trial_started_at=started_iso,
        reason=None,
    )


def _licensed_state(license_text: str, current: datetime) -> EntitlementState:
    result: VerificationResult = verify_license_with_embedded_key(
        license_text, now=current
    )

    if result.valid:
        # Signature good, not expired.
        assert result.claims is not None
        expires_at = _parse_claims_iso(result.claims.expires_at)
        days_remaining = max(0, (expires_at - current).days)
        return _state_with_claims(
            "licensed_active",
            result.claims,
            is_functional=True,
            is_read_only=False,
            days_remaining=days_remaining,
            reason=None,
        )

    # Invalid path — distinguish expired (claims still parseable) from
    # signature/schema failure (claims may be None).
    if result.expired and result.claims is not None:
        expires_at = _parse_claims_iso(result.claims.expires_at)
        grace_elapsed = current - expires_at
        if grace_elapsed <= timedelta(days=GRACE_DAYS):
            grace_left = max(0, GRACE_DAYS - grace_elapsed.days)
            return _state_with_claims(
                "licensed_in_grace",
                result.claims,
                is_functional=True,
                is_read_only=False,
                days_remaining=grace_left,
                reason=(
                    f"License expired; grace period ends in {grace_left} days. "
                    "Renew to keep working."
                ),
            )
        return _state_with_claims(
            "licensed_past_grace",
            result.claims,
            is_functional=False,
            is_read_only=True,
            days_remaining=0,
            reason=(
                "License expired and 30-day grace period has elapsed. "
                "Read-only mode — your data is at "
                "~/DeepBind/memory/ and remains accessible."
            ),
        )

    # Bad signature, malformed wire format, or schema failure. Don't
    # leak claims (may be partial / attacker-supplied).
    return EntitlementState(
        state="licensed_invalid",
        is_functional=False,
        is_read_only=False,
        claims=None,
        days_remaining=0,
        trial_started_at=None,
        reason=result.reason or "License file is invalid.",
    )


def _state_with_claims(
    state_name: EntitlementStateName,
    claims: LicenseClaims,
    *,
    is_functional: bool,
    is_read_only: bool,
    days_remaining: int,
    reason: Optional[str],
) -> EntitlementState:
    """Helper: build an EntitlementState carrying license claims for UI display."""
    return EntitlementState(
        state=state_name,
        is_functional=is_functional,
        is_read_only=is_read_only,
        claims={
            "license_id": claims.license_id,
            "customer": claims.customer,
            "seat_count": claims.seat_count,
            "issued_at": claims.issued_at,
            "expires_at": claims.expires_at,
            "feature_flags": dict(claims.feature_flags),
        },
        days_remaining=days_remaining,
        trial_started_at=None,
        expires_at=claims.expires_at,
        customer=claims.customer,
        license_id=claims.license_id,
        reason=reason,
    )


# --- Cached-state convenience helpers --------------------------------------


def current_state(*, now: Optional[datetime] = None) -> EntitlementState:
    """Compute the entitlement state from the cached inputs.

    Re-computes on every call so day-rollover, grace-period boundaries,
    and clock-rollback detection reflect the current clock without
    requiring a Tauri-side push.
    """
    license_text, trial_started_at, monotonic_floor = get_cached_inputs()
    return compute_state(
        license_text,
        trial_started_at,
        now=now,
        monotonic_floor=monotonic_floor,
    )


def is_functional(*, now: Optional[datetime] = None) -> bool:
    """True when the app should run normally. Used by chunk-4 gates.

    `is_functional=True` covers active trial, expiring trial, valid
    license, and in-grace license. `False` means the gate must block —
    the wall states. Past-grace returns False here AND is_read_only=True;
    callers that want to permit read paths should branch on
    `is_read_only` separately.
    """
    return current_state(now=now).is_functional


def is_read_only(*, now: Optional[datetime] = None) -> bool:
    """True when in past-grace state — read paths allowed, writes gated."""
    return current_state(now=now).is_read_only


# --- ISO helpers -----------------------------------------------------------


def _coerce_utc(dt: datetime) -> datetime:
    """Return a tz-aware UTC datetime. Reattach UTC if naive (defensive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_iso(dt: datetime) -> str:
    """Format as ISO 8601 with `Z` suffix matching the signing-side
    contract from ADR 006."""
    return _coerce_utc(dt).isoformat().replace("+00:00", "Z")


def _parse_claims_iso(iso_text: str) -> datetime:
    """Parse a claims-side ISO timestamp. Trusts that LicenseClaims
    schema validation already enforced UTC awareness."""
    return _coerce_utc(datetime.fromisoformat(iso_text.replace("Z", "+00:00")))
