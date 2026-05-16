from __future__ import annotations

from pathlib import Path
from typing import Optional

from services.source_import.manifest import (
    get_batch_runtime,
    get_batch_summary,
    request_batch_cancel,
)
from services.source_import.models import SourceImportBatchSummary


_CANCELLABLE_STATES = {"queued", "importing", "cancelling"}


class SourceImportCancelConflict(Exception):
    """Raised when a batch cannot be cancelled in its current lifecycle state."""


async def cancel_import_batch(
    *,
    batch_id: str,
    workspace_path: Optional[Path] = None,
) -> SourceImportBatchSummary:
    batch, _files = await get_batch_runtime(batch_id, workspace_path=workspace_path)
    state = str(batch["state"])

    if state == "cancelled":
        return await get_batch_summary(batch_id, workspace_path=workspace_path)
    if state not in _CANCELLABLE_STATES:
        raise SourceImportCancelConflict(
            "Import is not cancellable in its current state"
        )

    await request_batch_cancel(batch_id, workspace_path=workspace_path)
    return await get_batch_summary(batch_id, workspace_path=workspace_path)
