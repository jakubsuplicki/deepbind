<template>
  <SettingsSection
    id="privacy"
    title="Privacy & Network"
    section-class="privacy-section"
    icon="ph:shield-check"
    icon-active="ph:shield-check-fill"
    :default-open="false"
  >
    <template #suffix>
      <span class="privacy-badge" :class="{ 'privacy-badge--active': privacy?.offline_mode }">
        <Icon
          v-if="privacy?.offline_mode"
          name="ph:shield-check-fill"
          class="icon--sm icon--success privacy-badge__icon"
        />
        <Icon
          v-else
          name="ph:globe"
          class="icon--sm icon--muted privacy-badge__icon"
        />
        {{ privacy?.offline_mode ? 'Offline mode active' : 'Network access on' }}
      </span>
    </template>

    <p class="settings-page__hint">
      Control which outbound network calls Jarvis is allowed to make.
      All AI runs locally via Ollama and is <strong>never</strong> blocked —
      these toggles only gate optional network features (URL ingest).
    </p>

    <!-- Master switch -->
    <div class="privacy-row privacy-row--master">
      <label class="privacy-toggle" :class="{ 'privacy-toggle--locked': privacy?.offline_mode_locked }">
        <input
          type="checkbox"
          :checked="!!privacy?.offline_mode"
          :disabled="privacy?.offline_mode_locked || saving"
          @change="onSet('offline_mode', ($event.target as HTMLInputElement).checked)"
        />
        <span class="privacy-toggle__main">
          <strong>Offline mode</strong>
          <span class="privacy-toggle__sub">
            Hard-block every outbound integration: URL ingest.
            Local Ollama is unaffected.
          </span>
        </span>
      </label>
      <p v-if="privacy?.offline_mode_locked" class="privacy-locked-note">
        <Icon name="ph:lock-key-fill" class="icon--sm icon--muted privacy-locked-note__icon" />
        Locked by <code>JARVIS_OFFLINE_MODE</code> environment variable.
      </p>
    </div>

    <div class="privacy-row" :class="{ 'privacy-row--disabled': privacy?.offline_mode }">
      <label class="privacy-toggle">
        <input
          type="checkbox"
          :checked="!!privacy?.url_ingest_enabled"
          :disabled="privacy?.offline_mode || saving"
          @change="onSet('url_ingest_enabled', ($event.target as HTMLInputElement).checked)"
        />
        <span class="privacy-toggle__main">
          <strong>Allow URL ingest</strong>
          <span class="privacy-toggle__sub">
            Fetch web pages and YouTube metadata/transcripts for imports. Outbound HTTP
            to public URLs (private IPs are blocked).
          </span>
        </span>
      </label>
    </div>

    <p v-if="error" class="privacy-error">{{ error }}</p>
  </SettingsSection>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import { usePrivacySettings, type PrivacyState } from '~/composables/settings/usePrivacySettings'
import { useSettingsStatus } from '~/composables/settings/useSettingsStatus'

const { privacy, saving, error, load, set } = usePrivacySettings()
const status = useSettingsStatus()

async function onSet(key: keyof PrivacyState, value: boolean) {
  await set(key, value)
  if (!error.value) {
    status.set(value
      ? `Enabled: ${String(key).replaceAll('_', ' ')}`
      : `Disabled: ${String(key).replaceAll('_', ' ')}`)
  }
}

onMounted(load)
</script>
