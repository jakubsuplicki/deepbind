from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import aiosqlite

from config import get_settings
from services.source_import.models import (
    SourceImportBatchSummary,
    SourceImportCompletionSummary,
    SourceImportFileReviewItem,
    SourceImportFileReviewReport,
    SourceImportFileOutcome,
    SourceImportFileStatus,
    SourceImportSuggestedQuestion,
    SourceImportState,
    SourceScanFileItem,
    SourceScanResult,
    SourceSelectionRecord,
)


_INITIALIZED_DB_PATHS: set[str] = set()


SOURCE_IMPORT_SQL = """
CREATE TABLE IF NOT EXISTS source_import_batches (
    batch_id            TEXT PRIMARY KEY,
    scan_id             TEXT NOT NULL,
    selection_id        TEXT NOT NULL,
    duplicate_policy    TEXT NOT NULL DEFAULT 'skip',
    source_kind         TEXT NOT NULL DEFAULT 'local_folder',
    source_display_name TEXT NOT NULL,
    source_root_path    TEXT NOT NULL,
    destination_root    TEXT NOT NULL,
    state               TEXT NOT NULL,
    total_file_count    INTEGER NOT NULL DEFAULT 0,
    total_bytes         INTEGER NOT NULL DEFAULT 0,
    created_note_count  INTEGER NOT NULL DEFAULT 0,
    current_file        TEXT,
    started_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    finished_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_import_batches_started
    ON source_import_batches(started_at);

CREATE TABLE IF NOT EXISTS source_import_files (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       TEXT NOT NULL,
    file_id        TEXT NOT NULL,
    relpath        TEXT NOT NULL,
    filename       TEXT NOT NULL,
    extension      TEXT NOT NULL,
    size           INTEGER NOT NULL DEFAULT 0,
    modified_at    TEXT,
    status         TEXT NOT NULL,
    stage          TEXT,
    reason         TEXT,
    duplicate_of   TEXT,
    content_hash   TEXT,
    warnings       TEXT NOT NULL DEFAULT '[]',
    note_paths     TEXT NOT NULL DEFAULT '[]',
    updated_at     TEXT NOT NULL,
    UNIQUE(batch_id, file_id),
    FOREIGN KEY (batch_id) REFERENCES source_import_batches(batch_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_source_import_files_batch
    ON source_import_files(batch_id);
CREATE INDEX IF NOT EXISTS idx_source_import_files_status
    ON source_import_files(status);
CREATE INDEX IF NOT EXISTS idx_source_import_files_content_hash
    ON source_import_files(content_hash);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_path(workspace_path: Optional[Path] = None) -> Path:
    return workspace_path or get_settings().workspace_path


def _db_path(workspace_path: Optional[Path] = None) -> Path:
    return _workspace_path(workspace_path) / "app" / "jarvis.db"


async def _ensure_db(workspace_path: Optional[Path] = None) -> Path:
    from models.database import init_database

    db_path = _db_path(workspace_path)
    key = str(db_path)
    if key not in _INITIALIZED_DB_PATHS:
        await init_database(db_path)
        _INITIALIZED_DB_PATHS.add(key)
    return db_path


def _public_destination(destination_root: str) -> str:
    cleaned = destination_root.strip("/")
    return f"memory/{cleaned}/" if cleaned else "memory/"


def _decode_note_paths(value: object) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _decode_warnings(value: object) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _outcome_from_row(row: aiosqlite.Row) -> SourceImportFileOutcome:
    return SourceImportFileOutcome(
        file_id=row["file_id"],
        relpath=row["relpath"],
        filename=row["filename"],
        extension=row["extension"],
        size=int(row["size"] or 0),
        modified_at=row["modified_at"],
        status=row["status"],
        stage=row["stage"],
        reason=row["reason"],
        duplicate_of=row["duplicate_of"],
        content_hash=row["content_hash"],
        warnings=_decode_warnings(row["warnings"] if "warnings" in row.keys() else None),
        note_paths=_decode_note_paths(row["note_paths"]),
    )


_ACTIVE_STATES = ("queued", "importing", "cancelling", "removing")
_REVIEW_STATUSES = ("skipped", "failed")
_COMPLETION_QUESTION_LIMIT = 5


def _normalise_reason(reason: Optional[str], fallback: str) -> str:
    if not reason:
        return fallback
    cleaned = reason.strip()
    return cleaned or fallback


def _can_retry_file(*, status: str, reason: Optional[str]) -> bool:
    reason_text = (reason or "").lower()
    if "duplicate_content" in reason_text:
        return False
    if status == "failed":
        return True
    return any(
        marker in reason_text
        for marker in (
            "app_closed_during_import",
            "cancelled_by_user",
            "no longer available",
            "outside the selected folder",
            "permission",
            "unreadable",
        )
    )


def _can_fix_locally(*, reason: Optional[str]) -> bool:
    reason_text = (reason or "").lower()
    return any(
        marker in reason_text
        for marker in (
            "archive_",
            "encrypted",
            "file_too_large",
            "limit",
            "no longer available",
            "online_only",
            "outside the selected folder",
            "password",
            "permission",
            "placeholder",
            "source file",
            "unreadable",
            "unsupported",
        )
    )


def _review_item_from_outcome(
    item: SourceImportFileOutcome,
) -> SourceImportFileReviewItem:
    if item.status not in {"skipped", "failed"}:
        raise ValueError("Review items must be skipped or failed")
    review_status = "skipped" if item.status == "skipped" else "failed"
    return SourceImportFileReviewItem(
        file_id=item.file_id,
        relpath=item.relpath,
        filename=item.filename,
        extension=item.extension,
        size=item.size,
        modified_at=item.modified_at,
        status=review_status,
        stage=item.stage,
        reason=item.reason,
        duplicate_of=item.duplicate_of,
        note_paths=item.note_paths,
        can_retry=_can_retry_file(status=item.status, reason=item.reason),
        can_fix_locally=_can_fix_locally(reason=item.reason),
    )


def _parent_folder(relpath: str) -> str:
    parent = Path(relpath).parent.as_posix()
    return "." if parent in {"", "."} else parent


def _sorted_counts(counts: dict[str, int], *, limit: int = 6) -> dict[str, int]:
    return dict(
        sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    )


def _add_question(
    questions: list[SourceImportSuggestedQuestion],
    seen: set[str],
    question: str,
    *,
    reason: str = "general",
) -> None:
    if question in seen or len(questions) >= _COMPLETION_QUESTION_LIMIT:
        return
    seen.add(question)
    questions.append(
        SourceImportSuggestedQuestion(
            question=question,
            reason=reason,  # type: ignore[arg-type]
        )
    )


def _build_suggested_questions(
    *,
    imported_extension_counts: dict[str, int],
    imported_folder_counts: dict[str, int],
    skipped_file_count: int,
    failed_file_count: int,
) -> list[SourceImportSuggestedQuestion]:
    questions: list[SourceImportSuggestedQuestion] = []
    seen: set[str] = set()
    extensions = set(imported_extension_counts)

    _add_question(
        questions,
        seen,
        "What are the main themes across this import?",
    )
    _add_question(
        questions,
        seen,
        "Which files should I review first?",
    )
    _add_question(
        questions,
        seen,
        "What risks, open questions, or decisions appear across these files?",
    )

    if skipped_file_count or failed_file_count:
        _add_question(
            questions,
            seen,
            "What important files were skipped or failed, and what should I fix?",
            reason="issues",
        )
    if extensions & {".csv", ".xlsx", ".xml", ".json"}:
        _add_question(
            questions,
            seen,
            "Which files mention pricing, budget, dates, or status?",
            reason="file_types",
        )
    if ".eml" in extensions:
        _add_question(
            questions,
            seen,
            "What decisions or follow-ups appear in the emails?",
            reason="file_types",
        )
    if ".pptx" in extensions:
        _add_question(
            questions,
            seen,
            "What are the main points across the decks?",
            reason="file_types",
        )
    if extensions & {".docx", ".pdf", ".txt", ".rtf", ".md"}:
        _add_question(
            questions,
            seen,
            "Summarize the requirements and commitments in these documents.",
            reason="file_types",
        )

    top_folder = next(
        (folder for folder in imported_folder_counts if folder != "."),
        "",
    )
    if top_folder:
        _add_question(
            questions,
            seen,
            f"Summarize the {top_folder} folder.",
            reason="folders",
        )

    return questions


async def create_batch_manifest(
    *,
    batch_id: str,
    scan: SourceScanResult,
    selection: SourceSelectionRecord,
    files: Iterable[SourceScanFileItem],
    destination_root: str,
    duplicate_policy: str = "skip",
    workspace_path: Optional[Path] = None,
) -> SourceImportBatchSummary:
    db_path = await _ensure_db(workspace_path)
    now = _now_iso()
    file_list = list(files)
    total_bytes = sum(max(item.size, 0) for item in file_list)
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            INSERT INTO source_import_batches(
                batch_id, scan_id, selection_id, duplicate_policy, source_kind,
                source_display_name, source_root_path, destination_root, state, total_file_count,
                total_bytes, created_note_count, started_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                scan.report.scan_id,
                selection.summary.selection_id,
                duplicate_policy,
                scan.report.source_kind,
                scan.report.source_display_name,
                scan.report.source_root_path,
                destination_root.strip("/"),
                "queued",
                len(file_list),
                total_bytes,
                0,
                now,
                now,
            ),
        )
        for item in file_list:
            await db.execute(
                """
                INSERT INTO source_import_files(
                    batch_id, file_id, relpath, filename, extension, size,
                    modified_at, status, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    item.id,
                    item.relpath,
                    item.filename,
                    item.extension,
                    item.size,
                    item.modified_at,
                    "queued",
                    now,
                ),
            )
        await db.commit()
    return await get_batch_summary(batch_id, workspace_path=workspace_path)


