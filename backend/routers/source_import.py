import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException

from services.entitlement_gate import require_functional
from services.source_import.grants import (
    SourceGrantError,
    consume_grant,
    create_grant,
)
from services.source_import.models import (
    SourceGrantRequest,
    SourceGrantResponse,
    SourceScanReport,
    SourceScanRequest,
)
from services.source_import.scan import scan_folder
from services.source_import.store import get_scan, new_scan_id, save_scan


router = APIRouter(prefix="/api/source-import", tags=["source-import"])


def _require_shell_grant_token(value: str | None) -> None:
    expected = os.environ.get("JARVIS_SOURCE_IMPORT_GRANT_TOKEN")
    if not expected:
        raise HTTPException(status_code=403, detail="Source grants are disabled")
    if not value or not hmac.compare_digest(value, expected):
        raise HTTPException(status_code=403, detail="Invalid source grant token")


@router.post(
    "/grants",
    response_model=SourceGrantResponse,
    dependencies=[Depends(require_functional)],
)
async def create_source_grant_endpoint(
    body: SourceGrantRequest,
    x_deepfiles_shell_token: str | None = Header(default=None),
) -> SourceGrantResponse:
    """Create a short-lived source grant from a trusted desktop picker path.

    This endpoint intentionally requires a shell-only token. The public scan
    endpoint accepts the resulting source token, not arbitrary local paths.
    """
    _require_shell_grant_token(x_deepfiles_shell_token)
    try:
        grant = create_grant(body.path, source_kind=body.source_kind)
    except SourceGrantError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return SourceGrantResponse(
        source_token=grant.token,
        source_kind="local_folder",
        display_name=grant.display_name,
        root_path=str(grant.root_path),
        expires_at=grant.expires_at.isoformat(),
    )


@router.post(
    "/scan",
    response_model=SourceScanReport,
    dependencies=[Depends(require_functional)],
)
async def scan_source_endpoint(body: SourceScanRequest) -> SourceScanReport:
    try:
        grant = consume_grant(body.source_token)
    except SourceGrantError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    scan_id = new_scan_id()
    try:
        report = scan_folder(
            grant.root_path,
            scan_id=scan_id,
            include_hidden=body.include_hidden,
            max_files=body.max_files,
        )
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Selected source is unreadable") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    save_scan(report)
    return report


@router.get("/scans/{scan_id}", response_model=SourceScanReport)
async def get_source_scan_endpoint(scan_id: str) -> SourceScanReport:
    try:
        return get_scan(scan_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Scan not found")
