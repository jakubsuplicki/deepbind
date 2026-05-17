import { flushPromises } from '@vue/test-utils'
import { beforeEach, describe, it, expect, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import ImportDialog from '~/components/ImportDialog.vue'

const sourceImportMocks = vi.hoisted(() => ({
  cancelImport: vi.fn(),
  createSelection: vi.fn(),
  getImport: vi.fn(),
  getImportCompletion: vi.fn(),
  getImportReview: vi.fn(),
  pickFolderSource: vi.fn(),
  pickSampleDataset: vi.fn(),
  removeImport: vi.fn(),
  rescanImport: vi.fn(),
  scanSource: vi.fn(),
  startImport: vi.fn(),
}))

vi.mock('~/composables/useSourceImport', () => ({
  useSourceImport: () => sourceImportMocks,
}))

describe('components/ImportDialog.vue', () => {
  beforeEach(() => {
    sourceImportMocks.cancelImport.mockReset()
    sourceImportMocks.createSelection.mockReset()
    sourceImportMocks.getImport.mockReset()
    sourceImportMocks.getImportCompletion.mockReset()
    sourceImportMocks.getImportReview.mockReset()
    sourceImportMocks.pickFolderSource.mockReset()
    sourceImportMocks.pickSampleDataset.mockReset()
    sourceImportMocks.removeImport.mockReset()
    sourceImportMocks.rescanImport.mockReset()
    sourceImportMocks.scanSource.mockReset()
    sourceImportMocks.startImport.mockReset()
  })

  it('renders when visible=true', async () => {
    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    expect(wrapper.find('.import-dialog').exists()).toBe(true)
    expect(wrapper.find('.import-dialog__title').text()).toBe('Import')
  })

  it('does not render when visible=false', async () => {
    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: false },
    })
    expect(wrapper.find('.import-dialog').exists()).toBe(false)
  })

  it('file input accepts supported document formats', async () => {
    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    const input = wrapper.find('.import-dialog__file-input')
    expect(input.attributes('accept')).toContain('.docx')
    expect(input.attributes('accept')).toContain('.xlsx')
    expect(input.attributes('accept')).toContain('.eml')
  })

  it('file input allows multiple in generic mode', async () => {
    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    const input = wrapper.find('.import-dialog__file-input')
    // Generic mode is default; multiple bound to (mode === 'generic').
    expect(input.attributes('multiple')).toBeDefined()
  })

  it('dropzone is visible', async () => {
    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    expect(wrapper.find('.import-dialog__dropzone').exists()).toBe(true)
  })

  it('folder mode shows source picker and hides file dropzone', async () => {
    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    await wrapper.findAll('.import-dialog__mode-btn')[1]!.trigger('click')
    expect(wrapper.find('.import-dialog__source').exists()).toBe(true)
    expect(wrapper.find('.import-dialog__dropzone').exists()).toBe(false)
    expect(wrapper.find('.import-dialog__hint').text()).toContain('File contents are imported only after you approve')
  })

  it('folder mode can select the bundled sample folder before scanning', async () => {
    sourceImportMocks.pickSampleDataset.mockResolvedValue({
      source_token: 'sample-source-token',
      source_kind: 'local_folder',
      display_name: 'deepfiles-demo-folder',
      root_path: '/Applications/DeepFilesAI.app/Contents/Resources/sample-data/deepfiles-demo-folder',
      expires_at: '2026-05-16T00:00:00Z',
    })
    sourceImportMocks.scanSource.mockResolvedValue({
      scan_id: 'scan_sample',
      source_kind: 'local_folder',
      source_display_name: 'deepfiles-demo-folder',
      source_root_path: '/Applications/DeepFilesAI.app/Contents/Resources/sample-data/deepfiles-demo-folder',
      proposed_destination_root: 'memory/imports/deepfiles-demo-folder/',
      total_files_seen: 7,
      total_size_seen: 1024,
      supported_file_count: 7,
      unsupported_file_count: 0,
      skipped_file_count: 0,
      skipped_by_reason: {},
      counts_by_extension: { '.md': 3, '.csv': 1, '.html': 2, '.eml': 1 },
      largest_files: [],
      folder_summary: [],
      files: [],
      file_list_truncated: false,
      limit_hit: false,
      created_at: '2026-05-16T00:00:01Z',
    })
    sourceImportMocks.createSelection.mockResolvedValue({
      selection_id: 'sel_sample',
      scan_id: 'scan_sample',
      source_display_name: 'deepfiles-demo-folder',
      proposed_destination_root: 'memory/imports/deepfiles-demo-folder/',
      approved_file_count: 7,
      approved_total_size: 1024,
      excluded_file_count: 0,
      excluded_total_size: 0,
      unsupported_file_count: 0,
      skipped_file_count: 0,
      excluded_by_rule: {},
      approved_files: [],
      approved_file_list_truncated: false,
      created_at: '2026-05-16T00:00:02Z',
    })

    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    await wrapper.findAll('.import-dialog__mode-btn')[1]!.trigger('click')
    await wrapper.find('.import-dialog__sample-btn').trigger('click')
    await flushPromises()

    expect(sourceImportMocks.pickSampleDataset).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('deepfiles-demo-folder')
    expect(wrapper.text()).toContain('Sample folder selected')

    await wrapper.find('.import-dialog__import-btn').trigger('click')
    await flushPromises()

    expect(sourceImportMocks.scanSource).toHaveBeenCalledWith('sample-source-token', {
      includeHidden: false,
    })
    expect(wrapper.text()).toContain('Scan ready')
  })

  it('folder scan review lets users exclude file types', async () => {
    sourceImportMocks.pickFolderSource.mockResolvedValue({
      source_token: 'source-token',
      source_kind: 'local_folder',
      display_name: 'Client A',
      root_path: '/Users/me/Client A',
      expires_at: '2026-05-16T00:00:00Z',
    })
    sourceImportMocks.scanSource.mockResolvedValue({
      scan_id: 'scan_123',
      source_kind: 'local_folder',
      source_display_name: 'Client A',
      source_root_path: '/Users/me/Client A',
      proposed_destination_root: 'memory/imports/client-a/',
      total_files_seen: 2,
      total_size_seen: 30,
      supported_file_count: 2,
      unsupported_file_count: 0,
      skipped_file_count: 0,
      skipped_by_reason: {},
      counts_by_extension: { '.md': 1, '.txt': 1 },
      largest_files: [],
      folder_summary: [{ relpath: 'Docs', file_count: 1, total_size: 10 }],
      files: [
        {
          id: 'brief-md',
          relpath: 'brief.md',
          filename: 'brief.md',
          extension: '.md',
          size: 20,
          modified_at: null,
          status: 'supported',
          reason: null,
        },
        {
          id: 'notes-txt',
          relpath: 'Docs/notes.txt',
          filename: 'notes.txt',
          extension: '.txt',
          size: 10,
          modified_at: null,
          status: 'supported',
          reason: null,
        },
      ],
      file_list_truncated: false,
      limit_hit: false,
      created_at: '2026-05-16T00:00:00Z',
    })
    sourceImportMocks.createSelection
      .mockResolvedValueOnce({
        selection_id: 'sel_1',
        scan_id: 'scan_123',
        source_display_name: 'Client A',
        proposed_destination_root: 'memory/imports/client-a/',
        approved_file_count: 2,
        approved_total_size: 30,
        excluded_file_count: 0,
        excluded_total_size: 0,
        unsupported_file_count: 0,
        skipped_file_count: 0,
        excluded_by_rule: {},
        approved_files: [],
        approved_file_list_truncated: false,
        created_at: '2026-05-16T00:00:01Z',
      })
      .mockResolvedValueOnce({
        selection_id: 'sel_2',
        scan_id: 'scan_123',
        source_display_name: 'Client A',
        proposed_destination_root: 'memory/imports/client-a/',
        approved_file_count: 1,
        approved_total_size: 20,
        excluded_file_count: 1,
        excluded_total_size: 10,
        unsupported_file_count: 0,
        skipped_file_count: 0,
        excluded_by_rule: { file_type: 1 },
        approved_files: [],
        approved_file_list_truncated: false,
        created_at: '2026-05-16T00:00:02Z',
      })

    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    await wrapper.findAll('.import-dialog__mode-btn')[1]!.trigger('click')
    await wrapper.find('.import-dialog__browse-btn').trigger('click')
    await flushPromises()
    await wrapper.find('.import-dialog__import-btn').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Selected for import')
    expect(wrapper.text()).toContain('2')

    const txtChip = wrapper
      .findAll('.import-dialog__chip-btn')
      .find(button => button.text().includes('.txt'))
    expect(txtChip).toBeTruthy()
    await txtChip!.trigger('click')
    await flushPromises()

    expect(sourceImportMocks.createSelection).toHaveBeenLastCalledWith('scan_123', {
      excludedFileIds: [],
      excludedExtensions: ['.txt'],
      excludedFolders: [],
    })
    expect(wrapper.text()).toContain('Excluded by review')
  })

  it('folder mode starts approved import after review', async () => {
    sourceImportMocks.pickFolderSource.mockResolvedValue({
      source_token: 'source-token',
      source_kind: 'local_folder',
      display_name: 'Client A',
      root_path: '/Users/me/Client A',
      expires_at: '2026-05-16T00:00:00Z',
    })
    sourceImportMocks.scanSource.mockResolvedValue({
      scan_id: 'scan_123',
      source_kind: 'local_folder',
      source_display_name: 'Client A',
      source_root_path: '/Users/me/Client A',
      proposed_destination_root: 'memory/imports/client-a/',
      total_files_seen: 1,
      total_size_seen: 20,
      supported_file_count: 1,
      unsupported_file_count: 0,
      skipped_file_count: 0,
      skipped_by_reason: {},
      counts_by_extension: { '.md': 1 },
      largest_files: [],
      folder_summary: [],
      files: [{
        id: 'brief-md',
        relpath: 'brief.md',
        filename: 'brief.md',
        extension: '.md',
        size: 20,
        modified_at: null,
        status: 'supported',
        reason: null,
      }],
      file_list_truncated: false,
      limit_hit: false,
      created_at: '2026-05-16T00:00:00Z',
    })
    sourceImportMocks.createSelection.mockResolvedValue({
      selection_id: 'sel_1',
      scan_id: 'scan_123',
      source_display_name: 'Client A',
      proposed_destination_root: 'memory/imports/client-a/',
      approved_file_count: 1,
      approved_total_size: 20,
      excluded_file_count: 0,
      excluded_total_size: 0,
      unsupported_file_count: 0,
      skipped_file_count: 0,
      excluded_by_rule: {},
      approved_files: [],
      approved_file_list_truncated: false,
      created_at: '2026-05-16T00:00:01Z',
    })
    sourceImportMocks.startImport.mockResolvedValue({
      batch_id: 'import_1',
      scan_id: 'scan_123',
      selection_id: 'sel_1',
      source_kind: 'local_folder',
      source_display_name: 'Client A',
      destination_root: 'memory/imports/client-a-import1/',
      state: 'completed',
      total_file_count: 1,
      imported_file_count: 1,
      skipped_file_count: 0,
      failed_file_count: 0,
      created_note_count: 1,
      total_bytes: 20,
      processed_bytes: 20,
      current_file: null,
      files: [],
      started_at: '2026-05-16T00:00:02Z',
      updated_at: '2026-05-16T00:00:03Z',
      finished_at: '2026-05-16T00:00:03Z',
    })
    sourceImportMocks.getImportCompletion.mockResolvedValue({
      batch_id: 'import_1',
      source_display_name: 'Client A',
      state: 'completed',
      destination_root: 'memory/imports/client-a-import1/',
      total_file_count: 1,
      imported_file_count: 1,
      skipped_file_count: 0,
      failed_file_count: 0,
      duplicate_file_count: 0,
      created_note_count: 1,
      imported_extension_counts: { '.md': 1 },
      imported_folder_counts: { '.': 1 },
      suggested_questions: [
        {
          question: 'Which files should I review first?',
          reason: 'general',
        },
      ],
      can_ask_about_import: true,
      updated_at: '2026-05-16T00:00:03Z',
    })

    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    await wrapper.findAll('.import-dialog__mode-btn')[1]!.trigger('click')
    await wrapper.find('.import-dialog__browse-btn').trigger('click')
    await flushPromises()
    await wrapper.find('.import-dialog__import-btn').trigger('click')
    await flushPromises()
    await wrapper.find('.import-dialog__import-btn').trigger('click')
    await flushPromises()

    expect(sourceImportMocks.startImport).toHaveBeenCalledWith('scan_123', 'sel_1')
    expect(wrapper.text()).toContain('Imported 1 files and created 1 notes')
    await flushPromises()
    expect(sourceImportMocks.getImportCompletion).toHaveBeenCalledWith('import_1')
    expect(wrapper.text()).toContain('Ready to ask')
    expect(wrapper.text()).toContain('Which files should I review first?')
  })

  it('folder mode shows skipped and failed files after import', async () => {
    sourceImportMocks.pickFolderSource.mockResolvedValue({
      source_token: 'source-token',
      source_kind: 'local_folder',
      display_name: 'Client A',
      root_path: '/Users/me/Client A',
      expires_at: '2026-05-16T00:00:00Z',
    })
    sourceImportMocks.scanSource.mockResolvedValue({
      scan_id: 'scan_123',
      source_kind: 'local_folder',
      source_display_name: 'Client A',
      source_root_path: '/Users/me/Client A',
      proposed_destination_root: 'memory/imports/client-a/',
      total_files_seen: 3,
      total_size_seen: 60,
      supported_file_count: 3,
      unsupported_file_count: 0,
      skipped_file_count: 0,
      skipped_by_reason: {},
      counts_by_extension: { '.md': 3 },
      largest_files: [],
      folder_summary: [],
      files: [
        {
          id: 'keep-md',
          relpath: 'keep.md',
          filename: 'keep.md',
          extension: '.md',
          size: 20,
          modified_at: null,
          status: 'supported',
          reason: null,
        },
      ],
      file_list_truncated: false,
      limit_hit: false,
      created_at: '2026-05-16T00:00:00Z',
    })
    sourceImportMocks.createSelection.mockResolvedValue({
      selection_id: 'sel_1',
      scan_id: 'scan_123',
      source_display_name: 'Client A',
      proposed_destination_root: 'memory/imports/client-a/',
      approved_file_count: 3,
      approved_total_size: 60,
      excluded_file_count: 0,
      excluded_total_size: 0,
      unsupported_file_count: 0,
      skipped_file_count: 0,
      excluded_by_rule: {},
      approved_files: [],
      approved_file_list_truncated: false,
      created_at: '2026-05-16T00:00:01Z',
    })
    sourceImportMocks.startImport.mockResolvedValue({
      batch_id: 'import_1',
      scan_id: 'scan_123',
      selection_id: 'sel_1',
      source_kind: 'local_folder',
      source_display_name: 'Client A',
      destination_root: 'memory/imports/client-a-import1/',
      state: 'completed',
      total_file_count: 3,
      imported_file_count: 1,
      skipped_file_count: 1,
      failed_file_count: 1,
      created_note_count: 1,
      total_bytes: 60,
      processed_bytes: 60,
      current_file: null,
      files: [],
      started_at: '2026-05-16T00:00:02Z',
      updated_at: '2026-05-16T00:00:03Z',
      finished_at: '2026-05-16T00:00:03Z',
    })
    sourceImportMocks.getImportReview.mockResolvedValue({
      batch_id: 'import_1',
      source_display_name: 'Client A',
      state: 'completed',
      skipped_file_count: 1,
      failed_file_count: 1,
      problem_file_count: 2,
      reason_counts: {
        duplicate_content: 1,
        password_protected: 1,
      },
      files: [
        {
          file_id: 'locked-md',
          relpath: 'locked.md',
          filename: 'locked.md',
          extension: '.md',
          size: 20,
          modified_at: null,
          status: 'failed',
          stage: 'failed',
          reason: 'password_protected',
          duplicate_of: null,
          note_paths: [],
          can_retry: true,
          can_fix_locally: true,
        },
        {
          file_id: 'copy-md',
          relpath: 'copy.md',
          filename: 'copy.md',
          extension: '.md',
          size: 20,
          modified_at: null,
          status: 'skipped',
          stage: 'done',
          reason: 'duplicate_content',
          duplicate_of: 'keep.md',
          note_paths: [],
          can_retry: false,
          can_fix_locally: false,
        },
      ],
      file_list_truncated: false,
      updated_at: '2026-05-16T00:00:03Z',
    })

    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    await wrapper.findAll('.import-dialog__mode-btn')[1]!.trigger('click')
    await wrapper.find('.import-dialog__browse-btn').trigger('click')
    await flushPromises()
    await wrapper.find('.import-dialog__import-btn').trigger('click')
    await flushPromises()
    await wrapper.find('.import-dialog__import-btn').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(sourceImportMocks.getImportReview).toHaveBeenCalledWith('import_1', 100)
    expect(wrapper.text()).toContain('Skipped and failed files')
    expect(wrapper.text()).toContain('locked.md')
    expect(wrapper.text()).toContain('Export an unlocked copy, then import it.')
    expect(wrapper.text()).toContain('copy.md')
    expect(wrapper.text()).toContain('Already imported from keep.md.')
  })

  it('folder mode can remove a completed import', async () => {
    sourceImportMocks.pickFolderSource.mockResolvedValue({
      source_token: 'source-token',
      source_kind: 'local_folder',
      display_name: 'Client A',
      root_path: '/Users/me/Client A',
      expires_at: '2026-05-16T00:00:00Z',
    })
    sourceImportMocks.scanSource.mockResolvedValue({
      scan_id: 'scan_123',
      source_kind: 'local_folder',
      source_display_name: 'Client A',
      source_root_path: '/Users/me/Client A',
      proposed_destination_root: 'memory/imports/client-a/',
      total_files_seen: 1,
      total_size_seen: 20,
      supported_file_count: 1,
      unsupported_file_count: 0,
      skipped_file_count: 0,
      skipped_by_reason: {},
      counts_by_extension: { '.md': 1 },
      largest_files: [],
      folder_summary: [],
      files: [{
        id: 'brief-md',
        relpath: 'brief.md',
        filename: 'brief.md',
        extension: '.md',
        size: 20,
        modified_at: null,
        status: 'supported',
        reason: null,
      }],
      file_list_truncated: false,
      limit_hit: false,
      created_at: '2026-05-16T00:00:00Z',
    })
    sourceImportMocks.createSelection.mockResolvedValue({
      selection_id: 'sel_1',
      scan_id: 'scan_123',
      source_display_name: 'Client A',
      proposed_destination_root: 'memory/imports/client-a/',
      approved_file_count: 1,
      approved_total_size: 20,
      excluded_file_count: 0,
      excluded_total_size: 0,
      unsupported_file_count: 0,
      skipped_file_count: 0,
      excluded_by_rule: {},
      approved_files: [],
      approved_file_list_truncated: false,
      created_at: '2026-05-16T00:00:01Z',
    })
    sourceImportMocks.startImport.mockResolvedValue({
      batch_id: 'import_1',
      scan_id: 'scan_123',
      selection_id: 'sel_1',
      source_kind: 'local_folder',
      source_display_name: 'Client A',
      destination_root: 'memory/imports/client-a-import1/',
      state: 'completed',
      total_file_count: 1,
      imported_file_count: 1,
      skipped_file_count: 0,
      failed_file_count: 0,
      created_note_count: 1,
      total_bytes: 20,
      processed_bytes: 20,
      current_file: null,
      files: [],
      started_at: '2026-05-16T00:00:02Z',
      updated_at: '2026-05-16T00:00:03Z',
      finished_at: '2026-05-16T00:00:03Z',
    })
    sourceImportMocks.removeImport.mockResolvedValue({
      batch_id: 'import_1',
      scan_id: 'scan_123',
      selection_id: 'sel_1',
      source_kind: 'local_folder',
      source_display_name: 'Client A',
      destination_root: 'memory/imports/client-a-import1/',
      state: 'removed',
      total_file_count: 1,
      imported_file_count: 1,
      skipped_file_count: 0,
      failed_file_count: 0,
      created_note_count: 1,
      total_bytes: 20,
      processed_bytes: 20,
      current_file: null,
      files: [],
      started_at: '2026-05-16T00:00:02Z',
      updated_at: '2026-05-16T00:00:04Z',
      finished_at: '2026-05-16T00:00:04Z',
    })

    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    await wrapper.findAll('.import-dialog__mode-btn')[1]!.trigger('click')
    await wrapper.find('.import-dialog__browse-btn').trigger('click')
    await flushPromises()
    await wrapper.find('.import-dialog__import-btn').trigger('click')
    await flushPromises()
    await wrapper.find('.import-dialog__import-btn').trigger('click')
    await flushPromises()

    await wrapper.find('.import-dialog__remove-btn').trigger('click')
    await flushPromises()
    const confirmButton = document.body.querySelector(
      '.confirm-dialog__btn--confirm'
    ) as HTMLButtonElement | null
    expect(confirmButton).toBeTruthy()
    confirmButton!.click()
    await flushPromises()

    expect(sourceImportMocks.removeImport).toHaveBeenCalledWith('import_1', 'import_1')
    expect(wrapper.text()).toContain('Removed import: 1 created notes moved out of memory')
  })

  it('folder mode rescans a completed import and imports only changes', async () => {
    sourceImportMocks.pickFolderSource.mockResolvedValue({
      source_token: 'source-token',
      source_kind: 'local_folder',
      display_name: 'Client A',
      root_path: '/Users/me/Client A',
      expires_at: '2026-05-16T00:00:00Z',
    })
    sourceImportMocks.scanSource.mockResolvedValue({
      scan_id: 'scan_123',
      source_kind: 'local_folder',
      source_display_name: 'Client A',
      source_root_path: '/Users/me/Client A',
      proposed_destination_root: 'memory/imports/client-a/',
      total_files_seen: 1,
      total_size_seen: 20,
      supported_file_count: 1,
      unsupported_file_count: 0,
      skipped_file_count: 0,
      skipped_by_reason: {},
      counts_by_extension: { '.md': 1 },
      largest_files: [],
      folder_summary: [],
      files: [{
        id: 'brief-md',
        relpath: 'brief.md',
        filename: 'brief.md',
        extension: '.md',
        size: 20,
        modified_at: null,
        status: 'supported',
        reason: null,
      }],
      file_list_truncated: false,
      limit_hit: false,
      created_at: '2026-05-16T00:00:00Z',
    })
    sourceImportMocks.createSelection
      .mockResolvedValueOnce({
        selection_id: 'sel_1',
        scan_id: 'scan_123',
        source_display_name: 'Client A',
        proposed_destination_root: 'memory/imports/client-a/',
        approved_file_count: 1,
        approved_total_size: 20,
        excluded_file_count: 0,
        excluded_total_size: 0,
        unsupported_file_count: 0,
        skipped_file_count: 0,
        excluded_by_rule: {},
        approved_files: [],
        approved_file_list_truncated: false,
        created_at: '2026-05-16T00:00:01Z',
      })
      .mockResolvedValueOnce({
        selection_id: 'sel_rescan',
        scan_id: 'scan_rescan',
        source_display_name: 'Client A',
        proposed_destination_root: 'memory/imports/client-a/',
        approved_file_count: 2,
        approved_total_size: 64,
        excluded_file_count: 0,
        excluded_total_size: 0,
        unsupported_file_count: 0,
        skipped_file_count: 0,
        excluded_by_rule: {},
        approved_files: [],
        approved_file_list_truncated: false,
        created_at: '2026-05-16T00:00:05Z',
      })
    sourceImportMocks.startImport
      .mockResolvedValueOnce({
        batch_id: 'import_1',
        scan_id: 'scan_123',
        selection_id: 'sel_1',
        source_kind: 'local_folder',
        source_display_name: 'Client A',
        destination_root: 'memory/imports/client-a-import1/',
        state: 'completed',
        total_file_count: 1,
        imported_file_count: 1,
        skipped_file_count: 0,
        failed_file_count: 0,
        created_note_count: 1,
        total_bytes: 20,
        processed_bytes: 20,
        current_file: null,
        files: [],
        started_at: '2026-05-16T00:00:02Z',
        updated_at: '2026-05-16T00:00:03Z',
        finished_at: '2026-05-16T00:00:03Z',
      })
      .mockResolvedValueOnce({
        batch_id: 'import_2',
        scan_id: 'scan_rescan',
        selection_id: 'sel_rescan',
        source_kind: 'local_folder',
        source_display_name: 'Client A',
        destination_root: 'memory/imports/client-a-import2/',
        state: 'completed',
        total_file_count: 2,
        imported_file_count: 2,
        skipped_file_count: 0,
        failed_file_count: 0,
        created_note_count: 2,
        total_bytes: 64,
        processed_bytes: 64,
        current_file: null,
        files: [],
        started_at: '2026-05-16T00:00:06Z',
        updated_at: '2026-05-16T00:00:07Z',
        finished_at: '2026-05-16T00:00:07Z',
      })
    sourceImportMocks.rescanImport.mockResolvedValue({
      batch_id: 'import_1',
      scan_id: 'scan_rescan',
      source_kind: 'local_folder',
      source_display_name: 'Client A',
      proposed_destination_root: 'memory/imports/client-a/',
      total_files_seen: 4,
      current_supported_file_count: 3,
      unsupported_file_count: 0,
      skipped_file_count: 0,
      unchanged_file_count: 1,
      changed_file_count: 1,
      new_file_count: 1,
      missing_file_count: 1,
      importable_file_count: 2,
      importable_total_size: 64,
      skipped_by_reason: {},
      files: [
        {
          id: 'new-md',
          relpath: 'new.md',
          filename: 'new.md',
          extension: '.md',
          size: 24,
          modified_at: null,
          status: 'new',
          reason: null,
          previous_status: null,
          previous_size: null,
          previous_modified_at: null,
        },
        {
          id: 'brief-md',
          relpath: 'brief.md',
          filename: 'brief.md',
          extension: '.md',
          size: 40,
          modified_at: null,
          status: 'changed',
          reason: 'metadata_changed',
          previous_status: 'done',
          previous_size: 20,
          previous_modified_at: null,
        },
      ],
      file_list_truncated: false,
      created_at: '2026-05-16T00:00:04Z',
    })

    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    await wrapper.findAll('.import-dialog__mode-btn')[1]!.trigger('click')
    await wrapper.find('.import-dialog__browse-btn').trigger('click')
    await flushPromises()
    await wrapper.find('.import-dialog__import-btn').trigger('click')
    await flushPromises()
    await wrapper.find('.import-dialog__import-btn').trigger('click')
    await flushPromises()

    await wrapper.find('.import-dialog__rescan-btn').trigger('click')
    await flushPromises()

    expect(sourceImportMocks.rescanImport).toHaveBeenCalledWith('import_1')
    expect(wrapper.text()).toContain('Since last import')
    expect(wrapper.text()).toContain('Scan again ready: 1 new and 1 changed files')

    await wrapper.find('.import-dialog__change-btn').trigger('click')
    await flushPromises()

    expect(sourceImportMocks.createSelection).toHaveBeenLastCalledWith('scan_rescan', {})
    expect(sourceImportMocks.startImport).toHaveBeenLastCalledWith('scan_rescan', 'sel_rescan')
    expect(wrapper.text()).toContain('Imported 2 files and created 2 notes')
  })

  it('folder mode can cancel an active import', async () => {
    sourceImportMocks.pickFolderSource.mockResolvedValue({
      source_token: 'source-token',
      source_kind: 'local_folder',
      display_name: 'Client A',
      root_path: '/Users/me/Client A',
      expires_at: '2026-05-16T00:00:00Z',
    })
    sourceImportMocks.scanSource.mockResolvedValue({
      scan_id: 'scan_123',
      source_kind: 'local_folder',
      source_display_name: 'Client A',
      source_root_path: '/Users/me/Client A',
      proposed_destination_root: 'memory/imports/client-a/',
      total_files_seen: 1,
      total_size_seen: 20,
      supported_file_count: 1,
      unsupported_file_count: 0,
      skipped_file_count: 0,
      skipped_by_reason: {},
      counts_by_extension: { '.md': 1 },
      largest_files: [],
      folder_summary: [],
      files: [{
        id: 'brief-md',
        relpath: 'brief.md',
        filename: 'brief.md',
        extension: '.md',
        size: 20,
        modified_at: null,
        status: 'supported',
        reason: null,
      }],
      file_list_truncated: false,
      limit_hit: false,
      created_at: '2026-05-16T00:00:00Z',
    })
    sourceImportMocks.createSelection.mockResolvedValue({
      selection_id: 'sel_1',
      scan_id: 'scan_123',
      source_display_name: 'Client A',
      proposed_destination_root: 'memory/imports/client-a/',
      approved_file_count: 1,
      approved_total_size: 20,
      excluded_file_count: 0,
      excluded_total_size: 0,
      unsupported_file_count: 0,
      skipped_file_count: 0,
      excluded_by_rule: {},
      approved_files: [],
      approved_file_list_truncated: false,
      created_at: '2026-05-16T00:00:01Z',
    })
    sourceImportMocks.startImport.mockResolvedValue({
      batch_id: 'import_1',
      scan_id: 'scan_123',
      selection_id: 'sel_1',
      source_kind: 'local_folder',
      source_display_name: 'Client A',
      destination_root: 'memory/imports/client-a-import1/',
      state: 'importing',
      total_file_count: 1,
      imported_file_count: 0,
      skipped_file_count: 0,
      failed_file_count: 0,
      created_note_count: 0,
      total_bytes: 20,
      processed_bytes: 0,
      current_file: 'brief.md',
      files: [],
      started_at: '2026-05-16T00:00:02Z',
      updated_at: '2026-05-16T00:00:03Z',
      finished_at: null,
    })
    sourceImportMocks.cancelImport.mockResolvedValue({
      batch_id: 'import_1',
      scan_id: 'scan_123',
      selection_id: 'sel_1',
      source_kind: 'local_folder',
      source_display_name: 'Client A',
      destination_root: 'memory/imports/client-a-import1/',
      state: 'cancelling',
      total_file_count: 1,
      imported_file_count: 0,
      skipped_file_count: 0,
      failed_file_count: 0,
      created_note_count: 0,
      total_bytes: 20,
      processed_bytes: 0,
      current_file: 'brief.md',
      files: [],
      started_at: '2026-05-16T00:00:02Z',
      updated_at: '2026-05-16T00:00:04Z',
      finished_at: null,
    })

    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    await wrapper.findAll('.import-dialog__mode-btn')[1]!.trigger('click')
    await wrapper.find('.import-dialog__browse-btn').trigger('click')
    await flushPromises()
    await wrapper.find('.import-dialog__import-btn').trigger('click')
    await flushPromises()
    await wrapper.find('.import-dialog__import-btn').trigger('click')
    await flushPromises()

    await wrapper.find('.import-dialog__stop-btn').trigger('click')
    await flushPromises()

    expect(sourceImportMocks.cancelImport).toHaveBeenCalledWith('import_1')
    expect(wrapper.text()).toContain('Cancelling import after the current file finishes')
    wrapper.unmount()
  })

  it('import button disabled when no file selected', async () => {
    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    const btn = wrapper.find('.import-dialog__import-btn')
    expect(btn.attributes('disabled')).toBeDefined()
  })

  it('has folder selector with default options', async () => {
    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    const select = wrapper.find('.import-dialog__select')
    expect(select.exists()).toBe(true)
    const options = select.findAll('option')
    const values = options.map(o => o.attributes('value'))
    expect(values).toContain('knowledge')
    expect(values).toContain('inbox')
  })

  it('cancel button emits close', async () => {
    const wrapper = await mountSuspended(ImportDialog, {
      props: { visible: true },
    })
    await wrapper.find('.import-dialog__cancel-btn').trigger('click')
    expect(wrapper.emitted('close')).toBeTruthy()
  })
})
