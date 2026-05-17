from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path, PurePosixPath
from typing import Optional

from services.ingest import IngestError, fast_ingest
from services.source_import.cloud_placeholders import (
    ONLINE_ONLY_PLACEHOLDER_REASON,
    classify_read_error_reason,
    detect_online_only_placeholder,
)
from services.source_import.manifest import (
    create_batch_manifest,
    get_batch_runtime,
    get_batch_summary,
    is_batch_cancellation_requested,
    mark_unprocessed_files_cancelled,
    update_batch_state,
    update_file_status,
)
from services.source_import.models import (
    SourceImportBatchSummary,
    SourceScanFileItem,
    SourceScanResult,
    SourceSelectionRecord,
)


logger = logging.getLogger(__name__)


def _short_batch_suffix(batch_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "", batch_id)[-6:].lower() or "batch"


def _destination_relroot(scan: SourceScanResult, batch_id: str) -> str:
    proposed = scan.report.proposed_destination_root.strip("/")
    if proposed.startswith("memory/"):
        proposed = proposed[len("memory/") :]
    proposed = proposed.strip("/") or "imports/source"
    return f"{proposed}-{_short_batch_suffix(batch_id)}"


def _approved_files(
    scan: SourceScanResult,
    selection: SourceSelectionRecord,
) -> list[SourceScanFileItem]:
    approved_ids = set(selection.approved_file_ids)
    return [
        item
        for item in scan.files
        if item.id in approved_ids and item.status == "supported"
    ]


def _target_folder(destination_root: str, relpath: str) -> str:
    parent = PurePosixPath(relpath).parent.as_posix()
    if parent in {"", "."}:
        return destination_root.strip("/")
    return f"{destination_root.strip('/')}/{parent.strip('/')}"


def _safe_source_path(root: Path, relpath: str) -> Path:
    rel = PurePosixPath(relpath)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise IngestError("Invalid source-relative path")
    try:
        candidate = (root / Path(*rel.parts)).resolve(strict=True)
        candidate.relative_to(root)
    except FileNotFoundError as exc:
        raise IngestError("Source file is no longer available") from exc
    except (OSError, ValueError) as exc:
        raise IngestError("Source file is no longer inside the selected folder") from exc
    if not candidate.is_file():
        raise IngestError("Source file is no longer available")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _result_note_paths(result: dict) -> list[str]:
    paths: list[str] = []
    if isinstance(result.get("path"), str):
        paths.append(str(result["path"]))
    notes = result.get("notes")
    if isinstance(notes, list):
        for note in notes:
            if isinstance(note, dict) and isinstance(note.get("path"), str):
                paths.append(str(note["path"]))
    return list(dict.fromkeys(paths))


def _created_note_count(result: dict) -> int:
    total_notes = result.get("total_notes")
    if isinstance(total_notes, int):
        return max(total_notes, 0)
    sections = result.get("sections")
    if isinstance(sections, int):
        return max(sections, 0) + 1
    return 1 if result.get("path") else len(_result_note_paths(result))


async def start_import_batch(
    *,
    batch_id: str,
    scan: SourceScanResult,
    selection: SourceSelectionRecord,
    workspace_path: Optional[Path] = None,
) -> SourceImportBatchSummary:
    files = _approved_files(scan, selection)
    if not files:
        raise ValueError("No supported files are approved for import")

    destination_root = _destination_relroot(scan, batch_id)
    summary = await create_batch_manifest(
        batch_id=batch_id,
        scan=scan,
        selection=selection,
        files=files,
        destination_root=destination_root,
        workspace_path=workspace_path,
    )
    asyncio.create_task(run_import_batch(batch_id, workspace_path=workspace_path))
    return summary


