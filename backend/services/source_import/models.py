from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


SourceGrantKind = Literal["local_folder"]


class SourceGrantRequest(BaseModel):
    path: str = Field(min_length=1)
    source_kind: SourceGrantKind = "local_folder"


class SourceGrantResponse(BaseModel):
    source_token: str
    source_kind: SourceGrantKind
    display_name: str
    root_path: str
    expires_at: str


class SourceScanRequest(BaseModel):
    source_token: str = Field(min_length=16)
    include_hidden: bool = False
    max_files: Optional[int] = Field(default=None, ge=1, le=100_000)


class SourceScanFileItem(BaseModel):
    id: str
    relpath: str
    filename: str
    extension: str
    size: int
    modified_at: Optional[str] = None
    status: Literal["supported", "unsupported", "skipped"]
    reason: Optional[str] = None


class SourceScanFolderSummary(BaseModel):
    relpath: str
    file_count: int
    total_size: int


class SourceScanLargestFile(BaseModel):
    relpath: str
    size: int
    extension: str


class SourceScanReport(BaseModel):
    scan_id: str
    source_kind: SourceGrantKind
    source_display_name: str
    source_root_path: str
    proposed_destination_root: str
    total_files_seen: int
    total_size_seen: int
    supported_file_count: int
    unsupported_file_count: int
    skipped_file_count: int
    skipped_by_reason: dict[str, int]
    counts_by_extension: dict[str, int]
    largest_files: list[SourceScanLargestFile]
    folder_summary: list[SourceScanFolderSummary]
    files: list[SourceScanFileItem]
    file_list_truncated: bool = False
    limit_hit: bool = False
    created_at: str
