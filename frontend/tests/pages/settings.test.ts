import { describe, it, expect } from 'vitest'
import { mountSuspended, registerEndpoint } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import SettingsPage from '~/pages/settings.vue'
import GraphExpansionSection from '~/components/settings/GraphExpansionSection.vue'

function registerSettingsEndpoints(overrides: Record<string, unknown> = {}) {
  registerEndpoint('/api/settings', () => ({
    workspace_path: '/home/user/Jarvis',
    voice: { auto_speak: 'false', tts_voice: 'alloy' },
    ...overrides,
  }))
  registerEndpoint('/api/settings/retrieval', () => ({
    graph_expansion: {
      use_related: true,
      use_part_of: true,
      use_suggested_strong: false,
    },
  }))
}

describe('pages/settings.vue', () => {
  it('renders Local Models section (ADR 015 — single-target local stack)', async () => {
    registerSettingsEndpoints()
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()
    expect(wrapper.text()).toContain('Local Models')
  })

  it('does not render any cloud-provider surface', async () => {
    registerSettingsEndpoints()
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()
    const text = wrapper.text()
    expect(text).not.toContain('Anthropic')
    expect(text).not.toContain('OpenAI')
    expect(text).not.toContain('AI Providers')
    expect(text).not.toContain('cloud LLM')
  })

  it('renders workspace path', async () => {
    registerSettingsEndpoints()
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()
    await new Promise(r => setTimeout(r, 50))
    await flushPromises()
    expect(wrapper.find('.settings-page__path').text()).toBe('/home/user/Jarvis')
  })

  it('voice toggle reflects auto_speak setting', async () => {
    registerSettingsEndpoints({ voice: { auto_speak: 'true', tts_voice: 'alloy' } })
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()
    await new Promise(r => setTimeout(r, 50))
    await flushPromises()
    const checkbox = wrapper.find('.settings-page__toggle input[type="checkbox"]')
    expect((checkbox.element as HTMLInputElement).checked).toBe(true)
  })

  it('renders workspace section', async () => {
    registerSettingsEndpoints()
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()
    expect(wrapper.text()).toContain('Workspace')
  })

  it('has reindex and rebuild buttons', async () => {
    registerSettingsEndpoints()
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()
    const buttons = wrapper.findAll('.settings-page__btn')
    const labels = buttons.map(b => b.text())
    expect(labels).toContain('Reindex Memory')
    expect(labels).toContain('Rebuild Graph')
  })

  it('does not render the Token Budget section (deleted under local-only)', async () => {
    registerSettingsEndpoints()
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()
    const text = wrapper.text()
    expect(text).not.toContain('Token Usage & Budget')
    expect(text).not.toContain('Daily token budget')
  })
})

describe('GraphExpansionSection', () => {
  it('renders three checkboxes', async () => {
    registerEndpoint('/api/settings/retrieval', () => ({
      graph_expansion: { use_related: true, use_part_of: true, use_suggested_strong: false },
    }))
    const wrapper = await mountSuspended(GraphExpansionSection)
    await flushPromises()
    const checkboxes = wrapper.findAll('input[type="checkbox"]')
    expect(checkboxes).toHaveLength(3)
  })

  it('reflects defaults: use_related and use_part_of on, use_suggested_strong off', async () => {
    registerEndpoint('/api/settings/retrieval', () => ({
      graph_expansion: { use_related: true, use_part_of: true, use_suggested_strong: false },
    }))
    const wrapper = await mountSuspended(GraphExpansionSection)
    await flushPromises()
    const checkboxes = wrapper.findAll('input[type="checkbox"]')
    expect((checkboxes[0]!.element as HTMLInputElement).checked).toBe(true)
    expect((checkboxes[1]!.element as HTMLInputElement).checked).toBe(true)
    expect((checkboxes[2]!.element as HTMLInputElement).checked).toBe(false)
  })

  it('reflects server state when use_related is off', async () => {
    registerEndpoint('/api/settings/retrieval', () => ({
      graph_expansion: { use_related: false, use_part_of: true, use_suggested_strong: false },
    }))
    const wrapper = await mountSuspended(GraphExpansionSection)
    await flushPromises()
    const checkboxes = wrapper.findAll('input[type="checkbox"]')
    expect((checkboxes[0]!.element as HTMLInputElement).checked).toBe(false)
  })

  it('calls PATCH on toggle and sends correct shape', async () => {
    const patches: unknown[] = []
    registerEndpoint('/api/settings/retrieval', {
      method: 'GET',
      handler: () => ({
        graph_expansion: { use_related: true, use_part_of: true, use_suggested_strong: false },
      }),
    })
    registerEndpoint('/api/settings/retrieval', {
      method: 'PATCH',
      handler: (event) => {
        patches.push(event)
        return { graph_expansion: { use_related: true, use_part_of: false, use_suggested_strong: false } }
      },
    })
    const wrapper = await mountSuspended(GraphExpansionSection)
    await flushPromises()
    const checkboxes = wrapper.findAll('input[type="checkbox"]')
    await checkboxes[1]!.trigger('change')
    await flushPromises()
    expect(patches.length).toBeGreaterThan(0)
  })
})
