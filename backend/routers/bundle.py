"""Bundle-capability probe (ADR 014 §90 audit verification path 2).

Exposes the same capability list shipped in the .app's `Info.plist` as a
live HTTP endpoint so a buyer's compliance probe can verify the running
shell at curl-distance:

    curl http://127.0.0.1:<sidecar_port>/api/bundle/capabilities
    # → {"capabilities": ["local-llm", "vault-markdown", ...],
    #    "is_desktop_bundle": true,
    #    "cloud_providers_available": false}

Always registered (regardless of `JARVIS_DESKTOP_BUNDLE`) — the audit
signal *is* the response payload, not the endpoint's presence/absence.
A buyer probing this on a hybrid build sees `cloud-llm` in the array
and `cloud_providers_available=true`; on the local-only build, those
absent / false.
"""

from fastapi import APIRouter

from services.bundle import bundle_capabilities, cloud_providers_available, is_desktop_bundle

router = APIRouter(prefix="/api/bundle", tags=["bundle"])


@router.get("/capabilities")
async def get_capabilities():
    return {
        "capabilities": bundle_capabilities(),
        "is_desktop_bundle": is_desktop_bundle(),
        "cloud_providers_available": cloud_providers_available(),
    }
