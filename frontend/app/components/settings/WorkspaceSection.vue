<template>
  <SettingsSection
    id="workspace"
    title="Workspace"
    icon="ph:folder-open"
    icon-active="ph:folder-open-fill"
    :default-open="true"
  >
    <p class="settings-page__path">{{ path }}</p>
  </SettingsSection>
</template>

<script setup lang="ts">
import SettingsSection from '~/components/settings/SettingsSection.vue'

defineProps<{ path: string }>()
</script>
