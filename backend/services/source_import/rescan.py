from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from services.source_import.manifest import get_batch_runtime
from services.source_import.models import (
    SourceImportFileOutcome,
    SourceImportRescanFileItem,
    SourceImportRescanReport,
    SourceScanFileItem,
    SourceScanFolderSummary,
    SourceScanLargestFile,
    SourceScanReport,
    SourceScanResult,
)
from services.source_import.scan import FILE_LIST_LIMIT, scan_source


class SourceImportRescanConflict(Exception):
    pass


_RESCANNABLE_STATES = {"completed", "failed", "cancelled", "interrupted"}
_ACTIVE_STATES = {"queued", "importing", "cancelling", "removing"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_from_previous(item: SourceImportFileOutcome) -> SourceImportRescanFileItem:
    return SourceImportRescanFileItem(
        id=item.file_id,
        relpath=item.relpath,
        filename=item.filename,
        extension=item.extension,
        size=0,
        modified_at=None,
        status="missing",
        reason="missing_from_source",
        previous_status=item.status,
        previous_size=item.size,
        previous_modified_at=item.modified_at,
    )


def _previous_needs_import(item: SourceImportFileOutcome) -> bool:
    if item.status == "done":
        return False
    if item.status == "skipped" and "duplicate_content" in (item.reason or ""):
        return False
    return True


def _metadata_changed(
    current: SourceScanFileItem,
    previous: SourceImportFileOutcome,
) -> bool:
    return current.size != previous.size or current.modified_at != previous.modified_at


def _comparison_item(
    current: SourceScanFileItem,
    previous: Optional[SourceImportFileOutcome],
) -> tuple[SourceImportRescanFileItem, bool]:
    if current.status == "unsupported":
        return (
            SourceImportRescanFileItem(
                id=current.id,
                relpath=current.relpath,
                filename=current.filename,
                extension=current.extension,
                size=current.size,
                modified_at=current.modified_at,
                status="unsupported",
                reason=current.reason,
                previous_status=previous.status if previous else None,
                previous_size=previous.size if previous else None,
                previous_modified_at=previous.modified_at if previous else None,
            ),
            False,
        )
    if current.status == "skipped":
        return (
            SourceImportRescanFileItem(
                id=current.id,
                relpath=current.relpath,
                filename=current.filename,
                extension=current.extension,
                size=current.size,
                modified_at=current.modified_at,
                status="skipped",
                reason=current.reason,
                previous_status=previous.status if previous else None,
                previous_size=previous.size if previous else None,
                previous_modified_at=previous.modified_at if previous else None,
            ),
            False,
        )

    if previous is None:
        return (
            SourceImportRescanFileItem(
                id=current.id,
                relpath=current.relpath,
                filename=current.filename,
                extension=current.extension,
                size=current.size,
                modified_at=current.modified_at,
                status="new",
            ),
            True,
        )

    if _previous_needs_import(previous):
        return (
            SourceImportRescanFileItem(
                id=current.id,
                relpath=current.relpath,
                filename=current.filename,
                extension=current.extension,
                size=current.size,
                modified_at=current.modified_at,
                status="changed",
                reason="previous_import_not_completed",
                previous_status=previous.status,
                previous_size=previous.size,
                previous_modified_at=previous.modified_at,
            ),
            True,
        )

    if _metadata_changed(current, previous):
        return (
            SourceImportRescanFileItem(
                id=current.id,
                relpath=current.relpath,
                filename=current.filename,
                extension=current.extension,
                size=current.size,
                modified_at=current.modified_at,
                status="changed",
                reason="metadata_changed",
                previous_status=previous.status,
                previous_size=previous.size,
                previous_modified_at=previous.modified_at,
            ),
            True,
        )

    reason = "previous_duplicate_content" if "duplicate_content" in (previous.reason or "") else None
    return (
        SourceImportRescanFileItem(
            id=current.id,
            relpath=current.relpath,
            filename=current.filename,
            extension=current.extension,
            size=current.size,
            modified_at=current.modified_at,
            status="unchanged",
            reason=reason,
            previous_status=previous.status,
            previous_size=previous.size,
            previous_modified_at=previous.modified_at,
        ),
        False,
    )


def _sort_comparison(items: list[SourceImportRescanFileItem]) -> list[SourceImportRescanFileItem]:
    priority = {
        "new": 0,
        "changed": 1,
        "missing": 2,
        "unsupported": 3,
        "skipped": 4,
        "unchanged": 5,
    }
    return sorted(items, key=lambda item: (priority[item.status], item.relpath.lower()))


def _folder_key(relpath: str) -> str:
    parent = PurePosixPath(relpath).parent.as_posix()
    return "." if parent in {"", "."} else parent


def _scan_for_importable_changes(
    *,
    source_scan: SourceScanResult,
    importable_files: list[SourceScanFileItem],
    scan_id: str,
) -> SourceScanResult:
    counts_by_extension: Counter[str] = Counter(item.extension for item in importable_files)
    folders: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    largest: list[SourceScanLargestFile] = []
    total_size = 0
    for item in importable_files:
        total_size += max(item.size, 0)
        folder = _folder_key(item.relpath)
        folders[folder][0] += 1
        folders[folder][1] += max(item.size, 0)
        largest.append(
            SourceScanLargestFile(
                relpath=item.relpath,
                size=item.size,
                extension=item.extension,
            )
        )

    largest.sort(key=lambda row: row.size, reverse=True)
    folder_summary = [
        SourceScanFolderSummary(relpath=relpath, file_count=values[0], total_size=values[1])
        for relpath, values in sorted(
            folders.items(),
            key=lambda pair: (pair[0].count("/"), pair[0].lower()),
        )[:20]
    ]
    preview = importable_files[:FILE_LIST_LIMIT]

    report = SourceScanReport(
        scan_id=scan_id,
        source_kind=source_scan.report.source_kind,
        source_display_name=source_scan.report.source_display_name,
        source_root_path=source_scan.report.source_root_path,
        proposed_destination_root=source_scan.report.proposed_destination_root,
        total_files_seen=len(importable_files),
        total_size_seen=total_size,
        supported_file_count=len(importable_files),
        unsupported_file_count=0,
        skipped_file_count=0,
        skipped_by_reason={},
        counts_by_extension=dict(sorted(counts_by_extension.items())),
        largest_files=largest[:10],
        folder_summary=folder_summary,
        files=preview,
        file_list_truncated=len(importable_files) > len(preview),
        limit_hit=False,
        created_at=_now_iso(),
    )
    return SourceScanResult(report=report, files=importable_files)


async def rescan_import_batch(
    *,
    batch_id: str,
    scan_id: str,
    workspace_path: Optional[Path] = None,
) -> tuple[SourceImportRescanReport, SourceScanResult | None]:
    batch, previous_files = await get_batch_runtime(batch_id, workspace_path=workspace_path)
    state = str(batch["state"])
    if state in _ACTIVE_STATES:
        raise SourceImportRescanConflict("Import is still running")
    if state not in _RESCANNABLE_STATES:
        raise SourceImportRescanConflict("Import is not rescannable in its current state")

    root = Path(str(batch["source_root_path"]))
    source_scan = scan_source(
        root,
        source_kind=str(batch.get("source_kind") or "local_folder"),
        scan_id=scan_id,
    )
    previous_by_relpath = {item.relpath: item for item in previous_files}
    current_relpaths = {item.relpath for item in source_scan.files}

    comparison: list[SourceImportRescanFileItem] = []
    importable_files: list[SourceScanFileItem] = []
    counts: Counter[str] = Counter()

    for current in source_scan.files:
        previous = previous_by_relpath.get(current.relpath)
        row, importable = _comparison_item(current, previous)
        comparison.append(row)
        counts[row.status] += 1
        if importable:
            importable_files.append(current)

    for previous in previous_files:
        if previous.relpath in current_relpaths:
            continue
        row = _file_from_previous(previous)
        comparison.append(row)
        counts[row.status] += 1

    comparison = _sort_comparison(comparison)
    preview = comparison[:FILE_LIST_LIMIT]
    import_scan = (
        _scan_for_importable_changes(
            source_scan=source_scan,
            importable_files=importable_files,
            scan_id=scan_id,
        )
        if importable_files
        else None
    )

    report = SourceImportRescanReport(
        batch_id=batch_id,
        scan_id=scan_id if import_scan else None,
        source_kind=source_scan.report.source_kind,
        source_display_name=str(batch["source_display_name"]),
        proposed_destination_root=source_scan.report.proposed_destination_root,
        total_files_seen=source_scan.report.total_files_seen,
        current_supported_file_count=source_scan.report.supported_file_count,
        unsupported_file_count=source_scan.report.unsupported_file_count,
        skipped_file_count=source_scan.report.skipped_file_count,
        unchanged_file_count=counts["unchanged"],
        changed_file_count=counts["changed"],
        new_file_count=counts["new"],
        missing_file_count=counts["missing"],
        importable_file_count=len(importable_files),
        importable_total_size=sum(max(item.size, 0) for item in importable_files),
        skipped_by_reason=source_scan.report.skipped_by_reason,
        files=preview,
        file_list_truncated=len(comparison) > len(preview),
        created_at=_now_iso(),
    )
    return report, import_scan
