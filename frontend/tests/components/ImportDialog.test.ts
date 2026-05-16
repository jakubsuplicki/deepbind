import { flushPromises } from '@vue/test-utils'
import { beforeEach, describe, it, expect, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import ImportDialog from '~/components/ImportDialog.vue'

const sourceImportMocks = vi.hoisted(() => ({
  createSelection: vi.fn(),
  getImport: vi.fn(),
  pickFolderSource: vi.fn(),
  scanSource: vi.fn(),
  startImport: vi.fn(),
}))

vi.mock('~/composables/useSourceImport', () => ({
  useSourceImport: () => sourceImportMocks,
}))

describe('components/ImportDialog.vue', () => {
  beforeEach(() => {
    sourceImportMocks.createSelection.mockReset()
    sourceImportMocks.getImport.mockReset()
    sourceImportMocks.pickFolderSource.mockReset()
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
