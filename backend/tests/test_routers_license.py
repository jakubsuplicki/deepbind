"""HTTP-level tests for the license router (ADR 019, chunk 2).

The state-machine logic is covered exhaustively in test_entitlements.py.
This module pins the *transport contract* — request body validation,
response shape, and the cache-pushing behaviour of POST vs. the
recompute-from-cache behaviour of GET.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services import entitlements
from services.license_service import (
    LicenseClaims,
    serialize_license,
    sign_license,
)

DEV_PRIVATE = (
    Path(__file__).parent / "fixtures" / "license_dev_keys" / "private.key"
).read_bytes()


def _signed(*, expires_at: datetime, customer: str = "Acme") -> str:
    issued = expires_at - timedelta(days=395)
    claims = LicenseClaims(
        license_id="lic_router_test",
        customer=customer,
        seat_count=10,
        issued_at=issued.isoformat().replace("+00:00", "Z"),
        expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        feature_flags={},
    )
    sig = sign_license(claims, DEV_PRIVATE)
    return serialize_license(claims, sig)


@pytest.fixture(autouse=True)
def _reset_cache():
    entitlements.reset_cache()
    yield
    entitlements.reset_cache()


@pytest.mark.asyncio
async def test_post_state_with_no_inputs_returns_just_started_trial(client):
    """First-launch shape: shell calls POST with both fields null. Backend
    treats as just-started trial, echoes back trial_started_at."""
    resp = await client.post("/api/license/state", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "unlicensed_trial_active"
    assert body["is_functional"] is True
    assert body["days_remaining"] == entitlements.TRIAL_DAYS
    assert body["trial_started_at"] is not None
    assert body["claims"] is None


@pytest.mark.asyncio
async def test_post_state_with_explicit_trial_start(client):
    """Subsequent launch: shell reads keychain, posts the stored ISO string."""
    started = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    resp = await client.post(
        "/api/license/state",
        json={"trial_started_at": started},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "unlicensed_trial_active"
    assert 19 <= body["days_remaining"] <= 20  # depends on exact second precision


@pytest.mark.asyncio
async def test_post_state_with_valid_license(client):
    """License path: shell posts the file contents, backend verifies + caches."""
    license_text = _signed(
        expires_at=datetime.now(timezone.utc) + timedelta(days=300),
        customer="Boutique Law LLP",
    )
    resp = await client.post(
        "/api/license/state",
        json={"license_text": license_text},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "licensed_active"
    assert body["customer"] == "Boutique Law LLP"
    assert body["claims"]["customer"] == "Boutique Law LLP"
    assert body["claims"]["license_id"] == "lic_router_test"


@pytest.mark.asyncio
async def test_post_state_with_invalid_license_returns_invalid_state(client):
    """Garbage license_text → state=licensed_invalid, no 4xx/5xx."""
    resp = await client.post(
        "/api/license/state",
        json={"license_text": "not.a.real.license"},
    )
    assert resp.status_code == 200  # validation result is a state, not a 400
    body = resp.json()
    assert body["state"] == "licensed_invalid"
    assert body["is_functional"] is False
    assert body["claims"] is None


@pytest.mark.asyncio
async def test_post_state_rejects_naive_trial_start(client):
    """Shell-side bug protection: naive ISO timestamp is a contract violation,
    not silently UTC-coerced. Mirrors the LicenseClaims.expires_at contract."""
    naive = "2026-05-05T12:00:00"  # no Z, no offset
    resp = await client.post(
        "/api/license/state",
        json={"trial_started_at": naive},
    )
    assert resp.status_code == 422
    detail = resp.json()
    assert "timezone-aware" in str(detail)


@pytest.mark.asyncio
async def test_post_state_rejects_malformed_iso(client):
    resp = await client.post(
        "/api/license/state",
        json={"trial_started_at": "not-iso-at-all"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_state_uses_pushed_cache(client):
    """GET re-reads cache and recomputes. Pin the integration with POST."""
    license_text = _signed(
        expires_at=datetime.now(timezone.utc) + timedelta(days=200),
    )
    await client.post("/api/license/state", json={"license_text": license_text})

    resp = await client.get("/api/license/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "licensed_active"
    assert body["customer"] == "Acme"


@pytest.mark.asyncio
async def test_get_state_with_empty_cache_returns_first_launch_trial(client):
    """No prior POST → cache is empty → state machine treats as first launch."""
    resp = await client.get("/api/license/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "unlicensed_trial_active"
    assert body["days_remaining"] == entitlements.TRIAL_DAYS


@pytest.mark.asyncio
async def test_post_overwrites_previous_inputs(client):
    """Shell pushing a license over a prior trial-start state replaces both."""
    # Push trial start.
    started = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    await client.post("/api/license/state", json={"trial_started_at": started})
    cached = entitlements.get_cached_inputs()
    assert cached[0] is None  # license_text
    assert cached[1] is not None  # trial_started_at
    assert cached[2] is None  # monotonic_floor

    # Push license — should clear trial-side input.
    license_text = _signed(
        expires_at=datetime.now(timezone.utc) + timedelta(days=200),
    )
    resp = await client.post(
        "/api/license/state",
        json={"license_text": license_text},
    )
    assert resp.status_code == 200
    cached_after = entitlements.get_cached_inputs()
    assert cached_after[0] == license_text
    assert cached_after[1] is None  # trial_started_at cleared
    assert cached_after[2] is None  # monotonic_floor cleared too
