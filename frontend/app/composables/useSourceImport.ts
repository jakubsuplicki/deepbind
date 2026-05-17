export interface SourceGrantResponse {
  source_token: string
  source_kind: 'local_folder' | 'local_archive'
  display_name: string
  root_path: string
  expires_at: string
}

export type SourceDuplicatePolicy = 'skip' | 'import'

export interface SourceScanFileItem {
  id: string
  relpath: string
  filename: string
  extension: string
  size: number
  modified_at?: string | null
  status: 'supported' | 'unsupported' | 'skipped'
  reason?: string | null
}

export interface SourceScanReport {
  scan_id: string
  source_kind: SourceGrantResponse['source_kind']
  source_display_name: string
  source_root_path: string
  proposed_destination_root: string
  total_files_seen: number
  total_size_seen: number
  supported_file_count: number
  unsupported_file_count: number
  skipped_file_count: number
  skipped_by_reason: Record<string, number>
  counts_by_extension: Record<string, number>
  largest_files: Array<{ relpath: string; size: number; extension: string }>
  folder_summary: Array<{ relpath: string; file_count: number; total_size: number }>
  files: SourceScanFileItem[]
  file_list_truncated: boolean
  limit_hit: boolean
  created_at: string
}

export interface SourceSelectionSummary {
  selection_id: string
  scan_id: string
  source_display_name: string
  proposed_destination_root: string
  approved_file_count: number
  approved_total_size: number
  excluded_file_count: number
  excluded_total_size: number
  unsupported_file_count: number
  skipped_file_count: number
  excluded_by_rule: Record<string, number>
  approved_files: SourceScanFileItem[]
  approved_file_list_truncated: boolean
  created_at: string
}

export interface SourceImportFileOutcome {
  file_id: string
  relpath: string
  filename: string
  extension: string
  size: number
  modified_at?: string | null
  status: 'queued' | 'importing' | 'done' | 'skipped' | 'failed'
  stage?: string | null
  reason?: string | null
  duplicate_of?: string | null
  content_hash?: string | null
  warnings: string[]
  note_paths: string[]
}

export interface SourceImportBatchSummary {
  batch_id: string
  scan_id: string
  selection_id: string
  duplicate_policy: SourceDuplicatePolicy
  source_kind: SourceGrantResponse['source_kind']
  source_display_name: string
  destination_root: string
  state:
    | 'queued'
    | 'importing'
    | 'cancelling'
    | 'completed'
    | 'cancelled'
    | 'interrupted'
    | 'removing'
    | 'removed'
    | 'failed'
  total_file_count: number
  imported_file_count: number
  skipped_file_count: number
  failed_file_count: number
  warning_file_count: number
  created_note_count: number
  total_bytes: number
  processed_bytes: number
  current_file?: string | null
  files: SourceImportFileOutcome[]
  started_at: string
  updated_at: string
  finished_at?: string | null
}

export interface SourceImportFileReviewItem {
  file_id: string
  relpath: string
  filename: string
  extension: string
  size: number
  modified_at?: string | null
  status: 'skipped' | 'failed'
  stage?: string | null
  reason?: string | null
  duplicate_of?: string | null
  note_paths: string[]
  can_retry: boolean
  can_fix_locally: boolean
}

export interface SourceImportFileReviewReport {
  batch_id: string
  source_display_name: string
  state: SourceImportBatchSummary['state']
  skipped_file_count: number
  failed_file_count: number
  problem_file_count: number
  reason_counts: Record<string, number>
  files: SourceImportFileReviewItem[]
  file_list_truncated: boolean
  updated_at: string
}

export interface SourceImportSuggestedQuestion {
  question: string
  reason: 'general' | 'file_types' | 'folders' | 'issues'
}

export interface SourceImportCompletionSummary {
  batch_id: string
  source_display_name: string
  state: SourceImportBatchSummary['state']
  destination_root: string
  total_file_count: number
  imported_file_count: number
  skipped_file_count: number
  failed_file_count: number
  duplicate_file_count: number
  warning_file_count: number
  created_note_count: number
  imported_extension_counts: Record<string, number>
  imported_folder_counts: Record<string, number>
  suggested_questions: SourceImportSuggestedQuestion[]
  can_ask_about_import: boolean
  updated_at: string
}

export interface SourceImportRescanFileItem {
  id: string
  relpath: string
  filename: string
  extension: string
  size: number
  modified_at?: string | null
  status: 'new' | 'changed' | 'unchanged' | 'missing' | 'unsupported' | 'skipped'
  reason?: string | null
  previous_status?: SourceImportFileOutcome['status'] | null
  previous_size?: number | null
  previous_modified_at?: string | null
}

export interface SourceImportRescanReport {
  batch_id: string
  scan_id?: string | null
  source_kind: SourceGrantResponse['source_kind']
  source_display_name: string
  proposed_destination_root: string
  total_files_seen: number
  current_supported_file_count: number
  unsupported_file_count: number
  skipped_file_count: number
  unchanged_file_count: number
  changed_file_count: number
  new_file_count: number
  missing_file_count: number
  importable_file_count: number
  importable_total_size: number
  skipped_by_reason: Record<string, number>
  files: SourceImportRescanFileItem[]
  file_list_truncated: boolean
  created_at: string
}

export interface SourceSelectionOptions {
  excludedFileIds?: string[]
  excludedExtensions?: string[]
  excludedFolders?: string[]
}

