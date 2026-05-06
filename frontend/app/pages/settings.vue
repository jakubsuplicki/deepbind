<template>
  <div class="settings-page">
    <h1 class="settings-page__title">
      <Icon name="ph:gear-six-fill" class="settings-page__title-icon" aria-hidden="true" />
      Settings
    </h1>

    <!-- ADR 015 — single-target local-only stack: cloud-providers UI is gone. -->

    <LicenseSection />

    <LocalModelsSection />

    <PerformanceSection />

    <WorkspaceSection :path="general.workspacePath.value" />

    <McpSection />

    <VoiceSection
      v-model="general.autoSpeak.value"
      @change="general.updateVoice()"
    />

    <MaintenanceSection />

    <SmartConnectSection />

    <GraphExpansionSection />

    <PrivacySection />

    <SharpenSection />

    <AcknowledgementsSection />

    <p v-if="status.message.value" class="settings-page__status">
      {{ status.message.value }}
    </p>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import LicenseSection from '~/components/settings/LicenseSection.vue'
import LocalModelsSection from '~/components/settings/LocalModelsSection.vue'
import PerformanceSection from '~/components/settings/PerformanceSection.vue'
import WorkspaceSection from '~/components/settings/WorkspaceSection.vue'
import McpSection from '~/components/settings/McpSection.vue'
import VoiceSection from '~/components/settings/VoiceSection.vue'
import MaintenanceSection from '~/components/settings/MaintenanceSection.vue'
import SmartConnectSection from '~/components/settings/SmartConnectSection.vue'
import GraphExpansionSection from '~/components/settings/GraphExpansionSection.vue'
import PrivacySection from '~/components/settings/PrivacySection.vue'
import SharpenSection from '~/components/settings/SharpenSection.vue'
import AcknowledgementsSection from '~/components/settings/AcknowledgementsSection.vue'
import { useGeneralSettings } from '~/composables/settings/useGeneralSettings'
import { useSettingsStatus } from '~/composables/settings/useSettingsStatus'

const general = useGeneralSettings()
const status = useSettingsStatus()

onMounted(async () => {
  await general.load()
  if (general.error.value) status.set(general.error.value)
})
</script>
