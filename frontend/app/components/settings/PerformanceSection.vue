<template>
  <SettingsSection
    id="performance"
    title="Performance"
    section-class="performance-section"
    :default-open="false"
  >
    <template #suffix>
      <span class="performance-badge" :class="{ 'performance-badge--active': enabled }">
        {{ enabled ? '🪶 Lightweight mode active' : 'Auto' }}
      </span>
    </template>

    <p class="settings-page__hint">
      Tune how Jarvis handles memory pressure when running local models.
    </p>

    <div class="performance-row">
      <label class="performance-toggle">
        <input
          type="checkbox"
          :checked="enabled"
          :disabled="saving || !loaded"
          @change="onToggle(($event.target as HTMLInputElement).checked)"
        />
        <span class="performance-toggle__main">
          <strong>Lightweight mode</strong>
          <span class="performance-toggle__sub">
            Pin chat to the smallest installed model on your hardware tier — chat keeps
            working even when other apps are eating RAM. Auto-downgrade is bypassed:
            no mid-turn switching, no warning chatter.
          </span>
        </span>
      </label>
    </div>

    <p v-if="error" class="performance-error">{{ error }}</p>
  </SettingsSection>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import { useLightweightMode } from '~/composables/settings/useLightweightMode'
import { useSettingsStatus } from '~/composables/settings/useSettingsStatus'

const lightweight = useLightweightMode()
const status = useSettingsStatus()

const enabled = computed(() => lightweight.enabled.value)
const loaded = computed(() => lightweight.loaded.value)
const saving = computed(() => lightweight.saving.value)
const error = computed(() => lightweight.error.value)

async function onToggle(next: boolean) {
  await lightweight.set(next)
  if (!lightweight.error.value) {
    status.set(next ? 'Lightweight mode enabled' : 'Lightweight mode disabled')
  }
}

onMounted(lightweight.load)
</script>

<style scoped>
.performance-section :deep(.settings-section__header) {
  align-items: center;
}

.performance-badge {
  display: inline-flex;
  align-items: center;
  padding: 0.18rem 0.55rem;
  border-radius: 999px;
  font-size: 0.7rem;
  letter-spacing: 0.02em;
  background: var(--bg-base);
  border: 1px solid var(--border-default);
  color: var(--text-muted);
}

.performance-badge--active {
  background: rgba(52, 211, 153, 0.08);
  border-color: rgba(52, 211, 153, 0.3);
  color: #34d399;
}

.performance-row {
  margin-top: 0.5rem;
}

.performance-toggle {
  display: flex;
  align-items: flex-start;
  gap: 0.7rem;
  padding: 0.65rem 0.85rem;
  border-radius: 8px;
  background: var(--bg-base);
  border: 1px solid var(--border-subtle);
  cursor: pointer;
  transition: border-color 0.15s;
}

.performance-toggle:hover {
  border-color: var(--neon-cyan-30);
}

.performance-toggle input[type="checkbox"] {
  margin-top: 0.25rem;
  flex-shrink: 0;
  cursor: pointer;
}

.performance-toggle input[type="checkbox"]:disabled {
  cursor: not-allowed;
}

.performance-toggle__main {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.performance-toggle__main strong {
  font-size: 0.88rem;
  color: var(--text-primary);
}

.performance-toggle__sub {
  font-size: 0.78rem;
  color: var(--text-secondary);
  line-height: 1.45;
}

.performance-error {
  margin-top: 0.55rem;
  font-size: 0.78rem;
  color: rgba(248, 113, 113, 0.9);
  background: rgba(248, 113, 113, 0.06);
  border: 1px solid rgba(248, 113, 113, 0.2);
  border-radius: 6px;
  padding: 0.4rem 0.7rem;
}
</style>
