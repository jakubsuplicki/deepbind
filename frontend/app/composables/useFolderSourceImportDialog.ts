import { computed, onUnmounted, ref, type Ref } from 'vue'
import { useSourceImport } from '~/composables/useSourceImport'
import type {
  SourceGrantResponse,
  SourceImportBatchSummary,
  SourceImportCompletionSummary,
  SourceImportFileReviewItem,
  SourceImportFileReviewReport,
  SourceImportRescanReport,
  SourceImportSuggestedQuestion,
  SourceScanReport,
  SourceSelectionSummary,
} from '~/composables/useSourceImport'

interface UseFolderSourceImportDialogOptions {
  error: Ref<string>
  success: Ref<string>
  onImported: (result: Record<string, unknown>) => void
}

export function folderIssueCanRetry(file: { status: string; reason?: string | null }): boolean {
  const reason = (file.reason ?? '').toLowerCase()
  if (reason.includes('duplicate_content')) return false
  if (file.status === 'failed') return true
  return [
    'app_closed_during_import',
    'cancelled_by_user',
    'no longer available',
    'outside the selected folder',
    'permission',
    'unreadable',
  ].some(marker => reason.includes(marker))
}

export function folderIssueCanFixLocally(file: { reason?: string | null }): boolean {
  const reason = (file.reason ?? '').toLowerCase()
  return [
    'encrypted',
    'file_too_large',
    'limit',
    'nested_archive',
    'no longer available',
    'online_only',
    'outside the selected folder',
    'password',
    'permission',
    'placeholder',
    'source file',
    'unreadable',
    'unsupported',
  ].some(marker => reason.includes(marker))
}

export function folderIssueActionHint(file: SourceImportFileReviewItem): string {
  const reason = (file.reason ?? '').toLowerCase()
  if (reason.includes('duplicate_content')) {
    return file.duplicate_of
      ? `Already imported from ${file.duplicate_of}.`
      : 'Already imported from another file in this batch.'
  }
  if (reason.includes('cancelled_by_user')) {
    return 'Scan again when you are ready to import it.'
  }
  if (reason.includes('app_closed_during_import')) {
    return 'Scan again to retry the unfinished file.'
  }
  if (reason.includes('password') || reason.includes('encrypted')) {
    return 'Export an unlocked copy, then import it.'
  }
  if (reason.includes('online_only') || reason.includes('placeholder')) {
    return 'Download it to this computer, then scan again.'
  }
  if (reason.includes('nested_archive')) {
    return 'Extract the nested archive locally, then import it as its own source.'
  }
  if (
    reason.includes('no longer available') ||
    reason.includes('outside the selected folder')
  ) {
    return 'Put the file back in the folder, then scan again.'
  }
  if (reason.includes('permission') || reason.includes('unreadable')) {
    return 'Check local file permissions, then scan again.'
  }
  if (reason.includes('unsupported')) {
    return 'Convert it to a supported document type.'
  }
  if (reason.includes('archive_')) {
    return 'Extract or repair the archive locally, then scan again.'
  }
  if (reason.includes('too large') || reason.includes('limit')) {
    return 'Split or reduce the file before importing.'
  }
  if (file.status === 'failed') {
    return 'Check that the file opens locally, then scan again.'
  }
  return 'This file was left out of memory for this import.'
}

export function humanizeSourceImportReason(reason: string): string {
  const labels: Record<string, string> = {
    archive_duplicate_member: 'Duplicate archive path',
    archive_empty: 'Empty archive',
    archive_entry_limit: 'Archive limit',
    archive_member_not_found: 'Archive file missing',
    archive_size_limit: 'Archive too large',
    archive_unreadable: 'Unreadable archive',
    archive_unsafe_path: 'Unsafe archive path',
    batch_size_limit: 'Import size limit',
    duplicate_content: 'Duplicate',
    duplicate_content_existing_import: 'Already imported',
    file_too_large: 'File too large',
    nested_archive: 'Nested archive',
    online_only_placeholder: 'Online-only file',
    previous_duplicate_content: 'Already imported',
    scan_file_limit: 'Scan limit',
  }
  if (labels[reason]) return labels[reason]
  return reason
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (ch) => ch.toUpperCase())
}