export interface SourceImportStartOptions {
  duplicatePolicy?: SourceDuplicatePolicy
}

export function useSourceImport() {
  async function pickFolderSource(): Promise<SourceGrantResponse> {
    const tauriWindow = typeof window === 'undefined'
      ? undefined
      : window as Window & { __TAURI_INTERNALS__?: unknown }
    if (!tauriWindow?.__TAURI_INTERNALS__) {
      throw new Error('Folder source import is available in the desktop app.')
    }
    const { invoke } = await import('@tauri-apps/api/core')
    return await invoke<SourceGrantResponse>('source_import_pick_folder')
  }

  async function pickArchiveSource(): Promise<SourceGrantResponse> {
    const tauriWindow = typeof window === 'undefined'
      ? undefined
      : window as Window & { __TAURI_INTERNALS__?: unknown }
    if (!tauriWindow?.__TAURI_INTERNALS__) {
      throw new Error('Archive source import is available in the desktop app.')
    }
    const { invoke } = await import('@tauri-apps/api/core')
    return await invoke<SourceGrantResponse>('source_import_pick_archive')
  }

  async function pickSampleDataset(): Promise<SourceGrantResponse> {
    const tauriWindow = typeof window === 'undefined'
      ? undefined
      : window as Window & { __TAURI_INTERNALS__?: unknown }
    if (!tauriWindow?.__TAURI_INTERNALS__) {
      throw new Error('Sample source import is available in the desktop app.')
    }
    const { invoke } = await import('@tauri-apps/api/core')
    return await invoke<SourceGrantResponse>('source_import_pick_sample_dataset')
  }

  async function scanSource(
    sourceToken: string,
    options: { includeHidden?: boolean; maxFiles?: number } = {},
  ): Promise<SourceScanReport> {
    return await $fetch<SourceScanReport>(apiUrl('/api/source-import/scan'), {
      method: 'POST',
      body: {
        source_token: sourceToken,
        include_hidden: options.includeHidden ?? false,
        max_files: options.maxFiles,
      },
    })
  }

  async function createSelection(
    scanId: string,
    options: SourceSelectionOptions = {},
  ): Promise<SourceSelectionSummary> {
    return await $fetch<SourceSelectionSummary>(
      apiUrl(`/api/source-import/scans/${encodeURIComponent(scanId)}/selection`),
      {
        method: 'POST',
        body: {
          excluded_file_ids: options.excludedFileIds ?? [],
          excluded_extensions: options.excludedExtensions ?? [],
          excluded_folders: options.excludedFolders ?? [],
        },
      },
    )
  }

  async function startImport(
    scanId: string,
    selectionId: string,
    options: SourceImportStartOptions = {},
  ): Promise<SourceImportBatchSummary> {
    return await $fetch<SourceImportBatchSummary>(
      apiUrl(`/api/source-import/scans/${encodeURIComponent(scanId)}/start`),
      {
        method: 'POST',
        body: {
          selection_id: selectionId,
          duplicate_policy: options.duplicatePolicy ?? 'skip',
        },
      },
    )
  }

  async function getImport(batchId: string): Promise<SourceImportBatchSummary> {
    return await $fetch<SourceImportBatchSummary>(
      apiUrl(`/api/source-import/imports/${encodeURIComponent(batchId)}`),
    )
  }

  async function listImports(limit = 10): Promise<SourceImportBatchSummary[]> {
    return await $fetch<SourceImportBatchSummary[]>(
      apiUrl('/api/source-import/imports'),
      {
        query: { limit },
      },
    )
  }

  async function getImportCompletion(
    batchId: string,
  ): Promise<SourceImportCompletionSummary> {
    return await $fetch<SourceImportCompletionSummary>(
      apiUrl(`/api/source-import/imports/${encodeURIComponent(batchId)}/completion`),
    )
  }

  async function getImportReview(
    batchId: string,
    limit = 100,
  ): Promise<SourceImportFileReviewReport> {
    return await $fetch<SourceImportFileReviewReport>(
      apiUrl(`/api/source-import/imports/${encodeURIComponent(batchId)}/review`),
      {
        query: { limit },
      },
    )
  }

  async function cancelImport(batchId: string): Promise<SourceImportBatchSummary> {
    return await $fetch<SourceImportBatchSummary>(
      apiUrl(`/api/source-import/imports/${encodeURIComponent(batchId)}/cancel`),
      {
        method: 'POST',
      },
    )
  }

  async function removeImport(
    batchId: string,
    confirmBatchId: string,
  ): Promise<SourceImportBatchSummary> {
    return await $fetch<SourceImportBatchSummary>(
      apiUrl(`/api/source-import/imports/${encodeURIComponent(batchId)}/remove`),
      {
        method: 'POST',
        body: {
          confirm_batch_id: confirmBatchId,
        },
      },
    )
  }

  async function rescanImport(batchId: string): Promise<SourceImportRescanReport> {
    return await $fetch<SourceImportRescanReport>(
      apiUrl(`/api/source-import/imports/${encodeURIComponent(batchId)}/rescan`),
      {
        method: 'POST',
      },
    )
  }

  return {
    cancelImport,
    createSelection,
    getImportCompletion,
    getImportReview,
    getImport,
    listImports,
    pickArchiveSource,
    pickSampleDataset,
    pickFolderSource,
    removeImport,
    rescanImport,
    scanSource,
    startImport,
  }
}