async def list_batch_summaries(
    *,
    limit: int = 20,
    workspace_path: Optional[Path] = None,
) -> list[SourceImportBatchSummary]:
    db_path = await _ensure_db(workspace_path)
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT batch_id FROM source_import_batches
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
    summaries = []
    for row in rows:
        summaries.append(
            await get_batch_summary(row["batch_id"], workspace_path=workspace_path)
        )
    return summaries


async def get_batch_summary(
    batch_id: str,
    *,
    workspace_path: Optional[Path] = None,
) -> SourceImportBatchSummary:
    db_path = await _ensure_db(workspace_path)
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        batch_cursor = await db.execute(
            "SELECT * FROM source_import_batches WHERE batch_id = ?",
            (batch_id,),
        )
        batch = await batch_cursor.fetchone()
        if batch is None:
            raise KeyError("Import batch not found")

        file_cursor = await db.execute(
            """
            SELECT * FROM source_import_files
            WHERE batch_id = ?
            ORDER BY id
            """,
            (batch_id,),
        )
        file_rows = await file_cursor.fetchall()

    files = [_outcome_from_row(row) for row in file_rows]
    imported_file_count = sum(1 for item in files if item.status == "done")
    skipped_file_count = sum(1 for item in files if item.status == "skipped")
    failed_file_count = sum(1 for item in files if item.status == "failed")
    warning_file_count = sum(
        1 for item in files if item.status == "done" and item.warnings
    )
    processed_bytes = sum(
        max(item.size, 0)
        for item in files
        if item.status in {"done", "skipped", "failed"}
    )
    return SourceImportBatchSummary(
        batch_id=batch["batch_id"],
        scan_id=batch["scan_id"],
        selection_id=batch["selection_id"],
        duplicate_policy=batch["duplicate_policy"] or "skip",
        source_kind=batch["source_kind"],
        source_display_name=batch["source_display_name"],
        destination_root=_public_destination(batch["destination_root"]),
        state=batch["state"],
        total_file_count=int(batch["total_file_count"] or 0),
        imported_file_count=imported_file_count,
        skipped_file_count=skipped_file_count,
        failed_file_count=failed_file_count,
        warning_file_count=warning_file_count,
        created_note_count=int(batch["created_note_count"] or 0),
        total_bytes=int(batch["total_bytes"] or 0),
        processed_bytes=processed_bytes,
        current_file=batch["current_file"],
        files=files,
        started_at=batch["started_at"],
        updated_at=batch["updated_at"],
        finished_at=batch["finished_at"],
    )


