"""Tests for the service-layer entitlement gate (ADR 019, chunk 4).

The conftest sets `JARVIS_LICENSE_GATE_BYPASS=1` so most tests don't
need to set up license context. These tests **clear that env var
locally** via monkeypatch so the gate actually fires, then push
specific entitlement states into the cache and pin the response.

Covers:

- Functional states (trial-active, in-grace) → gate passes (request
  reaches the endpoint, gets its normal response).
- Wall states (trial-expired, licensed-invalid) → 403 with the
  `license_required` payload.
- Past-grace state → 403 (writes blocked) but read paths unaffected
  (because read endpoints don't declare the gate).
- Bypass env var → gate is a no-op even when state is unfunctional.

We don't enumerate every gated endpoint here — the gate is a single
dependency declared in many places; testing one representative call
per response shape pins the contract. The wider integration tests in
the existing routers' test suites confirm the dependency doesn't
leak unwanted behaviour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import entitlements


@pytest.fixture(autouse=True)
def _disable_gate_bypass(monkeypatch):
    """Force the gate to actually fire — the conftest default is bypass-on."""
    monkeypatch.delenv("JARVIS_LICENSE_GATE_BYPASS", raising=False)
    entitlements.reset_cache()
    yield
    entitlements.reset_cache()


def _set_trial_state(*, days_ago: int) -> None:
    """Push a trial-state input into the cache equivalent to "trial started N days ago"."""
    started = datetime.now(timezone.utc) - timedelta(days=days_ago)
    entitlements.set_inputs(license_text=None, trial_started_at=started)


# --- Wall-state behaviour ---------------------------------------------------


@pytest.mark.asyncio
async def test_gate_blocks_write_when_trial_expired(client):
    """Trial expired → POST to a gated endpoint returns 403 with payload."""
    _set_trial_state(days_ago=40)

    resp = await client.post(
        "/api/memory/notes/test-note.md",
        json={"content": "should be blocked"},
    )
    assert resp.status_code == 403
    body = resp.json()
    # FastAPI wraps `detail` from HTTPException; our payload puts the
    # structured shape inside detail.
    detail = body.get("detail")
    assert isinstance(detail, dict)
    assert detail["detail"] == "license_required"
    assert detail["state"]["state"] == "unlicensed_trial_expired"


@pytest.mark.asyncio
async def test_gate_blocks_chat_send_when_trial_expired(client):
    """Most load-bearing gate: the inference path."""
    _set_trial_state(days_ago=40)

    resp = await client.post(
        "/api/chat/message",
        json={"type": "user", "content": "hello", "session_id": "s1"},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["detail"] == "license_required"


@pytest.mark.asyncio
async def test_gate_blocks_writes_in_past_grace(client):
    """Past-grace = read-only — writes blocked, reads still pass."""
    # Build a license that expired 40 days ago (>30 day grace).
    from pathlib import Path

    from services.license_service import (
        LicenseClaims, serialize_license, sign_license,
    )

    priv = (
        Path(__file__).parent / "fixtures" / "license_dev_keys" / "private.key"
    ).read_bytes()
    now = datetime.now(timezone.utc)
    claims = LicenseClaims(
        license_id="lic_past_grace",
        customer="Acme",
        seat_count=5,
        issued_at=(now - timedelta(days=400)).isoformat().replace("+00:00", "Z"),
        expires_at=(now - timedelta(days=40)).isoformat().replace("+00:00", "Z"),
        feature_flags={},
    )
    license_text = serialize_license(claims, sign_license(claims, priv))
    entitlements.set_inputs(license_text=license_text, trial_started_at=None)

    # Write blocked.
    resp = await client.post(
        "/api/memory/notes/test-note.md",
        json={"content": "should be blocked"},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["state"]["state"] == "licensed_past_grace"
    assert detail["state"]["is_read_only"] is True


@pytest.mark.asyncio
async def test_gate_blocks_invalid_license(client):
    """Garbage license_text → state=licensed_invalid → 403."""
    entitlements.set_inputs(
        license_text="not.a.real.license",
        trial_started_at=None,
    )

    resp = await client.post(
        "/api/memory/notes/test-note.md",
        json={"content": "x"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["state"]["state"] == "licensed_invalid"


# --- Functional states pass the gate ---------------------------------------


@pytest.mark.asyncio
async def test_gate_allows_writes_in_active_trial(client):
    """Trial-active → gate passes → request reaches the endpoint
    (will then succeed/fail on the endpoint's own logic — all we check
    is that it's *not* the 403 license_required block)."""
    _set_trial_state(days_ago=5)

    resp = await client.post(
        "/api/memory/notes/test-trial-active.md",
        json={"content": "hello"},
    )
    # The gate let it through. The endpoint may 4xx for other reasons
    # (workspace setup, validation) but it did NOT 403 license_required.
    if resp.status_code == 403:
        body = resp.json()
        detail = body.get("detail")
        if isinstance(detail, dict):
            assert detail.get("detail") != "license_required", body


@pytest.mark.asyncio
async def test_gate_allows_writes_with_valid_license(client):
    """licensed_active → gate passes."""
    from pathlib import Path

    from services.license_service import (
        LicenseClaims, serialize_license, sign_license,
    )

    priv = (
        Path(__file__).parent / "fixtures" / "license_dev_keys" / "private.key"
    ).read_bytes()
    now = datetime.now(timezone.utc)
    claims = LicenseClaims(
        license_id="lic_test",
        customer="Acme",
        seat_count=5,
        issued_at=now.isoformat().replace("+00:00", "Z"),
        expires_at=(now + timedelta(days=200)).isoformat().replace("+00:00", "Z"),
        feature_flags={},
    )
    license_text = serialize_license(claims, sign_license(claims, priv))
    entitlements.set_inputs(license_text=license_text, trial_started_at=None)

    resp = await client.post(
        "/api/memory/notes/test-licensed.md",
        json={"content": "hello"},
    )
    if resp.status_code == 403:
        body = resp.json()
        detail = body.get("detail")
        if isinstance(detail, dict):
            assert detail.get("detail") != "license_required", body


# --- Read paths never gated ------------------------------------------------


@pytest.mark.asyncio
async def test_read_path_unaffected_by_expired_trial(client):
    """Listing notes is a read path — must not be gated."""
    _set_trial_state(days_ago=40)

    resp = await client.get("/api/memory/notes")
    # 200 with a list, or some other non-403; the contract is "not blocked
    # by license gate" — anything other than 403 license_required is fine.
    if resp.status_code == 403:
        body = resp.json()
        detail = body.get("detail")
        if isinstance(detail, dict):
            assert detail.get("detail") != "license_required", body


@pytest.mark.asyncio
async def test_read_path_unaffected_by_past_grace(client):
    """Past-grace is read-only — read endpoints must always work."""
    from pathlib import Path

    from services.license_service import (
        LicenseClaims, serialize_license, sign_license,
    )

    priv = (
        Path(__file__).parent / "fixtures" / "license_dev_keys" / "private.key"
    ).read_bytes()
    now = datetime.now(timezone.utc)
    claims = LicenseClaims(
        license_id="lic_past",
        customer="Acme",
        seat_count=5,
        issued_at=(now - timedelta(days=400)).isoformat().replace("+00:00", "Z"),
        expires_at=(now - timedelta(days=40)).isoformat().replace("+00:00", "Z"),
        feature_flags={},
    )
    license_text = serialize_license(claims, sign_license(claims, priv))
    entitlements.set_inputs(license_text=license_text, trial_started_at=None)

    resp = await client.get("/api/memory/notes")
    if resp.status_code == 403:
        body = resp.json()
        detail = body.get("detail")
        if isinstance(detail, dict):
            assert detail.get("detail") != "license_required", body


# --- License endpoint always works -----------------------------------------


@pytest.mark.asyncio
async def test_license_endpoint_never_gated(client):
    """The license/state endpoint must always be reachable — otherwise
    the frontend can't recover from licensed_invalid (it needs to GET
    state to know what to display on the wall)."""
    _set_trial_state(days_ago=40)
    resp = await client.get("/api/license/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "unlicensed_trial_expired"


# --- Bypass env var --------------------------------------------------------


@pytest.mark.asyncio
async def test_bypass_env_disables_gate(client, monkeypatch):
    """JARVIS_LICENSE_GATE_BYPASS=1 → gate is a no-op even in wall states."""
    monkeypatch.setenv("JARVIS_LICENSE_GATE_BYPASS", "1")
    _set_trial_state(days_ago=40)  # trial expired

    resp = await client.post(
        "/api/memory/notes/test-bypass.md",
        json={"content": "would be blocked without bypass"},
    )
    if resp.status_code == 403:
        body = resp.json()
        detail = body.get("detail")
        if isinstance(detail, dict):
            assert detail.get("detail") != "license_required", body
