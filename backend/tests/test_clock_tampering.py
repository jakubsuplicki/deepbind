"""Tests for the clock-tampering defense (ADR 019, chunk 6).

Two layers:

1. ``effective_now()`` returns ``max(system_now, build_epoch, monotonic_floor)``
   — used as the clock by every entitlement computation.
2. ``is_clock_rolled_back()`` detects when the system clock is more than
   ``CLOCK_ROLLBACK_TOLERANCE`` behind the floor — the trigger for the
   ``clock_invalid`` state.

The dev build epoch is `2020-01-01T00:00:00Z` (a far-past date that
never blocks dev work). Tests pass `system_now=` explicitly to pin the
clock to whatever shape they need.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services import build_epoch, entitlements
from services.license_service import (
    LicenseClaims, serialize_license, sign_license,
)


DEV_PRIVATE = (
    Path(__file__).parent / "fixtures" / "license_dev_keys" / "private.key"
).read_bytes()


def _utc(year: int, month: int = 6, day: int = 1) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=timezone.utc)


def _signed(*, expires_at: datetime) -> str:
    issued = expires_at - timedelta(days=395)
    claims = LicenseClaims(
        license_id="lic_clock_test",
        customer="Acme",
        seat_count=5,
        issued_at=issued.isoformat().replace("+00:00", "Z"),
        expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        feature_flags={},
    )
    return serialize_license(claims, sign_license(claims, DEV_PRIVATE))


@pytest.fixture(autouse=True)
def _reset_cache():
    entitlements.reset_cache()
    yield
    entitlements.reset_cache()


# --- effective_now -----------------------------------------------------------


class TestEffectiveNow:

    def test_returns_system_now_when_floors_are_lower(self):
        """Dev build epoch (2020) is below 2026 system clock — system wins."""
        sys_now = _utc(2026)
        eff = entitlements.effective_now(system_now=sys_now)
        assert eff == sys_now

    def test_returns_monotonic_floor_when_higher_than_system(self):
        """Clock rolled back to 2024 with floor at 2026 → floor wins."""
        sys_now = _utc(2024)
        floor = _utc(2026)
        eff = entitlements.effective_now(
            system_now=sys_now,
            monotonic_floor=floor,
        )
        assert eff == floor

    def test_returns_build_epoch_when_system_is_below_it(self):
        """System clock impossibly old (e.g. 1999) → build epoch wins."""
        sys_now = datetime(1999, 1, 1, tzinfo=timezone.utc)
        eff = entitlements.effective_now(system_now=sys_now)
        assert eff == build_epoch.BUILD_EPOCH

    def test_max_of_all_three(self):
        """Real production case: monotonic_floor advanced past build epoch
        because the user has been running the app, then clock rolled back."""
        sys_now = _utc(2025)
        floor = _utc(2026, 6, 15)
        eff = entitlements.effective_now(
            system_now=sys_now,
            monotonic_floor=floor,
        )
        assert eff == floor


# --- is_clock_rolled_back --------------------------------------------------


class TestIsClockRolledBack:

    def test_normal_clock_is_not_rolled_back(self):
        """System clock at 2026, no floor → not rolled back (build epoch is 2020)."""
        assert (
            entitlements.is_clock_rolled_back(system_now=_utc(2026))
            is False
        )

    def test_within_tolerance_window(self):
        """Clock 2 minutes behind floor → tolerated (allows for NTP drift,
        normal CMOS-battery decay, brief power-off recovery)."""
        floor = datetime(2026, 6, 1, 12, 5, tzinfo=timezone.utc)
        sys_now = datetime(2026, 6, 1, 12, 3, tzinfo=timezone.utc)  # 2 min behind
        assert (
            entitlements.is_clock_rolled_back(
                system_now=sys_now,
                monotonic_floor=floor,
            )
            is False
        )

    def test_beyond_tolerance_is_rolled_back(self):
        """Clock 10 minutes behind floor → rolled back."""
        floor = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        sys_now = datetime(2026, 6, 1, 11, 50, tzinfo=timezone.utc)  # 10 min behind
        assert (
            entitlements.is_clock_rolled_back(
                system_now=sys_now,
                monotonic_floor=floor,
            )
            is True
        )

    def test_clock_below_build_epoch_is_rolled_back(self):
        """Even with no monotonic_floor, a clock pre-build-epoch trips the check."""
        sys_now = datetime(1999, 1, 1, tzinfo=timezone.utc)
        assert entitlements.is_clock_rolled_back(system_now=sys_now) is True


# --- clock_invalid state ---------------------------------------------------


class TestClockInvalidState:

    def test_clock_invalid_overrides_license_state(self):
        """If clock is rolled back, the state is clock_invalid regardless
        of whether a valid license is present."""
        floor = _utc(2026)
        license_text = _signed(expires_at=_utc(2027))
        sys_now = _utc(2025)  # 1 year behind floor

        state = entitlements.compute_state(
            license_text,
            None,
            now=sys_now,
            monotonic_floor=floor,
        )
        assert state.state == "clock_invalid"
        assert state.is_functional is False
        assert state.claims is None  # don't leak partial info
        assert "clock" in (state.reason or "").lower()

    def test_clock_invalid_overrides_trial_state(self):
        """Same precedence for the trial path."""
        floor = _utc(2026)
        sys_now = _utc(2025)

        state = entitlements.compute_state(
            None,
            sys_now,  # trial just started (irrelevant — clock check fires first)
            now=sys_now,
            monotonic_floor=floor,
        )
        assert state.state == "clock_invalid"

    def test_normal_clock_with_floor_lets_license_through(self):
        """When the clock is fine (within tolerance), the license check runs normally."""
        floor = _utc(2026)
        sys_now = _utc(2026, 6, 5)  # 4 days after the floor — fine
        license_text = _signed(expires_at=_utc(2027))

        state = entitlements.compute_state(
            license_text,
            None,
            now=sys_now,
            monotonic_floor=floor,
        )
        assert state.state == "licensed_active"

    def test_clock_invalid_is_a_wall_state_via_is_functional(self):
        """The chunk-4 service-layer gate must block in clock_invalid."""
        floor = _utc(2026)
        entitlements.set_inputs(
            license_text=None,
            trial_started_at=None,
            monotonic_floor=floor,
        )
        # Force the clock to look rolled back. We can't easily mock
        # datetime.now in the gate path, so use compute_state directly
        # through current_state's input-cache rather than via gate HTTP.
        # is_functional reads through current_state which uses real now;
        # to simulate clock_invalid we need the floor > real now.
        far_future_floor = datetime.now(timezone.utc) + timedelta(days=400)
        entitlements.set_inputs(
            license_text=None,
            trial_started_at=None,
            monotonic_floor=far_future_floor,
        )
        # is_functional reads cached inputs; floor is way ahead of system
        # clock → state should be clock_invalid → not functional.
        assert entitlements.is_functional() is False


# --- monotonic floor advance contract --------------------------------------


class TestMonotonicAdvance:
    """The Tauri side advances the keychain floor via response.effective_now.
    These tests pin the contract that the backend echoes back the right
    value to advance to."""

    @pytest.mark.asyncio
    async def test_response_includes_effective_now(self, client):
        """POST /api/license/state response carries an effective_now field
        the Tauri shell can persist as the new floor."""
        resp = await client.post("/api/license/state", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert "effective_now" in body
        # ISO 8601 with `Z` suffix.
        assert body["effective_now"].endswith("Z")

    @pytest.mark.asyncio
    async def test_get_response_also_includes_effective_now(self, client):
        """GET also carries effective_now so the shell can advance the
        floor on plain refreshes (focus events, polling)."""
        resp = await client.get("/api/license/state")
        assert resp.status_code == 200
        assert "effective_now" in resp.json()

    @pytest.mark.asyncio
    async def test_floor_passed_in_round_trips(self, client):
        """Posted monotonic_floor reaches the cache; recomputed state uses it."""
        floor = (datetime.now(timezone.utc) + timedelta(days=400)).isoformat().replace(
            "+00:00", "Z"
        )
        resp = await client.post(
            "/api/license/state",
            json={"monotonic_floor": floor},
        )
        body = resp.json()
        assert body["state"] == "clock_invalid"
        assert body["is_functional"] is False
