from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from services.ingest import SUPPORTED_EXTENSIONS
from services.source_import.cloud_placeholders import (
    ONLINE_ONLY_PLACEHOLDER_REASON,
    detect_online_only_placeholder,
    display_extension_for_cloud_placeholder,
    filename_indicates_cloud_placeholder,
)
from services.source_import.models import (
    SourceScanFileItem,
    SourceScanFolderSummary,
    SourceScanLargestFile,
    SourceScanReport,
    SourceScanResult,
)


DEFAULT_MAX_FILES = 25_000
FILE_LIST_LIMIT = 500
LARGEST_FILES_LIMIT = 10
FOLDER_SUMMARY_LIMIT = 20

SKIP_DIR_NAMES = {
    ".cache",
    ".git",
    ".hg",
    ".svn",
    ".tmp",
    ".trash",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
    ".venv",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _slugify(text: str) -> str:
    if not text:
        return "source"
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    ascii_only = ascii_only.translate(str.maketrans({
        "\u0142": "l",
        "\u0141": "l",
        "\u0111": "d",
        "\u0110": "d",
        "\u00f8": "o",
        "\u00d8": "o",
        "\u00df": "ss",
    }))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return slug or "source"


def _relpath(path: Path, root: Path) -> str:
    if path == root:
        return "."
    return path.relative_to(root).as_posix()


def _is_hidden(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return any(part.startswith(".") for part in parts if part not in {"", "."})


def _safe_file_id(relpath: str) -> str:
    # Stable for review controls and not a content hash.
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", relpath).strip("_") or "root"
    digest = hashlib.sha256(relpath.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"


def _iter_scandir(path: Path) -> Iterable[os.DirEntry[str]]:
    with os.scandir(path) as it:
        for entry in it:
            yield entry


def scan_folder(
    root_path: Path,
    *,
    scan_id: str,
    include_hidden: bool = False,
    max_files: Optional[int] = None,
) -> SourceScanResult:
    root = root_path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Selected source is not a folder")

    max_files = max_files or DEFAULT_MAX_FILES
    total_files_seen = 0
    total_size_seen = 0
    supported_count = 0
    unsupported_count = 0
    skipped_count = 0
    skipped_by_reason: Counter[str] = Counter()
    counts_by_extension: Counter[str] = Counter()
    folders: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    largest: list[SourceScanLargestFile] = []
    preview_files: list[SourceScanFileItem] = []
    all_files: list[SourceScanFileItem] = []
    file_list_truncated = False
    limit_hit = False

    stack = [root]

    def add_file_item(item: SourceScanFileItem) -> None:
        nonlocal file_list_truncated
        all_files.append(item)
        if len(preview_files) < FILE_LIST_LIMIT:
            preview_files.append(item)
        else:
            file_list_truncated = True

    def add_largest(relpath: str, size: int, extension: str) -> None:
        largest.append(SourceScanLargestFile(relpath=relpath, size=size, extension=extension))
        largest.sort(key=lambda row: row.size, reverse=True)
        del largest[LARGEST_FILES_LIMIT:]

    def record_skip(reason: str, size: int = 0) -> None:
        nonlocal skipped_count, total_size_seen
        skipped_count += 1
        skipped_by_reason[reason] += 1
        total_size_seen += max(size, 0)

    def count_seen_file() -> bool:
        nonlocal total_files_seen, limit_hit
        total_files_seen += 1
        if total_files_seen > max_files:
            limit_hit = True
            record_skip("scan_file_limit")
            stack.clear()
            return False
        return True

    def add_placeholder_file(
        *,
        entry: os.DirEntry[str],
        rel: str,
        st: Optional[os.stat_result] = None,
    ) -> None:
        size = int(getattr(st, "st_size", 0) or 0) if st is not None else 0
        modified_at = _iso_from_timestamp(st.st_mtime) if st is not None else None
        extension = display_extension_for_cloud_placeholder(Path(entry.name))
        record_skip(ONLINE_ONLY_PLACEHOLDER_REASON, size)
        add_file_item(SourceScanFileItem(
            id=_safe_file_id(rel),
            relpath=rel,
            filename=entry.name,
            extension=extension,
            size=size,
            modified_at=modified_at,
            status="skipped",
            reason=ONLINE_ONLY_PLACEHOLDER_REASON,
        ))

    while stack:
        current = stack.pop()
        try:
            entries = sorted(_iter_scandir(current), key=lambda e: e.name.lower())
        except OSError:
            record_skip("unreadable_folder")
            continue

        for entry in reversed(entries):
            path = Path(entry.path)
            rel = _relpath(path, root)

            if not include_hidden and _is_hidden(path, root):
                if entry.is_dir(follow_symlinks=False):
                    record_skip("hidden_or_system_folder")
                else:
                    try:
                        st = entry.stat(follow_symlinks=False)
                        size = st.st_size
                    except OSError:
                        st = None
                        size = 0
                    if filename_indicates_cloud_placeholder(path):
                        if not count_seen_file():
                            break
                        add_placeholder_file(entry=entry, rel=rel, st=st)
                        continue
                    record_skip("hidden_or_system_file", size)
                continue

            if entry.is_symlink():
                try:
                    target = path.resolve(strict=True)
                    target.relative_to(root)
                    reason = "symlink"
                except (OSError, ValueError):
                    reason = "symlink_outside_root"
                record_skip(reason)
                continue

            if entry.is_dir(follow_symlinks=False):
                if entry.name in SKIP_DIR_NAMES:
                    record_skip("system_or_temporary_folder")
                    continue
                stack.append(path)
                continue

            if not entry.is_file(follow_symlinks=False):
                record_skip("unsupported_filesystem_entry")
                continue

            if not count_seen_file():
                break

            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                record_skip("unreadable_file")
                add_file_item(SourceScanFileItem(
                    id=_safe_file_id(rel),
                    relpath=rel,
                    filename=entry.name,
                    extension=Path(entry.name).suffix.lower(),
                    size=0,
                    modified_at=None,
                    status="skipped",
                    reason="unreadable_file",
                ))
                continue

            placeholder_reason = detect_online_only_placeholder(
                path,
                stat_result=st,
            )
            if placeholder_reason:
                add_placeholder_file(entry=entry, rel=rel, st=st)
                continue

            size = st.st_size
            modified_at = _iso_from_timestamp(st.st_mtime)
            total_size_seen += max(size, 0)
            extension = Path(entry.name).suffix.lower() or "(none)"
            counts_by_extension[extension] += 1
            folder_key = Path(rel).parent.as_posix()
            if folder_key == ".":
                folder_key = "."
            folders[folder_key][0] += 1
            folders[folder_key][1] += max(size, 0)
            add_largest(rel, size, extension)

            if extension in SUPPORTED_EXTENSIONS:
                supported_count += 1
                status = "supported"
                reason = None
            else:
                unsupported_count += 1
                status = "unsupported"
                reason = "unsupported_file_type"

            add_file_item(SourceScanFileItem(
                id=_safe_file_id(rel),
                relpath=rel,
                filename=entry.name,
                extension=extension,
                size=size,
                modified_at=modified_at,
                status=status,
                reason=reason,
            ))

    folder_summary = [
        SourceScanFolderSummary(relpath=rel, file_count=values[0], total_size=values[1])
        for rel, values in sorted(
            folders.items(),
            key=lambda item: (item[0].count("/"), item[0].lower()),
        )[:FOLDER_SUMMARY_LIMIT]
    ]

    report = SourceScanReport(
        scan_id=scan_id,
        source_kind="local_folder",
        source_display_name=root.name or str(root),
        source_root_path=str(root),
        proposed_destination_root=f"memory/imports/{_slugify(root.name or 'source')}/",
        total_files_seen=total_files_seen,
        total_size_seen=total_size_seen,
        supported_file_count=supported_count,
        unsupported_file_count=unsupported_count,
        skipped_file_count=skipped_count,
        skipped_by_reason=dict(sorted(skipped_by_reason.items())),
        counts_by_extension=dict(sorted(counts_by_extension.items())),
        largest_files=largest,
        folder_summary=folder_summary,
        files=preview_files,
        file_list_truncated=file_list_truncated,
        limit_hit=limit_hit,
        created_at=_now_iso(),
    )
    return SourceScanResult(report=report, files=all_files)
