from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from services.ingest import IngestError, fast_ingest
from services.source_import.archives import (
    ArchiveError,
    extract_archive_member_to_temp,
    extract_root_archive_member_to_temp,
    find_archive_member_reference,
)
from services.source_import.cloud_placeholders import (
    ONLINE_ONLY_PLACEHOLDER_REASON,
    classify_read_error_reason,
    detect_online_only_placeholder,
)
from services.source_import.dedupe import find_prior_imported_content_duplicate
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
    SourceDuplicatePolicy,
    SourceImportBatchSummary,
    SourceScanFileItem,
    SourceScanResult,
    SourceSelectionRecord,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PreparedSourceFile:
    path: Path
    origin_path: Path
    archive_relpath: Optional[str] = None
    archive_member_path: Optional[str] = None


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


def _prepare_source_file(
    root: Path,
    relpath: str,
    temp_dir: Path,
    source_kind: str,
) -> _PreparedSourceFile:
    if source_kind == "local_archive":
        try:
            extracted_path, reference = extract_root_archive_member_to_temp(
                archive_path=root,
                member_name=relpath,
                temp_dir=temp_dir,
            )
        except ArchiveError as exc:
            raise IngestError(exc.reason) from exc
        return _PreparedSourceFile(
            path=extracted_path,
            origin_path=reference.archive_path,
            archive_relpath=reference.archive_relpath,
            archive_member_path=reference.member_name,
        )

    archive_reference = find_archive_member_reference(root, relpath)
    if archive_reference is not None:
        try:
            extracted_path, reference = extract_archive_member_to_temp(
                root=root,
                relpath=relpath,
                temp_dir=temp_dir,
            )
        except ArchiveError as exc:
            raise IngestError(exc.reason) from exc
        return _PreparedSourceFile(
            path=extracted_path,
            origin_path=reference.archive_path,
            archive_relpath=reference.archive_relpath,
            archive_member_path=reference.member_name,
        )

    source_path = _safe_source_path(root, relpath)
    return _PreparedSourceFile(path=source_path, origin_path=source_path)


def _source_origin_path(root: Path, relpath: str, source_kind: str) -> Path:
    if source_kind == "local_archive":
        if not root.is_file():
            raise IngestError("Source archive is no longer available")
        return root
    archive_reference = find_archive_member_reference(root, relpath)
    if archive_reference is not None:
        return archive_reference.archive_path
    return _safe_source_path(root, relpath)


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
    duplicate_policy: SourceDuplicatePolicy = "skip",
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
        duplicate_policy=duplicate_policy,
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
        source_kind = str(batch.get("source_kind") or "local_folder")
        duplicate_policy = str(batch.get("duplicate_policy") or "skip")
        skip_duplicate_content = duplicate_policy != "import"
        job_label = "Archive import" if source_kind == "local_archive" else "Folder import"
        with tempfile.TemporaryDirectory(prefix=f"source-import-{batch_id}-") as temp_name:
            temp_dir = Path(temp_name)
            job_id = ingest_jobs.start_job(
                f"{job_label}: {batch['source_display_name']}",
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
                        ingest_jobs.finish_job(job_id, error=f"{job_label} cancelled")
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
                    origin_path = await asyncio.to_thread(
                        _source_origin_path,
                        root,
                        item.relpath,
                        source_kind,
                    )
                    placeholder_reason = detect_online_only_placeholder(origin_path)
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

                    prepared = await asyncio.to_thread(
                        _prepare_source_file,
                        root,
                        item.relpath,
                        temp_dir,
                        source_kind,
                    )
                    await update_file_status(
                        batch_id,
                        item.file_id,
                        "importing",
                        stage="hashing",
                        workspace_path=workspace_path,
                    )
                    content_hash = await asyncio.to_thread(_sha256_file, prepared.path)
                    if skip_duplicate_content:
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

                        prior_duplicate = await find_prior_imported_content_duplicate(
                            content_hash=content_hash,
                            current_batch_id=batch_id,
                            workspace_path=workspace_path,
                        )
                        if prior_duplicate:
                            await update_file_status(
                                batch_id,
                                item.file_id,
                                "skipped",
                                stage="done",
                                reason="duplicate_content_existing_import",
                                duplicate_of=prior_duplicate.display_label,
                                content_hash=content_hash,
                                workspace_path=workspace_path,
                            )
                            continue

                    if detect_online_only_placeholder(prepared.origin_path):
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
                        "source_kind": (
                            "local_archive_import"
                            if source_kind == "local_archive"
                            else "local_folder_import"
                        ),
                        "source_filename": item.filename,
                        "source_relpath": item.relpath,
                        "import_batch_id": batch_id,
                        "source_size": item.size,
                        "source_content_sha256": content_hash,
                    }
                    if item.modified_at:
                        metadata["source_modified_at"] = item.modified_at
                    if prepared.archive_relpath:
                        metadata["source_archive_relpath"] = prepared.archive_relpath
                    if prepared.archive_member_path:
                        metadata["source_archive_member_path"] = prepared.archive_member_path

                    result = await fast_ingest(
                        prepared.path,
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
                ingest_jobs.finish_job(job_id, error=f"{job_label} cancelled")
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
                error=None if final_state == "completed" else f"{job_label} failed",
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
