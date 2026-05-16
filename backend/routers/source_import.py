import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException

from services.entitlement_gate import require_functional
from services.source_import.grants import (
    SourceGrantError,
    consume_grant,
    create_grant,
)
from services.source_import.cancellation import (
    SourceImportCancelConflict,
    cancel_import_batch,
)
from services.source_import.models import (
    SourceImportBatchSummary,
    SourceImportRemoveRequest,
    SourceImportStartRequest,
    SourceGrantRequest,
    SourceGrantResponse,
    SourceSelectionRequest,
    SourceSelectionSummary,
    SourceScanReport,
    SourceScanRequest,
)
from services.source_import.removal import (
    SourceImportRemovalConflict,
    remove_import_batch,
)
from services.source_import.manifest import get_batch_summary, list_batch_summaries
from services.source_import.scan import scan_folder
from services.source_import.selection import build_selection
from services.source_import.store import (
    get_scan,
    get_scan_record,
    get_selection,
    new_import_batch_id,
    new_scan_id,
    new_selection_id,
    save_scan,
    save_selection,
)
from services.source_import.worker import start_import_batch


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
        scan = scan_folder(
            grant.root_path,
            scan_id=scan_id,
            include_hidden=body.include_hidden,
            max_files=body.max_files,
        )
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Selected source is unreadable") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    save_scan(scan)
    return scan.report


@router.get(
    "/scans/{scan_id}",
    response_model=SourceScanReport,
    dependencies=[Depends(require_functional)],
)
async def get_source_scan_endpoint(scan_id: str) -> SourceScanReport:
    try:
        return get_scan(scan_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Scan not found")


@router.post(
    "/scans/{scan_id}/selection",
    response_model=SourceSelectionSummary,
    dependencies=[Depends(require_functional)],
)
async def create_source_selection_endpoint(
    scan_id: str,
    body: SourceSelectionRequest,
) -> SourceSelectionSummary:
    try:
        scan = get_scan_record(scan_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Scan not found")

    selection = build_selection(
        scan,
        body,
        selection_id=new_selection_id(),
    )
    save_selection(selection)
    return selection.summary


@router.post(
    "/scans/{scan_id}/start",
    response_model=SourceImportBatchSummary,
    dependencies=[Depends(require_functional)],
)
async def start_source_import_endpoint(
    scan_id: str,
    body: SourceImportStartRequest,
) -> SourceImportBatchSummary:
    try:
        scan = get_scan_record(scan_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Scan not found")
    try:
        selection = get_selection(body.selection_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Selection not found")
    if selection.summary.scan_id != scan_id:
        raise HTTPException(status_code=400, detail="Selection does not belong to scan")

    try:
        return await start_import_batch(
            batch_id=new_import_batch_id(),
            scan=scan,
            selection=selection,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get(
    "/imports",
    response_model=list[SourceImportBatchSummary],
    dependencies=[Depends(require_functional)],
)
async def list_source_imports_endpoint() -> list[SourceImportBatchSummary]:
    return await list_batch_summaries()


@router.get(
    "/imports/{batch_id}",
    response_model=SourceImportBatchSummary,
    dependencies=[Depends(require_functional)],
)
async def get_source_import_endpoint(batch_id: str) -> SourceImportBatchSummary:
    try:
        return await get_batch_summary(batch_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Import batch not found")


@router.post(
    "/imports/{batch_id}/cancel",
    response_model=SourceImportBatchSummary,
    dependencies=[Depends(require_functional)],
)
async def cancel_source_import_endpoint(batch_id: str) -> SourceImportBatchSummary:
    try:
        return await cancel_import_batch(batch_id=batch_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Import batch not found")
    except SourceImportCancelConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post(
    "/imports/{batch_id}/remove",
    response_model=SourceImportBatchSummary,
    dependencies=[Depends(require_functional)],
)
async def remove_source_import_endpoint(
    batch_id: str,
    body: SourceImportRemoveRequest,
) -> SourceImportBatchSummary:
    try:
        return await remove_import_batch(
            batch_id=batch_id,
            confirm_batch_id=body.confirm_batch_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Import batch not found")
    except SourceImportRemovalConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