export function useFolderSourceImportDialog(options: UseFolderSourceImportDialogOptions) {
  const sourceImport = useSourceImport()

  const folderPicking = ref(false)
  const folderArchivePicking = ref(false)
  const folderSamplePicking = ref(false)
  const folderScanning = ref(false)
  const folderGrant = ref<SourceGrantResponse | null>(null)
  const folderScan = ref<SourceScanReport | null>(null)
  const folderSelection = ref<SourceSelectionSummary | null>(null)
  const folderSelectionLoading = ref(false)
  const folderImport = ref<SourceImportBatchSummary | null>(null)
  const folderImportStarting = ref(false)
  const folderImportCancelling = ref(false)
  const folderImportRemoving = ref(false)
  const folderRemoveConfirmOpen = ref(false)
  const folderImportCompletion = ref<SourceImportCompletionSummary | null>(null)
  const folderImportCompletionLoading = ref(false)
  const folderImportReview = ref<SourceImportFileReviewReport | null>(null)
  const folderImportReviewLoading = ref(false)
  const folderImportHistory = ref<SourceImportBatchSummary[]>([])
  const folderImportHistoryLoading = ref(false)
  const folderImportHistoryError = ref('')
  const folderRescan = ref<SourceImportRescanReport | null>(null)
  const folderRescanning = ref(false)
  const folderRescanImportStarting = ref(false)
  const includeHiddenInFolderScan = ref(false)
  const importDuplicateContent = ref(false)
  const excludedFileIds = ref<string[]>([])
  const excludedExtensions = ref<string[]>([])
  const excludedFolders = ref<string[]>([])
  let folderImportPollTimer: ReturnType<typeof setTimeout> | null = null

  const extensionRows = computed(() => {
    if (!folderScan.value) return []
    return Object.entries(folderScan.value.counts_by_extension)
      .map(([extension, count]) => ({ extension, count }))
      .sort((a, b) => b.count - a.count || a.extension.localeCompare(b.extension))
  })

  const skipRows = computed(() => {
    if (!folderScan.value) return []
    return Object.entries(folderScan.value.skipped_by_reason)
      .map(([reason, count]) => ({ reason, count }))
      .sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason))
  })

  const approvalRuleRows = computed(() => {
    if (!folderSelection.value) return []
    return Object.entries(folderSelection.value.excluded_by_rule)
      .map(([reason, count]) => ({ reason, count }))
      .filter(row => row.count > 0)
      .sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason))
  })

  const folderRows = computed(() => {
    if (!folderScan.value) return []
    return folderScan.value.folder_summary
      .filter(row => row.relpath !== '.')
      .slice(0, 12)
  })

  const folderSourceIsArchive = computed(() =>
    folderGrant.value?.source_kind === 'local_archive' ||
    folderScan.value?.source_kind === 'local_archive' ||
    folderImport.value?.source_kind === 'local_archive'
  )

  const folderHiddenToggleLabel = computed(() =>
    folderSourceIsArchive.value ? 'Include hidden archive entries' : 'Include hidden files and folders'
  )

  const folderScanningLabel = computed(() =>
    folderSourceIsArchive.value
      ? 'Scanning archive file names, types, sizes, and folders...'
      : 'Scanning file names, types, sizes, and folders...'
  )

  const folderImportActive = computed(() =>
    folderImportStarting.value ||
    folderImportCancelling.value ||
    folderImportRemoving.value ||
    folderRescanImportStarting.value ||
    folderImport.value?.state === 'queued' ||
    folderImport.value?.state === 'importing' ||
    folderImport.value?.state === 'cancelling' ||
    folderImport.value?.state === 'removing'
  )

  const folderImportTerminal = computed(() =>
    !!folderImport.value &&
    !['queued', 'importing', 'cancelling', 'removing'].includes(folderImport.value.state)
  )

  const folderImportCanCancel = computed(() =>
    !!folderImport.value &&
    ['queued', 'importing'].includes(folderImport.value.state)
  )

  const folderImportCanRemove = computed(() =>
    !!folderImport.value &&
    ['completed', 'failed', 'cancelled', 'interrupted'].includes(folderImport.value.state) &&
    !folderImportActive.value
  )

  const folderImportCanRescan = computed(() =>
    !!folderImport.value &&
    ['completed', 'failed', 'cancelled', 'interrupted'].includes(folderImport.value.state) &&
    !folderRescanning.value &&
    !folderImportActive.value &&
    !folderImportRemoving.value
  )

  const folderImportStatusLabel = computed(() => {
    if (folderImportRemoving.value || folderImport.value?.state === 'removing') {
      return 'Removing import'
    }
    if (folderImportCancelling.value || folderImport.value?.state === 'cancelling') {
      return 'Cancelling import'
    }
    if (folderImportActive.value) return 'Creating memory'
    return humanizeSourceImportReason(folderImport.value?.state ?? '')
  })

  const folderRescanStatusRows = computed(() => {
    if (!folderRescan.value) return []
    return [
      { label: 'New', count: folderRescan.value.new_file_count },
      { label: 'Changed', count: folderRescan.value.changed_file_count },
      { label: 'Unchanged', count: folderRescan.value.unchanged_file_count },
      { label: 'Missing', count: folderRescan.value.missing_file_count },
    ]
  })

  const folderImportProblemCount = computed(() =>
    (folderImport.value?.skipped_file_count ?? 0) +
    (folderImport.value?.failed_file_count ?? 0)
  )

  const folderImportReviewReasonRows = computed(() => {
    if (!folderImportReview.value) return []
    return Object.entries(folderImportReview.value.reason_counts)
      .map(([reason, count]) => ({ reason, count }))
      .sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason))
      .slice(0, 6)
  })

  const folderImportReviewRows = computed<SourceImportFileReviewItem[]>(() => {
    if (folderImportReview.value?.files.length) return folderImportReview.value.files
    if (!folderImport.value?.files.length) return []
    return folderImport.value.files
      .filter(file => file.status === 'skipped' || file.status === 'failed')
      .slice(0, 8)
      .map(file => ({
        ...file,
        status: file.status as 'skipped' | 'failed',
        can_retry: folderIssueCanRetry(file),
        can_fix_locally: folderIssueCanFixLocally(file),
      }))
  })

  const folderImportReviewTruncated = computed(() => {
    if (folderImportReview.value) return folderImportReview.value.file_list_truncated
    return folderImportReviewRows.value.length < folderImportProblemCount.value
  })

  const folderImportTypeRows = computed(() => {
    if (!folderImportCompletion.value) return []
    return Object.entries(folderImportCompletion.value.imported_extension_counts)
      .map(([extension, count]) => ({ extension, count }))
      .slice(0, 5)
  })

  const folderImportQuestionRows = computed<SourceImportSuggestedQuestion[]>(() =>
    folderImportCompletion.value?.suggested_questions ?? []
  )

  const folderImportHistoryRows = computed(() =>
    folderImportHistory.value.slice(0, 10)
  )

  const folderRemoveConfirmMessage = computed(() => {
    const count = folderImport.value?.created_note_count ?? 0
    const noun = count === 1 ? 'note' : 'notes'
    return `${count} created ${noun} will be moved out of memory. Unrelated notes will stay.`
  })

  const folderImportProgressPercent = computed(() => {
    if (!folderImport.value || folderImport.value.total_file_count <= 0) return '0%'
    const done =
      folderImport.value.imported_file_count +
      folderImport.value.skipped_file_count +
      folderImport.value.failed_file_count
    return `${Math.min(100, Math.round((done / folderImport.value.total_file_count) * 100))}%`
  })

  const folderReviewLocked = computed(() =>
    folderSelectionLoading.value ||
    folderRescanning.value ||
    folderImportActive.value ||
    folderImportTerminal.value
  )

  function resetFolderReview() {
    clearFolderImportPoll()
    folderScan.value = null
    folderSelection.value = null
    folderSelectionLoading.value = false
    folderImport.value = null
    folderImportStarting.value = false
    folderImportCancelling.value = false
    folderImportRemoving.value = false
    folderRemoveConfirmOpen.value = false
    folderImportCompletion.value = null
    folderImportCompletionLoading.value = false
    folderImportReview.value = null
    folderImportReviewLoading.value = false
    folderRescan.value = null
    folderRescanning.value = false
    folderRescanImportStarting.value = false
    importDuplicateContent.value = false
    excludedFileIds.value = []
    excludedExtensions.value = []
    excludedFolders.value = []
  }

  function resetFolderMode() {
    folderGrant.value = null
    folderArchivePicking.value = false
    includeHiddenInFolderScan.value = false
    resetFolderReview()
  }

  function resetFolderActiveSurface() {
    clearFolderImportPoll()
    folderGrant.value = null
    folderScan.value = null
    folderSelection.value = null
    folderSelectionLoading.value = false
    folderImportCompletion.value = null
    folderImportCompletionLoading.value = false
    folderImportReview.value = null
    folderImportReviewLoading.value = false
    folderRescan.value = null
    folderRescanning.value = false
    folderRescanImportStarting.value = false
    folderRemoveConfirmOpen.value = false
    importDuplicateContent.value = false
    excludedFileIds.value = []
    excludedExtensions.value = []
    excludedFolders.value = []
  }

  async function loadFolderImportHistory() {
    folderImportHistoryLoading.value = true
    folderImportHistoryError.value = ''
    try {
      folderImportHistory.value = await sourceImport.listImports(10)
    } catch (err: unknown) {
      folderImportHistoryError.value =
        err instanceof Error ? err.message : 'Failed to load source import history'
    } finally {
      folderImportHistoryLoading.value = false
    }
  }

  function openFolderImportHistoryItem(batch: SourceImportBatchSummary) {
    options.error.value = ''
    options.success.value = ''
    resetFolderActiveSurface()
    folderImport.value = batch
    if (folderImportTerminal.value) {
      void refreshFolderImportCompletion()
      void refreshFolderImportReview()
      return
    }
    scheduleFolderImportPoll()
  }

  async function chooseFolderSource() {
    folderPicking.value = true
    options.error.value = ''
    options.success.value = ''
    resetFolderReview()
    try {
      folderGrant.value = await sourceImport.pickFolderSource()
    } catch (err: unknown) {
      options.error.value = err instanceof Error ? err.message : 'Folder selection failed'
    } finally {
      folderPicking.value = false
    }
  }

  async function chooseArchiveSource() {
    folderArchivePicking.value = true
    options.error.value = ''
    options.success.value = ''
    resetFolderReview()
    try {
      folderGrant.value = await sourceImport.pickArchiveSource()
      options.success.value = 'ZIP archive selected. Scan it when ready.'
    } catch (err: unknown) {
      options.error.value = err instanceof Error ? err.message : 'Archive selection failed'
    } finally {
      folderArchivePicking.value = false
    }
  }

  async function chooseSampleDataset() {
    folderSamplePicking.value = true
    options.error.value = ''
    options.success.value = ''
    resetFolderReview()
    try {
      folderGrant.value = await sourceImport.pickSampleDataset()
      options.success.value = 'Sample folder selected. Scan it when ready.'
    } catch (err: unknown) {
      options.error.value = err instanceof Error ? err.message : 'Sample folder selection failed'
    } finally {
      folderSamplePicking.value = false
    }
  }

  async function scanFolderSource() {
    if (!folderGrant.value) return
    folderScanning.value = true
    options.error.value = ''
    options.success.value = ''
    resetFolderReview()
    try {
      folderScan.value = await sourceImport.scanSource(folderGrant.value.source_token, {
        includeHidden: includeHiddenInFolderScan.value,
      })
      await refreshFolderSelection()
      options.success.value =
        `Scan ready: ${folderScan.value.supported_file_count} supported files, ` +
        `${folderScan.value.skipped_file_count} skipped.`
    } catch (err: unknown) {
      options.error.value = err instanceof Error ? err.message : 'Source scan failed'
    } finally {
      folderScanning.value = false
    }
  }

  async function refreshFolderSelection() {
    if (!folderScan.value) return
    folderSelectionLoading.value = true
    options.error.value = ''
    folderImport.value = null
    folderImportCompletion.value = null
    folderImportReview.value = null
    folderRescan.value = null
    clearFolderImportPoll()
    try {
      folderSelection.value = await sourceImport.createSelection(folderScan.value.scan_id, {
        excludedFileIds: excludedFileIds.value,
        excludedExtensions: excludedExtensions.value,
        excludedFolders: excludedFolders.value,
      })
    } catch (err: unknown) {
      options.error.value = err instanceof Error ? err.message : 'Failed to update folder review'
    } finally {
      folderSelectionLoading.value = false
    }
  }

  async function startFolderImport() {
    if (!folderScan.value || !folderSelection.value) return
    folderImportStarting.value = true
    options.error.value = ''
    options.success.value = ''
    folderImportCompletion.value = null
    folderImportReview.value = null
    folderRescan.value = null
    try {
      folderImport.value = importDuplicateContent.value
        ? await sourceImport.startImport(
            folderScan.value.scan_id,
            folderSelection.value.selection_id,
            { duplicatePolicy: 'import' },
          )
        : await sourceImport.startImport(
            folderScan.value.scan_id,
            folderSelection.value.selection_id,
          )
      options.success.value = `Import started: ${folderImport.value.total_file_count} files approved.`
      void loadFolderImportHistory()
      scheduleFolderImportPoll()
    } catch (err: unknown) {
      options.error.value = err instanceof Error ? err.message : 'Folder import failed to start'
    } finally {
      folderImportStarting.value = false
    }
  }

  async function handleFolderPrimaryAction() {
    if (folderScan.value) {
      await startFolderImport()
      return
    }
    await scanFolderSource()
  }

  async function cancelFolderImport() {
    if (!folderImport.value || !folderImportCanCancel.value || folderImportCancelling.value) return
    const batch = folderImport.value

    folderImportCancelling.value = true
    options.error.value = ''
    options.success.value = ''
    try {
      folderImport.value = await sourceImport.cancelImport(batch.batch_id)
      options.success.value = 'Cancelling import after the current file finishes.'
      scheduleFolderImportPoll()
    } catch (err: unknown) {
      options.error.value = err instanceof Error ? err.message : 'Failed to cancel import'
    } finally {
      folderImportCancelling.value = false
    }
  }

  function requestRemoveFolderImport() {
    if (!folderImport.value || !folderImportCanRemove.value || folderImportRemoving.value) return
    folderRemoveConfirmOpen.value = true
  }

  async function confirmRemoveFolderImport() {
    if (!folderImport.value || !folderImportCanRemove.value || folderImportRemoving.value) return
    const batch = folderImport.value

    folderImportRemoving.value = true
    options.error.value = ''
    options.success.value = ''
    folderImportCompletion.value = null
    folderRescan.value = null
    clearFolderImportPoll()
    try {
      folderImport.value = await sourceImport.removeImport(batch.batch_id, batch.batch_id)
      options.success.value =
        `Removed import: ${folderImport.value.created_note_count} created notes moved out of memory.`
      folderImportReview.value = null
      options.onImported(folderImport.value as unknown as Record<string, unknown>)
      folderRemoveConfirmOpen.value = false
      void loadFolderImportHistory()
    } catch (err: unknown) {
      options.error.value = err instanceof Error ? err.message : 'Failed to remove import'
      folderRemoveConfirmOpen.value = false
    } finally {
      folderImportRemoving.value = false
    }
  }

  async function rescanFolderImport() {
    if (!folderImport.value || !folderImportCanRescan.value || folderRescanning.value) return
    const batch = folderImport.value

    folderRescanning.value = true
    options.error.value = ''
    options.success.value = ''
    folderImportCompletion.value = null
    folderImportReview.value = null
    folderRescan.value = null
    try {
      folderRescan.value = await sourceImport.rescanImport(batch.batch_id)
      if (folderRescan.value.importable_file_count > 0) {
        options.success.value =
          `Scan again ready: ${folderRescan.value.new_file_count} new and ` +
          `${folderRescan.value.changed_file_count} changed files.`
      } else {
        options.success.value = 'Scan again found no new or changed files.'
        void refreshFolderImportCompletion()
        void refreshFolderImportReview()
      }
    } catch (err: unknown) {
      options.error.value = err instanceof Error ? err.message : 'Failed to scan source again'
    } finally {
      folderRescanning.value = false
    }
  }

  async function startFolderRescanImport() {
    if (!folderRescan.value?.scan_id || folderRescan.value.importable_file_count === 0) return
    if (folderRescanImportStarting.value || folderImportActive.value) return

    folderRescanImportStarting.value = true
    options.error.value = ''
    options.success.value = ''
    folderImportCompletion.value = null
    folderImportReview.value = null
    try {
      const selection = await sourceImport.createSelection(folderRescan.value.scan_id, {})
      folderImport.value = importDuplicateContent.value
        ? await sourceImport.startImport(
            folderRescan.value.scan_id,
            selection.selection_id,
            { duplicatePolicy: 'import' },
          )
        : await sourceImport.startImport(
            folderRescan.value.scan_id,
            selection.selection_id,
          )
      folderSelection.value = selection
      options.success.value =
        `Importing ${folderImport.value.total_file_count} new or changed files.`
      folderRescan.value = null
      void loadFolderImportHistory()
      scheduleFolderImportPoll()
    } catch (err: unknown) {
      options.error.value = err instanceof Error ? err.message : 'Failed to import source changes'
    } finally {
      folderRescanImportStarting.value = false
    }
  }

  async function refreshFolderImportCompletion() {
    const batch = folderImport.value
    if (!batch || batch.imported_file_count === 0 || batch.state === 'removed') {
      folderImportCompletion.value = null
      return
    }
    folderImportCompletionLoading.value = true
    try {
      folderImportCompletion.value = await sourceImport.getImportCompletion(batch.batch_id)
    } catch (err: unknown) {
      options.error.value = err instanceof Error ? err.message : 'Failed to load import summary'
    } finally {
      folderImportCompletionLoading.value = false
    }
  }

  async function refreshFolderImportReview() {
    const batch = folderImport.value
    if (!batch || folderImportProblemCount.value === 0) {
      folderImportReview.value = null
      return
    }
    folderImportReviewLoading.value = true
    try {
      folderImportReview.value = await sourceImport.getImportReview(batch.batch_id, 100)
    } catch (err: unknown) {
      options.error.value = err instanceof Error ? err.message : 'Failed to load skipped file review'
    } finally {
      folderImportReviewLoading.value = false
    }
  }

  function clearFolderImportPoll() {
    if (folderImportPollTimer) {
      clearTimeout(folderImportPollTimer)
      folderImportPollTimer = null
    }
  }

  function scheduleFolderImportPoll() {
    clearFolderImportPoll()
    const batchId = folderImport.value?.batch_id
    if (!batchId || folderImportTerminal.value) {
      if (folderImport.value?.state === 'completed') {
        options.success.value =
          `Imported ${folderImport.value.imported_file_count} files ` +
          `and created ${folderImport.value.created_note_count} notes.`
        options.onImported(folderImport.value as unknown as Record<string, unknown>)
        void loadFolderImportHistory()
        void refreshFolderImportCompletion()
        void refreshFolderImportReview()
      } else if (folderImport.value?.state === 'cancelled') {
        options.success.value =
          `Import cancelled: ${folderImport.value.imported_file_count} files imported ` +
          `and ${folderImport.value.skipped_file_count} skipped.`
        options.onImported(folderImport.value as unknown as Record<string, unknown>)
        void loadFolderImportHistory()
        void refreshFolderImportCompletion()
        void refreshFolderImportReview()
      } else if (folderImport.value?.state === 'interrupted') {
        options.error.value = 'Import was interrupted. Created notes remain until you remove the import.'
        void loadFolderImportHistory()
        void refreshFolderImportCompletion()
        void refreshFolderImportReview()
      } else if (folderImport.value?.state === 'failed') {
        void loadFolderImportHistory()
        void refreshFolderImportCompletion()
        void refreshFolderImportReview()
      }
      return
    }
    folderImportPollTimer = setTimeout(async () => {
      try {
        folderImport.value = await sourceImport.getImport(batchId)
        scheduleFolderImportPoll()
      } catch (err: unknown) {
        options.error.value = err instanceof Error ? err.message : 'Failed to refresh import progress'
      }
    }, 900)
  }

  function askFolderImportQuestion(question: string) {
    const batch = folderImport.value
    if (!batch || !question.trim()) return
    void navigateTo({
      path: '/main',
      query: {
        import_batch_id: batch.batch_id,
        q: question,
      },
    })
  }

  function isExcludedFile(id: string): boolean {
    return excludedFileIds.value.includes(id)
  }

  function isExcludedExtension(extension: string): boolean {
    return excludedExtensions.value.includes(extension)
  }

  function isExcludedFolder(relpath: string): boolean {
    return excludedFolders.value.includes(relpath)
  }

  async function toggleFileExclusion(id: string) {
    if (folderReviewLocked.value) return
    excludedFileIds.value = toggleListValue(excludedFileIds.value, id)
    await refreshFolderSelection()
  }

  async function toggleExtensionExclusion(extension: string) {
    if (folderReviewLocked.value) return
    excludedExtensions.value = toggleListValue(excludedExtensions.value, extension)
    await refreshFolderSelection()
  }

  async function toggleFolderExclusion(relpath: string) {
    if (folderReviewLocked.value) return
    excludedFolders.value = toggleListValue(excludedFolders.value, relpath)
    await refreshFolderSelection()
  }

  function toggleListValue(values: string[], value: string): string[] {
    return values.includes(value)
      ? values.filter(item => item !== value)
      : [...values, value]
  }

  onUnmounted(() => {
    clearFolderImportPoll()
  })

  return {
    approvalRuleRows,
    askFolderImportQuestion,
    cancelFolderImport,
    chooseArchiveSource,
    chooseFolderSource,
    chooseSampleDataset,
    confirmRemoveFolderImport,
    extensionRows,
    folderArchivePicking,
    folderGrant,
    folderHiddenToggleLabel,
    folderImport,
    folderImportActive,
    folderImportCanCancel,
    folderImportCanRemove,
    folderImportCanRescan,
    folderImportCancelling,
    folderImportCompletion,
    folderImportCompletionLoading,
    folderImportHistory,
    folderImportHistoryError,
    folderImportHistoryLoading,
    folderImportHistoryRows,
    folderImportProblemCount,
    folderImportProgressPercent,
    folderImportQuestionRows,
    folderImportRemoving,
    folderImportReviewLoading,
    folderImportReviewReasonRows,
    folderImportReviewRows,
    folderImportReviewTruncated,
    folderImportStatusLabel,
    folderImportTerminal,
    folderImportTypeRows,
    folderPicking,
    folderRemoveConfirmMessage,
    folderRemoveConfirmOpen,
    folderRescan,
    folderRescanImportStarting,
    folderRescanStatusRows,
    folderRescanning,
    folderReviewLocked,
    folderRows,
    folderSamplePicking,
    folderScan,
    folderScanning,
    folderScanningLabel,
    folderSelection,
    folderSelectionLoading,
    folderSourceIsArchive,
    handleFolderPrimaryAction,
    humanizeReason: humanizeSourceImportReason,
    importDuplicateContent,
    includeHiddenInFolderScan,
    isExcludedExtension,
    isExcludedFile,
    isExcludedFolder,
    loadFolderImportHistory,
    openFolderImportHistoryItem,
    requestRemoveFolderImport,
    resetFolderMode,
    resetFolderReview,
    rescanFolderImport,
    scanFolderSource,
    skipRows,
    startFolderImport,
    startFolderRescanImport,
    toggleExtensionExclusion,
    toggleFileExclusion,
    toggleFolderExclusion,
    folderIssueActionHint,
  }
}
