"""API-keys router (ADR 014 §C — load-bearing route-level gating).

Hosts the historical ``PATCH /api/settings/api-key`` endpoint. The handler
is a no-op kept for API compatibility — Jarvis API keys are managed in
the browser (localStorage / sessionStorage) per ADR 002 §"pure local
product shape," so the server never sees the raw key. The endpoint exists
purely so callers that haven't migrated to the browser-stored convention
get a deterministic 200 instead of a confusing 404.

Per ADR 014 §C the audit signal is *the route does not exist* in the
desktop bundle. This router is conditionally registered in ``main.py``
based on ``JARVIS_DESKTOP_BUNDLE``: when the flag is on (the v1 default)
the router is not included, and `PATCH /api/settings/api-key` returns a
plain 404. When the flag is off (hybrid / dev SKU) the router is wired
up and the no-op handler runs as before.

Why a separate router rather than a single conditionally-decorated
handler inside ``settings.py``: clean route-level signal for the audit
script. A buyer can grep for the api-keys router file in the bundle and
confirm it isn't being included from ``main.py``.
"""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/settings", tags=["api-keys"])


@router.patch("/api-key")
async def update_api_key(body: dict):
    """No-op endpoint kept for API compatibility (see module docstring).

    Validates that a non-empty key was sent and returns ``{api_key_set: True}``.
    Real key storage is in the browser; this handler does not persist the
    key server-side. ADR 014 §C: the desktop bundle does not register this
    router so the route returns 404.
    """
    key = body.get("api_key", "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="API key must not be empty")
    return {"api_key_set": True}
