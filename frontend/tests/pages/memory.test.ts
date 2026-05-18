import { describe, it, expect } from 'vitest'
import { mountSuspended, registerEndpoint } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import MemoryPage from '~/pages/memory.vue'

const MOCK_NOTES = [
  {
    path: 'inbox/hello.md',
    title: 'Hello World',
    folder: 'inbox',
    tags: ['test'],
    // More recent date → sorts first after sortNoteTreeByRecency
    updated_at: '2026-01-02T00:00:00',
    word_count: 42,
  },
  {
    path: 'projects/jarvis.md',
    title: 'Jarvis Project',
    folder: 'projects',
    tags: ['project', 'ai'],
    updated_at: '2026-01-01T00:00:00',
    word_count: 120,
  },
]

const MOCK_DETAIL = {
  path: 'inbox/hello.md',
  title: 'Hello World',
  content: '---\ntitle: Hello World\ntags: [test]\n---\n\nHello content.',
  frontmatter: { title: 'Hello World', tags: ['test'] },
  updated_at: '2026-01-01T00:00:00',
}

const PROJECT_DETAIL = {
  path: 'projects/jarvis.md',
  title: 'Jarvis Project',
  content: '---\ntitle: Jarvis Project\ntags: [project]\n---\n\nProject content.',
  frontmatter: {
    title: 'Jarvis Project',
    tags: ['project'],
    suggested_related: [
      { path: 'inbox/hello.md', confidence: 0.91, methods: ['bm25'], tier: 'strong' },
    ],
  },
  updated_at: '2026-01-01T00:00:00',
}

const IMPORTED_NOTES = [
  {
    path: 'imports-client-a/brief.md',
    title: 'Client Brief',
    folder: 'imports-client-a',
    tags: ['imported'],
    updated_at: '2026-05-18T00:00:00',
    word_count: 88,
  },
]

const IMPORTED_DETAIL = {
  path: 'imports-client-a/brief.md',
  title: 'Client Brief',
  content: '---\ntitle: Client Brief\ntags: [imported]\n---\n\nImported content.',
  frontmatter: { title: 'Client Brief', tags: ['imported'] },
  updated_at: '2026-05-18T00:00:00',
}

function registerNotesEndpoints(notes = MOCK_NOTES) {
  registerEndpoint('/api/memory/notes', () => notes)
  // Path is URL-encoded when fetched via encodeURIComponent(path)
  registerEndpoint('/api/memory/notes/inbox%2Fhello.md', () => MOCK_DETAIL)
  registerEndpoint('/api/memory/notes/projects%2Fjarvis.md', () => PROJECT_DETAIL)
  // Stub coverage so SmartConnectStatus doesn't throw an unhandled 404
  registerEndpoint('/api/connections/coverage', () => ({
    notes_total: 2, notes_with_suggestions: 2, notes_pending: 0,
    sections_total: 0, sections_with_suggestions: 0, sections_pending: 0,
    documents_pending: 0, active_section_jobs: [],
  }))
}