async def run_import_batch(
    batch_id: str,
    *,
    workspace_path: Optional[Path] = None,
) -> None:
    from services import ingest_jobs

    job_id: Optional[str] = None
    imported_hashes: dict[str, str] = {}
    try:
        batch, files = await get_batch_runtime(batch_id, workspace_path=workspace_path)
        root = Path(batch["source_root_path"]).resolve(strict=True)
        job_id = ingest_jobs.start_job(
            f"Folder import: {batch['source_display_name']}",
            kind="source_import",
            size_bytes=batch.get("total_bytes"),
        )
        await update_batch_state(
            batch_id,
            "importing",
            workspace_path=workspace_path,
        )

        total = len(files)
        for index, item in enumerate(files, start=1):
            if await is_batch_cancellation_requested(
                batch_id,
                workspace_path=workspace_path,
            ):
                await mark_unprocessed_files_cancelled(
                    batch_id,
                    workspace_path=workspace_path,
                )
                await update_batch_state(
                    batch_id,
                    "cancelled",
                    current_file=None,
                    finished=True,
                    workspace_path=workspace_path,
                )
                if job_id:
                    ingest_jobs.finish_job(job_id, error="Folder import cancelled")
                return

            ingest_jobs.update_stage(job_id, f"reading {index}/{total}")
            await update_batch_state(
                batch_id,
                "importing",
                current_file=item.relpath,
                workspace_path=workspace_path,
            )
            await update_file_status(
                batch_id,
                item.file_id,
                "importing",
                stage="reading",
                workspace_path=workspace_path,
            )
            try:
                source_path = _safe_source_path(root, item.relpath)
                placeholder_reason = detect_online_only_placeholder(source_path)
                if placeholder_reason:
                    await update_file_status(
                        batch_id,
                        item.file_id,
                        "skipped",
                        stage="skipped",
                        reason=placeholder_reason,
                        workspace_path=workspace_path,
                    )
                    continue

                await update_file_status(
                    batch_id,
                    item.file_id,
                    "importing",
                    stage="hashing",
                    workspace_path=workspace_path,
                )
                content_hash = await asyncio.to_thread(_sha256_file, source_path)
                duplicate_of = imported_hashes.get(content_hash)
                if duplicate_of:
                    await update_file_status(
                        batch_id,
                        item.file_id,
                        "skipped",
                        stage="done",
                        reason="duplicate_content",
                        duplicate_of=duplicate_of,
                        content_hash=content_hash,
                        workspace_path=workspace_path,
                    )
                    continue

                if detect_online_only_placeholder(source_path):
                    await update_file_status(
                        batch_id,
                        item.file_id,
                        "skipped",
                        stage="skipped",
                        reason=ONLINE_ONLY_PLACEHOLDER_REASON,
                        workspace_path=workspace_path,
                    )
                    continue

                metadata = {
                    "source_kind": "local_folder_import",
                    "source_filename": item.filename,
                    "source_relpath": item.relpath,
                    "import_batch_id": batch_id,
                    "source_size": item.size,
                    "source_content_sha256": content_hash,
                }
                if item.modified_at:
                    metadata["source_modified_at"] = item.modified_at

                result = await fast_ingest(
                    source_path,
                    target_folder=_target_folder(
                        batch["destination_root"],
                        item.relpath,
                    ),
                    workspace_path=workspace_path,
                    original_name=item.filename,
                    job_id=job_id,
                    source_label=item.relpath,
                    extra_frontmatter=metadata,
                )
                note_paths = _result_note_paths(result)
                created_notes = _created_note_count(result)
                imported_hashes[content_hash] = item.relpath
                await update_file_status(
                    batch_id,
                    item.file_id,
                    "done",
                    stage="done",
                    content_hash=content_hash,
                    note_paths=note_paths,
                    workspace_path=workspace_path,
                )
                await update_batch_state(
                    batch_id,
                    "importing",
                    created_note_delta=created_notes,
                    workspace_path=workspace_path,
                )
            except OSError as exc:
                reason = classify_read_error_reason(exc)
                is_placeholder = reason == ONLINE_ONLY_PLACEHOLDER_REASON
                await update_file_status(
                    batch_id,
                    item.file_id,
                    "skipped" if is_placeholder else "failed",
                    stage="skipped" if is_placeholder else "failed",
                    reason=reason,
                    workspace_path=workspace_path,
                )
            except IngestError as exc:
                await update_file_status(
                    batch_id,
                    item.file_id,
                    "failed",
                    stage="failed",
                    reason=str(exc),
                    workspace_path=workspace_path,
                )
            except Exception as exc:
                logger.exception("Source import failed for %s", item.relpath)
                await update_file_status(
                    batch_id,
                    item.file_id,
                    "failed",
                    stage="failed",
                    reason=str(exc),
                    workspace_path=workspace_path,
                )

        if await is_batch_cancellation_requested(
            batch_id,
            workspace_path=workspace_path,
        ):
            await mark_unprocessed_files_cancelled(
                batch_id,
                workspace_path=workspace_path,
            )
            await update_batch_state(
                batch_id,
                "cancelled",
                current_file=None,
                finished=True,
                workspace_path=workspace_path,
            )
            if job_id:
                ingest_jobs.finish_job(job_id, error="Folder import cancelled")
            return

        summary = await get_batch_summary(batch_id, workspace_path=workspace_path)
        final_state = (
            "failed"
            if summary.imported_file_count == 0 and summary.failed_file_count > 0
            else "completed"
        )
        await update_batch_state(
            batch_id,
            final_state,
            current_file=None,
            finished=True,
            workspace_path=workspace_path,
        )
        try:
            ingest_jobs.schedule_graph_rebuild(workspace_path=workspace_path)
        except Exception as exc:
            logger.warning("Source import graph rebuild scheduling failed: %s", exc)
        if job_id:
            ingest_jobs.finish_job(
                job_id,
                error=None if final_state == "completed" else "Folder import failed",
            )
    except Exception as exc:
        logger.exception("Source import batch %s failed", batch_id)
        await update_batch_state(
            batch_id,
            "failed",
            current_file=None,
            finished=True,
            workspace_path=workspace_path,
        )
        if job_id:
            ingest_jobs.finish_job(job_id, error=str(exc))
