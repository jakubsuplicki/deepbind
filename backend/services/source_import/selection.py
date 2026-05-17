from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import PurePosixPath

from services.source_import.limits import MAX_APPROVED_BYTES_PER_BATCH
from services.source_import.models import (
    SourceScanFileItem,
    SourceScanResult,
    SourceSelectionRecord,
    SourceSelectionRequest,
    SourceSelectionSummary,
)
from services.source_import.scan import FILE_LIST_LIMIT


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_extension(value: str) -> str | None:
    extension = value.strip().lower()
    if not extension:
        return None
    if extension == "(none)":
        return extension
    if not extension.startswith("."):
        extension = f".{extension}"
    return extension


def _normalize_folder(value: str) -> str | None:
    folder = value.strip().replace("\\", "/").strip("/")
    if not folder:
        return None
    if folder == ".":
        return "."
    parts = [part for part in folder.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts) or "."


def _folder_for_file(relpath: str) -> str:
    parent = PurePosixPath(relpath).parent.as_posix()
    return "." if parent in {"", "."} else parent


def _is_in_folder(relpath: str, folder: str) -> bool:
    if folder == ".":
        return True
    parent = _folder_for_file(relpath)
    return parent == folder or parent.startswith(f"{folder}/")


def build_selection(
    scan: SourceScanResult,
    request: SourceSelectionRequest,
    *,
    selection_id: str,
) -> SourceSelectionRecord:
    excluded_file_ids = {value for value in request.excluded_file_ids if value}
    excluded_extensions = {
        extension
        for value in request.excluded_extensions
        if (extension := _normalize_extension(value)) is not None
    }
    excluded_folders = {
        folder
        for value in request.excluded_folders
        if (folder := _normalize_folder(value)) is not None
    }

    approved: list[SourceScanFileItem] = []
    approved_file_ids: list[str] = []
    approved_total_size = 0
    excluded_total_size = 0
    excluded_by_rule: Counter[str] = Counter()

    for item in scan.files:
        if item.status != "supported":
            continue

        rule: str | None = None
        if item.id in excluded_file_ids:
            rule = "file"
        elif item.extension.lower() in excluded_extensions:
            rule = "file_type"
        elif any(_is_in_folder(item.relpath, folder) for folder in excluded_folders):
            rule = "folder"
        elif approved_total_size + max(item.size, 0) > MAX_APPROVED_BYTES_PER_BATCH:
            rule = "batch_size_limit"

        if rule:
            excluded_by_rule[rule] += 1
            excluded_total_size += max(item.size, 0)
            continue

        approved.append(item)
        approved_file_ids.append(item.id)
        approved_total_size += max(item.size, 0)

    approved_preview = approved[:FILE_LIST_LIMIT]

    summary = SourceSelectionSummary(
        selection_id=selection_id,
        scan_id=scan.report.scan_id,
        source_display_name=scan.report.source_display_name,
        proposed_destination_root=scan.report.proposed_destination_root,
        approved_file_count=len(approved),
        approved_total_size=approved_total_size,
        excluded_file_count=sum(excluded_by_rule.values()),
        excluded_total_size=excluded_total_size,
        unsupported_file_count=scan.report.unsupported_file_count,
        skipped_file_count=scan.report.skipped_file_count,
        excluded_by_rule=dict(sorted(excluded_by_rule.items())),
        approved_files=approved_preview,
        approved_file_list_truncated=len(approved) > len(approved_preview),
        created_at=_now_iso(),
    )
    return SourceSelectionRecord(summary=summary, approved_file_ids=approved_file_ids)
