from __future__ import annotations

import hashlib
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from services.source_import.limits import (
    MAX_ARCHIVE_ENTRIES,
    MAX_ARCHIVE_UNCOMPRESSED_BYTES,
)


ZIP_EXTENSION = ".zip"
MAX_ZIP_ENTRIES = MAX_ARCHIVE_ENTRIES
MAX_ZIP_UNCOMPRESSED_BYTES = MAX_ARCHIVE_UNCOMPRESSED_BYTES
ZIP_ENCRYPTED_FLAG = 0x1


class ArchiveError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ArchiveMemberMetadata:
    relpath: str
    member_name: str
    filename: str
    extension: str
    size: int
    modified_at: Optional[str]


@dataclass(frozen=True)
class ArchiveMemberReference:
    archive_relpath: str
    archive_path: Path
    member_name: str


def _normalise_member_name(name: str) -> str:
    cleaned = name.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return PurePosixPath(cleaned).as_posix()


def _validate_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(_normalise_member_name(name))
    if path.as_posix() in {"", "."} or path.is_absolute() or ".." in path.parts:
        raise ArchiveError("archive_unsafe_path", "Archive contains an unsafe path")
    return path


def _zip_modified_at(info: zipfile.ZipInfo) -> Optional[str]:
    try:
        return datetime(*info.date_time, tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def validate_zip_infos(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ArchiveError(
            "archive_entry_limit",
            f"Archive has too many entries ({len(infos)} > {MAX_ZIP_ENTRIES})",
        )

    total = 0
    seen: set[str] = set()
    for info in infos:
        member_path = _validate_member_path(info.filename)
        if info.flag_bits & ZIP_ENCRYPTED_FLAG:
            raise ArchiveError(
                "archive_encrypted",
                "Archive contains password-protected or encrypted files",
            )
        if info.is_dir():
            continue
        member_name = member_path.as_posix()
        if member_name in seen:
            raise ArchiveError("archive_duplicate_member", "Archive contains duplicate file paths")
        seen.add(member_name)
        total += max(info.file_size, 0)
        if total > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise ArchiveError("archive_size_limit", "Archive is too large after decompression")
    return infos


def open_safe_zip(path: Path) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ArchiveError("archive_unreadable", "File is not a valid ZIP-based document") from exc
    try:
        validate_zip_infos(zf)
    except Exception:
        zf.close()
        raise
    return zf


def archive_child_relpath(archive_relpath: str, member_name: str) -> str:
    member = _validate_member_path(member_name).as_posix()
    if not archive_relpath.strip("/"):
        return member
    return f"{archive_relpath.rstrip('/')}/{member}"


def iter_zip_member_metadata(
    archive_path: Path,
    *,
    archive_relpath: str,
) -> list[ArchiveMemberMetadata]:
    with open_safe_zip(archive_path) as zf:
        rows: list[ArchiveMemberMetadata] = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            member_path = _validate_member_path(info.filename)
            filename = member_path.name
            extension = Path(filename).suffix.lower() or "(none)"
            rows.append(
                ArchiveMemberMetadata(
                    relpath=archive_child_relpath(archive_relpath, member_path.as_posix()),
                    member_name=member_path.as_posix(),
                    filename=filename,
                    extension=extension,
                    size=max(int(info.file_size or 0), 0),
                    modified_at=_zip_modified_at(info),
                )
            )
    return rows


def find_archive_member_reference(
    root: Path,
    relpath: str,
) -> ArchiveMemberReference | None:
    rel = PurePosixPath(relpath)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        return None

    parts = rel.parts
    for index, part in enumerate(parts[:-1], start=1):
        if Path(part).suffix.lower() != ZIP_EXTENSION:
            continue
        archive_relpath = PurePosixPath(*parts[:index]).as_posix()
        archive_path = root / Path(*parts[:index])
        if not archive_path.is_file():
            continue
        member_name = PurePosixPath(*parts[index:]).as_posix()
        if not member_name:
            return None
        return ArchiveMemberReference(
            archive_relpath=archive_relpath,
            archive_path=archive_path,
            member_name=_normalise_member_name(member_name),
        )
    return None


def extract_archive_member_to_temp(
    *,
    root: Path,
    relpath: str,
    temp_dir: Path,
) -> tuple[Path, ArchiveMemberReference]:
    reference = find_archive_member_reference(root, relpath)
    if reference is None:
        raise ArchiveError("archive_member_not_found", "Archive member is no longer available")

    target, member_name = _extract_member_to_temp(
        archive_path=reference.archive_path,
        member_name=reference.member_name,
        relpath=relpath,
        temp_dir=temp_dir,
    )

    return target, ArchiveMemberReference(
        archive_relpath=reference.archive_relpath,
        archive_path=reference.archive_path,
        member_name=member_name,
    )


def extract_root_archive_member_to_temp(
    *,
    archive_path: Path,
    member_name: str,
    temp_dir: Path,
) -> tuple[Path, ArchiveMemberReference]:
    target, normalized_member_name = _extract_member_to_temp(
        archive_path=archive_path,
        member_name=member_name,
        relpath=member_name,
        temp_dir=temp_dir,
    )
    return target, ArchiveMemberReference(
        archive_relpath=archive_path.name,
        archive_path=archive_path,
        member_name=normalized_member_name,
    )


def _locate_member_info(
    zf: zipfile.ZipFile,
    member_name: str,
) -> tuple[zipfile.ZipInfo, str]:
    wanted = _validate_member_path(member_name).as_posix()
    for info in zf.infolist():
        if info.is_dir():
            continue
        current = _validate_member_path(info.filename).as_posix()
        if current == wanted:
            return info, current
    raise ArchiveError("archive_member_not_found", "Archive member is no longer available")


def _extract_member_to_temp(
    *,
    archive_path: Path,
    member_name: str,
    relpath: str,
    temp_dir: Path,
) -> tuple[Path, str]:
    with open_safe_zip(archive_path) as zf:
        info, normalized_member_name = _locate_member_info(zf, member_name)
        digest = hashlib.sha256(relpath.encode("utf-8")).hexdigest()[:12]
        name = PurePosixPath(normalized_member_name).name or "archive-member"
        stem = Path(name).stem or "archive-member"
        suffix = Path(name).suffix
        target = temp_dir / f"{stem}-{digest}{suffix}"
        with zf.open(info, "r") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
    return target, normalized_member_name
