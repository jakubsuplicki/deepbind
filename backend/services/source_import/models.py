from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


SourceGrantKind = Literal["local_folder"]
SourceImportState = Literal[
    "queued",
    "importing",
    "completed",
    "cancelled",
    "interrupted",
    "removing",
    "removed",
    "failed",
]
SourceImportFileStatus = Literal["queued", "importing", "done", "skipped", "failed"]


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


class SourceScanResult(BaseModel):
    report: SourceScanReport
    files: list[SourceScanFileItem] = Field(default_factory=list)


class SourceSelectionRequest(BaseModel):
    excluded_file_ids: list[str] = Field(default_factory=list)
    excluded_extensions: list[str] = Field(default_factory=list)
    excluded_folders: list[str] = Field(default_factory=list)


class SourceSelectionSummary(BaseModel):
    selection_id: str
    scan_id: str
    source_display_name: str
    proposed_destination_root: str
    approved_file_count: int
    approved_total_size: int
    excluded_file_count: int
    excluded_total_size: int
    unsupported_file_count: int
    skipped_file_count: int
    excluded_by_rule: dict[str, int]
    approved_files: list[SourceScanFileItem]
    approved_file_list_truncated: bool = False
    created_at: str


class SourceSelectionRecord(BaseModel):
    summary: SourceSelectionSummary
    approved_file_ids: list[str] = Field(default_factory=list)


class SourceImportStartRequest(BaseModel):
    selection_id: str = Field(min_length=4)


class SourceImportFileOutcome(BaseModel):
    file_id: str
    relpath: str
    filename: str
    extension: str
    size: int
    modified_at: Optional[str] = None
    status: SourceImportFileStatus
    stage: Optional[str] = None
    reason: Optional[str] = None
    duplicate_of: Optional[str] = None
    content_hash: Optional[str] = None
    note_paths: list[str] = Field(default_factory=list)


class SourceImportBatchSummary(BaseModel):
    batch_id: str
    scan_id: str
    selection_id: str
    source_kind: SourceGrantKind = "local_folder"
    source_display_name: str
    destination_root: str
    state: SourceImportState
    total_file_count: int
    imported_file_count: int = 0
    skipped_file_count: int = 0
    failed_file_count: int = 0
    created_note_count: int = 0
    total_bytes: int = 0
    processed_bytes: int = 0
    current_file: Optional[str] = None
    files: list[SourceImportFileOutcome] = Field(default_factory=list)
    started_at: str
    updated_at: str
    finished_at: Optional[str] = None
