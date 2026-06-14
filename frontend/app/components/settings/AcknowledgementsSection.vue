<template>
  <SettingsSection
    id="acknowledgements"
    title="Acknowledgements"
    section-class="acknowledgements-section"
    icon="ph:scroll"
    icon-active="ph:scroll-fill"
    :default-open="false"
  >
    <p class="settings-page__hint">
      DeepBind bundles open-source software. The full attribution and
      license text for every bundled component is reproduced below.
    </p>

    <p v-if="loading" class="acknowledgements-section__status">
      Loading…
    </p>
    <p v-else-if="error" class="acknowledgements-section__status acknowledgements-section__status--error">
      {{ error }}
    </p>

    <article
      v-else
      class="acknowledgements-section__body markdown-body"
      v-html="rendered"
    />
  </SettingsSection>
</template>

<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import SettingsSection from '~/components/settings/SettingsSection.vue'

const source = ref('')
const loading = ref(true)
const error = ref('')

marked.setOptions({ breaks: true, gfm: true })

const rendered = computed(() => {
  if (!source.value) return ''
  const html = marked.parse(source.value, { async: false }) as string
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } })
})

onMounted(async () => {
  try {
    const resp = await fetch('/THIRD-PARTY-NOTICES.md')
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    source.value = await resp.text()
  } catch (e: unknown) {
    const err = e as Error
    error.value = `Could not load attribution document: ${err.message}`
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.acknowledgements-section__body {
  max-height: 60vh;
  overflow-y: auto;
  padding: 0.75rem 1rem;
  border: 1px solid var(--color-border, #2a2a2a);
  border-radius: 6px;
  background: var(--color-surface-subtle, rgba(0, 0, 0, 0.15));
  font-size: 0.9rem;
  line-height: 1.55;
}

.acknowledgements-section__body :deep(h1),
.acknowledgements-section__body :deep(h2),
.acknowledgements-section__body :deep(h3) {
  margin-top: 1.25rem;
  margin-bottom: 0.5rem;
  font-weight: 600;
}

.acknowledgements-section__body :deep(h1) { font-size: 1.15rem; }
.acknowledgements-section__body :deep(h2) { font-size: 1.05rem; }
.acknowledgements-section__body :deep(h3) { font-size: 0.95rem; }

.acknowledgements-section__body :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 0.75rem 0;
  font-size: 0.85rem;
}

.acknowledgements-section__body :deep(th),
.acknowledgements-section__body :deep(td) {
  padding: 0.4rem 0.6rem;
  text-align: left;
  border-bottom: 1px solid var(--color-border, #2a2a2a);
}

.acknowledgements-section__body :deep(th) {
  font-weight: 600;
  color: var(--color-muted, #888);
}

.acknowledgements-section__body :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.85em;
  padding: 0.1em 0.35em;
  background: rgba(127, 127, 127, 0.15);
  border-radius: 3px;
}

.acknowledgements-section__body :deep(pre) {
  padding: 0.75rem;
  margin: 0.75rem 0;
  background: rgba(0, 0, 0, 0.25);
  border-radius: 4px;
  overflow-x: auto;
  font-size: 0.78rem;
  line-height: 1.45;
}

.acknowledgements-section__body :deep(pre code) {
  background: transparent;
  padding: 0;
}

.acknowledgements-section__body :deep(a) {
  color: var(--color-link, #6ab0ff);
  text-decoration: none;
}

.acknowledgements-section__body :deep(a:hover) {
  text-decoration: underline;
}

.acknowledgements-section__body :deep(hr) {
  border: 0;
  border-top: 1px solid var(--color-border, #2a2a2a);
  margin: 1.5rem 0;
}

.acknowledgements-section__status {
  font-size: 0.875rem;
  color: var(--color-muted, #888);
  padding: 0.5rem 0;
}

.acknowledgements-section__status--error {
  color: var(--color-warning, #d99);
}
</style>
