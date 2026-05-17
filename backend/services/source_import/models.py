from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


SourceGrantKind = Literal["local_folder", "local_archive"]
SourceDuplicatePolicy = Literal["skip", "import"]
SourceImportState = Literal[
    "queued",
    "importing",
    "cancelling",
    "completed",
    "cancelled",
    "interrupted",
    "removing",
    "removed",
    "failed",
]
SourceImportFileStatus = Literal["queued", "importing", "done", "skipped", "failed"]
SourceImportRescanFileStatus = Literal[
    "new",
    "changed",
    "unchanged",
    "missing",
    "unsupported",
    "skipped",
]


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
    duplicate_policy: SourceDuplicatePolicy = "skip"


class SourceImportRemoveRequest(BaseModel):
    confirm_batch_id: str = Field(min_length=4)


class SourceImportRescanFileItem(BaseModel):
    id: str
    relpath: str
    filename: str
    extension: str
    size: int = 0
    modified_at: Optional[str] = None
    status: SourceImportRescanFileStatus
    reason: Optional[str] = None
    previous_status: Optional[SourceImportFileStatus] = None
    previous_size: Optional[int] = None
    previous_modified_at: Optional[str] = None


class SourceImportRescanReport(BaseModel):
    batch_id: str
    scan_id: Optional[str] = None
    source_kind: SourceGrantKind = "local_folder"
    source_display_name: str
    proposed_destination_root: str
    total_files_seen: int
    current_supported_file_count: int
    unsupported_file_count: int
    skipped_file_count: int
    unchanged_file_count: int
    changed_file_count: int
    new_file_count: int
    missing_file_count: int
    importable_file_count: int
    importable_total_size: int
    skipped_by_reason: dict[str, int]
    files: list[SourceImportRescanFileItem]
    file_list_truncated: bool = False
    created_at: str


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


class SourceImportFileReviewItem(BaseModel):
    file_id: str
    relpath: str
    filename: str
    extension: str
    size: int
    modified_at: Optional[str] = None
    status: Literal["skipped", "failed"]
    stage: Optional[str] = None
    reason: Optional[str] = None
    duplicate_of: Optional[str] = None
    note_paths: list[str] = Field(default_factory=list)
    can_retry: bool = False
    can_fix_locally: bool = False


class SourceImportFileReviewReport(BaseModel):
    batch_id: str
    source_display_name: str
    state: SourceImportState
    skipped_file_count: int = 0
    failed_file_count: int = 0
    problem_file_count: int = 0
    reason_counts: dict[str, int] = Field(default_factory=dict)
    files: list[SourceImportFileReviewItem] = Field(default_factory=list)
    file_list_truncated: bool = False
    updated_at: str


class SourceImportSuggestedQuestion(BaseModel):
    question: str
    reason: Literal["general", "file_types", "folders", "issues"] = "general"


class SourceImportCompletionSummary(BaseModel):
    batch_id: str
    source_display_name: str
    state: SourceImportState
    destination_root: str
    total_file_count: int
    imported_file_count: int = 0
    skipped_file_count: int = 0
    failed_file_count: int = 0
    duplicate_file_count: int = 0
    created_note_count: int = 0
    imported_extension_counts: dict[str, int] = Field(default_factory=dict)
    imported_folder_counts: dict[str, int] = Field(default_factory=dict)
    suggested_questions: list[SourceImportSuggestedQuestion] = Field(default_factory=list)
    can_ask_about_import: bool = False
    updated_at: str


class SourceImportBatchSummary(BaseModel):
    batch_id: str
    scan_id: str
    selection_id: str
    duplicate_policy: SourceDuplicatePolicy = "skip"
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