async def get_batch_file_review(
    batch_id: str,
    *,
    limit: int = 100,
    workspace_path: Optional[Path] = None,
) -> SourceImportFileReviewReport:
    db_path = await _ensure_db(workspace_path)
    capped_limit = min(max(limit, 1), 500)
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        batch_cursor = await db.execute(
            "SELECT * FROM source_import_batches WHERE batch_id = ?",
            (batch_id,),
        )
        batch = await batch_cursor.fetchone()
        if batch is None:
            raise KeyError("Import batch not found")

        status_placeholders = ",".join("?" for _ in _REVIEW_STATUSES)
        counts_cursor = await db.execute(
            f"""
            SELECT status, reason, COUNT(1) AS count
            FROM source_import_files
            WHERE batch_id = ?
              AND status IN ({status_placeholders})
            GROUP BY status, reason
            """,
            (batch_id, *_REVIEW_STATUSES),
        )
        count_rows = await counts_cursor.fetchall()

        files_cursor = await db.execute(
            f"""
            SELECT * FROM source_import_files
            WHERE batch_id = ?
              AND status IN ({status_placeholders})
            ORDER BY CASE status WHEN 'failed' THEN 0 ELSE 1 END, id
            LIMIT ?
            """,
            (batch_id, *_REVIEW_STATUSES, capped_limit + 1),
        )
        file_rows = await files_cursor.fetchall()

    skipped_file_count = 0
    failed_file_count = 0
    reason_counts: dict[str, int] = {}
    for row in count_rows:
        count = int(row["count"] or 0)
        status = str(row["status"])
        reason = _normalise_reason(row["reason"], status)
        reason_counts[reason] = reason_counts.get(reason, 0) + count
        if status == "skipped":
            skipped_file_count += count
        elif status == "failed":
            failed_file_count += count

    truncated = len(file_rows) > capped_limit
    outcomes = [_outcome_from_row(row) for row in file_rows[:capped_limit]]
    return SourceImportFileReviewReport(
        batch_id=batch["batch_id"],
        source_display_name=batch["source_display_name"],
        state=batch["state"],
        skipped_file_count=skipped_file_count,
        failed_file_count=failed_file_count,
        problem_file_count=skipped_file_count + failed_file_count,
        reason_counts=dict(sorted(reason_counts.items())),
        files=[_review_item_from_outcome(item) for item in outcomes],
        file_list_truncated=truncated,
        updated_at=batch["updated_at"],
    )


