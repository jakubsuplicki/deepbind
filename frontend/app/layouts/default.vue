<template>
  <div class="default-layout">
    <StatusBar />
    <!-- Trial / grace banner — slim, top-of-view, never modal. ADR 019 -->
    <TrialBanner />
    <slot />

    <!-- License walls — modal overlays when entitlement state requires.
         The wall is rendered last so it stacks over everything (z-index in
         the component handles paint order). Past-grace allows the
         underlying app to remain visible behind the wall (for visual
         continuity), while activation walls fully occlude. -->
    <LicenseWall
      v-if="license.showClockInvalidWall.value"
      variant="clock-invalid"
    />
    <LicenseWall
      v-else-if="license.showActivationWall.value"
      variant="activation"
    />
    <LicenseWall
      v-else-if="license.showPastGraceWall.value"
      variant="past-grace"
      :data-folder-path="workspacePath"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import TrialBanner from '~/components/license/TrialBanner.vue'
import LicenseWall from '~/components/license/LicenseWall.vue'
import { useLicenseState } from '~/composables/useLicenseState'
import { apiUrl } from '~/utils/apiUrl'

const license = useLicenseState()
const workspacePath = ref('')

// `unlisten` handle for the Tauri event subscription set up in onMounted.
// Cleared in onBeforeUnmount so we don't leak listeners across hot-reloads.
let unlistenFileOpened: (() => void) | null = null

onMounted(async () => {
  // Fetch workspace path so the past-grace wall can deep-link the user
  // to their data. Best-effort — if the call fails the wall just hides
  // the "Open my data folder" affordance gracefully (button needs a
  // path to fire). The endpoint is unauth + always reachable.
  try {
    const resp = await fetch(apiUrl('/api/workspace/status'))
    if (resp.ok) {
      const body = await resp.json()
      const path =
        (body?.workspace_path as string | undefined) ??
        (body?.path as string | undefined) ??
        ''
      // Append /memory so the user lands directly on their notes/chats.
      workspacePath.value = path ? `${path.replace(/\/$/, '')}/memory` : ''
    }
  } catch {
    /* silent — past-grace wall renders without the affordance */
  }

  // ADR 019 chunk 5 — listen for `.deepfileslic` file-open events from the
  // Rust shell. Fires when the user double-clicks a license attachment in
  // their email or in Finder. We pipe the file content into the same
  // installFromText flow the wall + Settings panel use.
  if (typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window) {
    try {
      const { listen } = await import('@tauri-apps/api/event')
      unlistenFileOpened = await listen<string>(
        'license:file_opened',
        async (evt) => {
          const text = evt.payload
          if (!text || typeof text !== 'string') return
          await license.installFromText(text)
        },
      )
    } catch (e) {
      console.error('[license] file_opened listener setup failed:', e)
    }
  }
})

onBeforeUnmount(() => {
  if (unlistenFileOpened) {
    unlistenFileOpened()
    unlistenFileOpened = null
  }
})
</script>

<style scoped>
.default-layout {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}
</style>