describe('pages/memory.vue', () => {
  it('renders folder buttons from API data', async () => {
    registerNotesEndpoints()
    const wrapper = await mountSuspended(MemoryPage)
    await flushPromises()
    const buttons = wrapper.findAll('.note-list__folder-btn')
    expect(buttons.length).toBeGreaterThanOrEqual(2)
    const text = buttons.map((b) => b.text())
    expect(text).toContain('inbox')
    expect(text).toContain('projects')
  })

  it('shows empty state message when no notes', async () => {
    registerNotesEndpoints([])
    const wrapper = await mountSuspended(MemoryPage)
    await flushPromises()
    expect(wrapper.find('.note-list__empty').text()).toBe('No notes yet')
  })

  it('clicking folder toggles active state', async () => {
    registerNotesEndpoints()
    const wrapper = await mountSuspended(MemoryPage)
    await flushPromises()
    const btn = wrapper.findAll('.note-list__folder-btn').find((b) => b.text() === 'inbox')
    expect(btn).toBeDefined()
    await btn!.trigger('click')
    await flushPromises()
    expect(btn!.classes()).toContain('note-list__folder-btn--active')
  })

  it('clicking note shows content in preview', async () => {
    registerNotesEndpoints()
    const wrapper = await mountSuspended(MemoryPage)
    await flushPromises()
    const item = wrapper.find('.note-list__item')
    expect(item.exists()).toBe(true)
    await item.trigger('click')
    await flushPromises()
    expect(wrapper.find('.note-viewer__title').text()).toBe('Hello World')
  })

  it('search input triggers API search on Enter', async () => {
    registerNotesEndpoints()
    const wrapper = await mountSuspended(MemoryPage)
    await flushPromises()
    const input = wrapper.find<HTMLInputElement>('.note-list__search-input')
    await input.setValue('python')
    await input.trigger('keydown.enter')
    expect(input.element.value).toBe('python')
  })

  it('search results replace folder view', async () => {
    registerNotesEndpoints()
    const wrapper = await mountSuspended(MemoryPage)
    await flushPromises()
    const input = wrapper.find('.note-list__search-input')
    await input.setValue('jarvis')
    await input.trigger('keydown.enter')
    await flushPromises()
    const active = wrapper.findAll('.note-list__folder-btn--active')
    expect(active.length).toBe(0)
  })

  it('clear search restores folder view', async () => {
    registerNotesEndpoints()
    const wrapper = await mountSuspended(MemoryPage)
    await flushPromises()
    const input = wrapper.find<HTMLInputElement>('.note-list__search-input')
    await input.setValue('test')
    await input.trigger('keydown.enter')
    await flushPromises()
    const clearBtn = wrapper.find('.note-list__clear')
    expect(clearBtn.exists()).toBe(true)
    await clearBtn.trigger('click')
    expect(input.element.value).toBe('')
  })

  it('select note empty state shows placeholder', async () => {
    registerNotesEndpoints()
    const wrapper = await mountSuspended(MemoryPage)
    await flushPromises()
    expect(wrapper.find('.note-viewer__empty').text()).toBe('Select a note to view')
  })

  it('opens imported notes from folder import completion', async () => {
    registerNotesEndpoints(IMPORTED_NOTES)
    registerEndpoint('/api/memory/notes/imports-client-a%2Fbrief.md', () => IMPORTED_DETAIL)
    const wrapper = await mountSuspended(MemoryPage, {
      global: {
        stubs: {
          ImportDialog: {
            template: `
              <button
                class="test-view-imported-notes"
                type="button"
                @click="$emit('view-notes', ['memory/imports-client-a/brief.md'])"
              >
                View imported notes
              </button>
            `,
          },
        },
      },
    })
    await flushPromises()

    await wrapper.find('.test-view-imported-notes').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.note-viewer__title').text()).toBe('Client Brief')
    expect(wrapper.find('.note-list__item--active').text()).toContain('Client Brief')
    expect(wrapper.find('.note-list__folder-btn--active').text()).toBe('imports-client-a')
  })

  it('opens the first pending Smart Connect note from the bulk review banner', async () => {
    registerNotesEndpoints()
    registerEndpoint('/api/connections/coverage', () => ({
      notes_total: 2,
      notes_with_suggestions: 2,
      notes_pending: 0,
      sections_total: 0,
      sections_with_suggestions: 0,
      sections_pending: 0,
      sections_unprocessed: 0,
      sections_no_match: 0,
      documents_pending: 0,
      pending_strong_suggestions: 1,
      pending_strong_notes: 1,
      strong_threshold: 0.8,
      active_section_jobs: [],
      pending_note_paths: ['projects/jarvis.md'],
    }))

    const wrapper = await mountSuspended(MemoryPage)
    await flushPromises()

    const review = wrapper.findAll('button').find((button) => button.text() === 'Review')
    expect(review).toBeDefined()
    await review!.trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.find('.note-viewer__title').text()).toBe('Jarvis Project')
    expect(wrapper.find('.note-list__item--active').text()).toContain('Jarvis Project')
    expect(wrapper.find('.note-list__folder-btn--active').text()).toBe('projects')
  })
})
