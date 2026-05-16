export interface SourceGrantResponse {
  source_token: string
  source_kind: 'local_folder'
  display_name: string
  root_path: string
  expires_at: string
}

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
  source_kind: 'local_folder'
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

  return {
    pickFolderSource,
    scanSource,
  }
}