async def get_batch_completion_summary(
    batch_id: str,
    *,
    workspace_path: Optional[Path] = None,
) -> SourceImportCompletionSummary:
    summary = await get_batch_summary(batch_id, workspace_path=workspace_path)
    imported_extension_counts: dict[str, int] = {}
    imported_folder_counts: dict[str, int] = {}
    duplicate_file_count = 0

    for item in summary.files:
        if item.status == "done":
            extension = item.extension or "(none)"
            imported_extension_counts[extension] = (
                imported_extension_counts.get(extension, 0) + 1
            )
            folder = _parent_folder(item.relpath)
            imported_folder_counts[folder] = imported_folder_counts.get(folder, 0) + 1
        elif item.status == "skipped" and "duplicate_content" in (item.reason or ""):
            duplicate_file_count += 1

    sorted_extensions = _sorted_counts(imported_extension_counts)
    sorted_folders = _sorted_counts(imported_folder_counts)
    return SourceImportCompletionSummary(
        batch_id=summary.batch_id,
        source_display_name=summary.source_display_name,
        state=summary.state,
        destination_root=summary.destination_root,
        total_file_count=summary.total_file_count,
        imported_file_count=summary.imported_file_count,
        skipped_file_count=summary.skipped_file_count,
        failed_file_count=summary.failed_file_count,
        duplicate_file_count=duplicate_file_count,
        warning_file_count=summary.warning_file_count,
        created_note_count=summary.created_note_count,
        imported_extension_counts=sorted_extensions,
        imported_folder_counts=sorted_folders,
        suggested_questions=_build_suggested_questions(
            imported_extension_counts=sorted_extensions,
            imported_folder_counts=sorted_folders,
            skipped_file_count=summary.skipped_file_count,
            failed_file_count=summary.failed_file_count,
        ),
        can_ask_about_import=summary.imported_file_count > 0,
        updated_at=summary.updated_at,
    )


