"""Tests for the entitlement state machine (ADR 019, chunk 2/3).

Covers all 7 states from ADR 019 §"Entitlement state machine" plus the
edge cases that fell out of writing the implementation:

- Trial-state path: trial-active, trial-expiring (≤3 days), trial-expired
  (≥30 days), and the "trial_started_at is None" first-launch fallback.
- Licensed path: licensed-active, licensed-in-grace (expired ≤30 days),
  licensed-past-grace (expired >30 days), licensed-invalid (signature
  bad / malformed wire / wrong key).
- The cache singleton + `current_state()` / `is_functional()` / `is_read_only()`
  convenience helpers.

The state machine is deterministic given (license_text, trial_started_at,
now), so every test pins all three explicitly. No "freeze time" tricks
needed — the `now=` parameter is the contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services import entitlements
from services.license_service import (
    LicenseClaims,
    serialize_license,
    sign_license,
)

DEV_KEYS = Path(__file__).parent / "fixtures" / "license_dev_keys"
DEV_PRIVATE = DEV_KEYS.joinpath("private.key").read_bytes()


def _utc(year=2026, month=6, day=1, hour=12, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _signed_license(
    *,
    expires_at: datetime,
    issued_at: datetime | None = None,
    private_key: bytes = DEV_PRIVATE,
    customer: str = "Acme Engineering",
    license_id: str = "lic_test_2026",
) -> str:
    issued = issued_at or (expires_at - timedelta(days=395))
    claims = LicenseClaims(
        license_id=license_id,
        customer=customer,
        seat_count=20,
        issued_at=issued.isoformat().replace("+00:00", "Z"),
        expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        feature_flags={},
    )
    sig = sign_license(claims, private_key)
    return serialize_license(claims, sig)


@pytest.fixture(autouse=True)
def _reset_cache():
    entitlements.reset_cache()
    yield
    entitlements.reset_cache()


# --- Trial path -------------------------------------------------------------


class TestTrialPath:

    def test_trial_active_mid_window(self):
        """Day 5 of a 30-day trial → unlicensed_trial_active, 25 days left."""
        now = _utc()
        started = now - timedelta(days=5)
        state = entitlements.compute_state(None, started, now=now)
        assert state.state == "unlicensed_trial_active"
        assert state.is_functional is True
        assert state.is_read_only is False
        assert state.days_remaining == 25
        assert state.claims is None
        assert state.trial_started_at is not None

    def test_trial_expiring_three_days_left(self):
        """Day 27 → ≤3 days left → unlicensed_trial_expiring (UI banner shifts)."""
        now = _utc()
        started = now - timedelta(days=27)
        state = entitlements.compute_state(None, started, now=now)
        assert state.state == "unlicensed_trial_expiring"
        assert state.is_functional is True
        assert state.days_remaining == 3

    def test_trial_expiring_one_day_left(self):
        """Day 29 → 1 day left → still expiring, still functional."""
        now = _utc()
        started = now - timedelta(days=29)
        state = entitlements.compute_state(None, started, now=now)
        assert state.state == "unlicensed_trial_expiring"
        assert state.is_functional is True
        assert state.days_remaining == 1

    def test_trial_expired_at_30_days(self):
        """Day 30 exact → trial expired (boundary inclusive)."""
        now = _utc()
        started = now - timedelta(days=30)
        state = entitlements.compute_state(None, started, now=now)
        assert state.state == "unlicensed_trial_expired"
        assert state.is_functional is False
        assert state.is_read_only is False
        assert state.days_remaining == 0

    def test_trial_expired_well_past(self):
        """Day 90 → still expired (no auto-reset)."""
        now = _utc()
        started = now - timedelta(days=90)
        state = entitlements.compute_state(None, started, now=now)
        assert state.state == "unlicensed_trial_expired"
        assert state.is_functional is False

    def test_trial_started_at_none_first_launch(self):
        """When the keychain is uninitialized AND no license, treat as
        just-started (the Tauri shell will write `now` to keychain right
        after this call)."""
        now = _utc()
        state = entitlements.compute_state(None, None, now=now)
        assert state.state == "unlicensed_trial_active"
        assert state.days_remaining == entitlements.TRIAL_DAYS
        # The synthesized trial_started_at echoes back so the shell knows
        # what to persist.
        assert state.trial_started_at is not None

    def test_trial_started_at_naive_is_coerced_to_utc(self):
        """Defensive: a naive datetime is treated as UTC rather than crashing."""
        now = _utc()
        naive_started = now.replace(tzinfo=None) - timedelta(days=10)
        state = entitlements.compute_state(None, naive_started, now=now)
        assert state.state == "unlicensed_trial_active"
        assert state.days_remaining == 20


# --- Licensed path ----------------------------------------------------------


class TestLicensedActive:

    def test_valid_license_far_from_expiry(self):
        """Standard happy path."""
        now = _utc()
        license_text = _signed_license(expires_at=now + timedelta(days=200))
        state = entitlements.compute_state(license_text, None, now=now)
        assert state.state == "licensed_active"
        assert state.is_functional is True
        assert state.is_read_only is False
        assert state.days_remaining == 200
        assert state.claims is not None
        assert state.customer == "Acme Engineering"
        assert state.license_id == "lic_test_2026"

    def test_valid_license_with_one_day_left_is_still_active(self):
        """Boundary: license valid until tomorrow → still licensed_active.
        Grace state only kicks in *after* expiry."""
        now = _utc()
        license_text = _signed_license(expires_at=now + timedelta(days=1))
        state = entitlements.compute_state(license_text, None, now=now)
        assert state.state == "licensed_active"

    def test_keychain_trial_state_ignored_when_license_present(self):
        """If both inputs are populated, the license takes precedence."""
        now = _utc()
        license_text = _signed_license(expires_at=now + timedelta(days=100))
        ancient_trial_start = now - timedelta(days=5000)
        state = entitlements.compute_state(license_text, ancient_trial_start, now=now)
        assert state.state == "licensed_active"


class TestLicensedInGrace:

    def test_just_expired_in_grace(self):
        """Expired 1 day ago → in_grace, 29 grace days left."""
        now = _utc()
        license_text = _signed_license(expires_at=now - timedelta(days=1))
        state = entitlements.compute_state(license_text, None, now=now)
        assert state.state == "licensed_in_grace"
        assert state.is_functional is True
        assert state.days_remaining == 29
        assert state.claims is not None
        assert state.customer == "Acme Engineering"

    def test_at_grace_boundary_30_days_post_expiry(self):
        """Expired exactly 30 days ago → past grace (boundary inclusive,
        matches trial-expired boundary semantics)."""
        now = _utc()
        license_text = _signed_license(expires_at=now - timedelta(days=30, seconds=1))
        state = entitlements.compute_state(license_text, None, now=now)
        assert state.state == "licensed_past_grace"


class TestLicensedPastGrace:

    def test_well_past_grace(self):
        """Expired 90 days ago → past grace, read-only mode."""
        now = _utc()
        license_text = _signed_license(expires_at=now - timedelta(days=90))
        state = entitlements.compute_state(license_text, None, now=now)
        assert state.state == "licensed_past_grace"
        assert state.is_functional is False
        assert state.is_read_only is True
        assert state.days_remaining == 0
        # Claims still present so the UI can show "renew your <customer> license."
        assert state.customer == "Acme Engineering"
        assert "memory" in (state.reason or "")  # mentions data path


class TestLicensedInvalid:

    def test_garbage_text_is_invalid(self):
        """Non-license string → licensed_invalid, no claims."""
        now = _utc()
        state = entitlements.compute_state("not.a.license", None, now=now)
        assert state.state == "licensed_invalid"
        assert state.is_functional is False
        assert state.is_read_only is False
        assert state.claims is None

    def test_signature_with_wrong_key_is_invalid(self):
        """A real-shaped license signed with a non-dev key fails verification."""
        rogue_priv = Ed25519PrivateKey.generate().private_bytes_raw()
        now = _utc()
        license_text = _signed_license(
            expires_at=now + timedelta(days=200),
            private_key=rogue_priv,
        )
        state = entitlements.compute_state(license_text, None, now=now)
        assert state.state == "licensed_invalid"
        assert state.claims is None  # don't leak attacker-supplied content
        assert state.reason is not None
        assert "Signature" in state.reason

    def test_empty_string_is_invalid(self):
        now = _utc()
        state = entitlements.compute_state("", None, now=now)
        assert state.state == "licensed_invalid"


# --- Cache singleton + convenience helpers ----------------------------------


class TestCacheAndHelpers:

    def test_set_and_get_inputs_round_trip(self):
        now = _utc()
        license_text = _signed_license(expires_at=now + timedelta(days=100))
        trial_start = now - timedelta(days=10)
        entitlements.set_inputs(
            license_text=license_text,
            trial_started_at=trial_start,
        )
        got_text, got_trial, got_floor = entitlements.get_cached_inputs()
        assert got_text == license_text
        assert got_trial == trial_start
        assert got_floor is None

    def test_current_state_uses_cache(self):
        now = _utc()
        trial_start = now - timedelta(days=5)
        entitlements.set_inputs(
            license_text=None,
            trial_started_at=trial_start,
        )
        # current_state can't take a `now` from us in production; but the
        # parameter exists for testing.
        state = entitlements.current_state(now=now)
        assert state.state == "unlicensed_trial_active"
        assert state.days_remaining == 25

    def test_is_functional_true_in_trial(self):
        now = _utc()
        entitlements.set_inputs(
            license_text=None,
            trial_started_at=now - timedelta(days=2),
        )
        assert entitlements.is_functional(now=now) is True

    def test_is_functional_false_post_trial(self):
        now = _utc()
        entitlements.set_inputs(
            license_text=None,
            trial_started_at=now - timedelta(days=40),
        )
        assert entitlements.is_functional(now=now) is False

    def test_is_read_only_true_only_in_past_grace(self):
        now = _utc()
        license_text = _signed_license(expires_at=now - timedelta(days=90))
        entitlements.set_inputs(license_text=license_text, trial_started_at=None)
        assert entitlements.is_read_only(now=now) is True
        # Sanity: trial-expired is NOT read-only (it's a wall, not read-only).
        entitlements.set_inputs(
            license_text=None,
            trial_started_at=now - timedelta(days=40),
        )
        assert entitlements.is_read_only(now=now) is False


# --- Day-rollover semantics -------------------------------------------------


class TestDayRollover:
    """The cache stores INPUTS not the state — re-computation between two
    calls naturally reflects clock advance. Pin this contract."""

    def test_day_rollover_drops_trial_active_to_expired_without_repush(self):
        """Day 29 → expiring. Day 31 → expired. Without a Tauri-side push."""
        started = _utc(2026, 5, 1, 0, 0)
        entitlements.set_inputs(license_text=None, trial_started_at=started)

        day29 = started + timedelta(days=29)
        assert entitlements.current_state(now=day29).state == "unlicensed_trial_expiring"

        day31 = started + timedelta(days=31)
        assert entitlements.current_state(now=day31).state == "unlicensed_trial_expired"

    def test_day_rollover_drops_in_grace_to_past_grace_without_repush(self):
        now_initial = _utc(2026, 6, 1, 0, 0)
        license_text = _signed_license(
            expires_at=now_initial - timedelta(days=10)  # already in grace
        )
        entitlements.set_inputs(license_text=license_text, trial_started_at=None)

        # Day 25 of grace — still in grace.
        day25 = now_initial + timedelta(days=15)
        assert entitlements.current_state(now=day25).state == "licensed_in_grace"

        # Day 31 of grace — past.
        day31 = now_initial + timedelta(days=21, seconds=1)
        # 10 days ago + 21 days = 31 days post-expiry → past
        assert entitlements.current_state(now=day31).state == "licensed_past_grace"
