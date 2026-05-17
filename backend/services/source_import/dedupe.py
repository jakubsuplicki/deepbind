from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiosqlite

from services.source_import.manifest import _decode_note_paths, _ensure_db


@dataclass(frozen=True)
class SourceImportDuplicateMatch:
    batch_id: str
    source_display_name: str
    relpath: str
    filename: str
    note_paths: list[str]

    @property
    def display_label(self) -> str:
        source = self.source_display_name.strip()
        if source:
            return f"{source}/{self.relpath}"
        return self.relpath


async def find_prior_imported_content_duplicate(
    *,
    content_hash: str,
    current_batch_id: str,
    workspace_path: Optional[Path] = None,
) -> SourceImportDuplicateMatch | None:
    if not content_hash:
        return None

    db_path = await _ensure_db(workspace_path)
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                f.batch_id,
                b.source_display_name,
                f.relpath,
                f.filename,
                f.note_paths
            FROM source_import_files f
            JOIN source_import_batches b ON b.batch_id = f.batch_id
            WHERE f.content_hash = ?
              AND f.batch_id != ?
              AND f.status = 'done'
              AND b.state != 'removed'
            ORDER BY b.started_at DESC, f.id DESC
            LIMIT 1
            """,
            (content_hash, current_batch_id),
        )
        row = await cursor.fetchone()

    if row is None:
        return None
    return SourceImportDuplicateMatch(
        batch_id=str(row["batch_id"]),
        source_display_name=str(row["source_display_name"]),
        relpath=str(row["relpath"]),
        filename=str(row["filename"]),
        note_paths=_decode_note_paths(row["note_paths"]),
    )