async def get_batch_runtime(
    batch_id: str,
    *,
    workspace_path: Optional[Path] = None,
) -> tuple[dict, list[SourceImportFileOutcome]]:
    db_path = await _ensure_db(workspace_path)
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        batch_cursor = await db.execute(
            "SELECT * FROM source_import_batches WHERE batch_id = ?",
            (batch_id,),
        )
        batch = await batch_cursor.fetchone()
        if batch is None:
            raise KeyError("Import batch not found")
        file_cursor = await db.execute(
            "SELECT * FROM source_import_files WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        )
        files = await file_cursor.fetchall()
    return dict(batch), [_outcome_from_row(row) for row in files]


async def update_batch_state(
    batch_id: str,
    state: SourceImportState,
    *,
    current_file: Optional[str] = None,
    created_note_delta: int = 0,
    finished: bool = False,
    workspace_path: Optional[Path] = None,
) -> None:
    db_path = await _ensure_db(workspace_path)
    now = _now_iso()
    finished_at = now if finished else None
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            UPDATE source_import_batches
            SET state = CASE
                    WHEN state = 'cancelling' AND ? = 'importing' THEN state
                    ELSE ?
                END,
                current_file = ?,
                created_note_count = created_note_count + ?,
                updated_at = ?,
                finished_at = CASE WHEN ? THEN ? ELSE finished_at END
            WHERE batch_id = ?
            """,
            (
                state,
                state,
                current_file,
                created_note_delta,
                now,
                1 if finished else 0,
                finished_at,
                batch_id,
            ),
        )
        await db.commit()


async def is_batch_cancellation_requested(
    batch_id: str,
    *,
    workspace_path: Optional[Path] = None,
) -> bool:
    db_path = await _ensure_db(workspace_path)
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            "SELECT state FROM source_import_batches WHERE batch_id = ?",
            (batch_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise KeyError("Import batch not found")
    return str(row[0]) in {"cancelling", "cancelled"}


async def request_batch_cancel(
    batch_id: str,
    *,
    workspace_path: Optional[Path] = None,
) -> str:
    db_path = await _ensure_db(workspace_path)
    now = _now_iso()
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            "SELECT state FROM source_import_batches WHERE batch_id = ?",
            (batch_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise KeyError("Import batch not found")
        state = str(row[0])
        if state in {"queued", "importing"}:
            await db.execute(
                """
                UPDATE source_import_batches
                SET state = 'cancelling',
                    updated_at = ?
                WHERE batch_id = ?
                """,
                (now, batch_id),
            )
            await db.commit()
            return "cancelling"
        return state


async def mark_unprocessed_files_cancelled(
    batch_id: str,
    *,
    reason: str = "cancelled_by_user",
    workspace_path: Optional[Path] = None,
) -> None:
    db_path = await _ensure_db(workspace_path)
    now = _now_iso()
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            UPDATE source_import_files
            SET status = 'skipped',
                stage = 'cancelled',
                reason = ?,
                updated_at = ?
            WHERE batch_id = ?
              AND status IN ('queued', 'importing')
            """,
            (reason, now, batch_id),
        )
        await db.commit()


async def mark_interrupted_batches(
    *,
    workspace_path: Optional[Path] = None,
) -> int:
    """Mark active manifests from a previous process as interrupted.

    Folder imports are background tasks. If the sidecar exits while a task is
    queued/importing/cancelling/removing, there is no safe worker to resume
    implicitly on the next launch. We expose the partial batch as interrupted
    so the user can remove it or explicitly re-import later.
    """
    db_path = await _ensure_db(workspace_path)
    now = _now_iso()
    async with aiosqlite.connect(str(db_path)) as db:
        active_placeholders = ",".join("?" for _ in _ACTIVE_STATES)
        cursor = await db.execute(
            f"""
            SELECT batch_id FROM source_import_batches
            WHERE state IN ({active_placeholders})
            """,
            list(_ACTIVE_STATES),
        )
        rows = await cursor.fetchall()
        batch_ids = [str(row[0]) for row in rows]
        if not batch_ids:
            return 0

        placeholders = ",".join("?" for _ in batch_ids)
        await db.execute(
            f"""
            UPDATE source_import_files
            SET status = 'failed',
                stage = 'interrupted',
                reason = 'app_closed_during_import',
                updated_at = ?
            WHERE batch_id IN ({placeholders})
              AND status = 'importing'
            """,
            [now, *batch_ids],
        )
        await db.execute(
            f"""
            UPDATE source_import_batches
            SET state = 'interrupted',
                current_file = NULL,
                updated_at = ?,
                finished_at = COALESCE(finished_at, ?)
            WHERE batch_id IN ({placeholders})
            """,
            [now, now, *batch_ids],
        )
        await db.commit()
    return len(batch_ids)


async def update_file_status(
    batch_id: str,
    file_id: str,
    status: SourceImportFileStatus,
    *,
    stage: Optional[str] = None,
    reason: Optional[str] = None,
    duplicate_of: Optional[str] = None,
    content_hash: Optional[str] = None,
    warnings: Optional[list[str]] = None,
    note_paths: Optional[list[str]] = None,
    workspace_path: Optional[Path] = None,
) -> None:
    db_path = await _ensure_db(workspace_path)
    now = _now_iso()
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            UPDATE source_import_files
            SET status = ?,
                stage = ?,
                reason = ?,
                duplicate_of = ?,
                content_hash = COALESCE(?, content_hash),
                warnings = COALESCE(?, warnings),
                note_paths = COALESCE(?, note_paths),
                updated_at = ?
            WHERE batch_id = ? AND file_id = ?
            """,
            (
                status,
                stage,
                reason,
                duplicate_of,
                content_hash,
                json.dumps(warnings) if warnings is not None else None,
                json.dumps(note_paths) if note_paths is not None else None,
                now,
                batch_id,
                file_id,
            ),
        )
        await db.commit()
