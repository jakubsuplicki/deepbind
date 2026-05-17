from __future__ import annotations

# Source import limits are product behavior: every cap should surface as a
# reviewable skip/limit reason rather than a silent truncation.

DEFAULT_SCAN_MAX_FILES = 25_000
MAX_SCAN_REQUEST_FILES = 100_000

FILE_LIST_PREVIEW_LIMIT = 500
LARGEST_FILES_LIMIT = 10
FOLDER_SUMMARY_LIMIT = 20

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_APPROVED_BYTES_PER_BATCH = 1 * 1024 * 1024 * 1024

MAX_ARCHIVE_ENTRIES = 2_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_DEPTH = 1

# The current worker processes files sequentially, so extraction/index fan-out
# is intentionally capped at one import-owned unit of work for now.
MAX_CONCURRENT_EXTRACTORS = 1
MAX_CONCURRENT_INDEXING_JOBS_PER_IMPORT = 1
