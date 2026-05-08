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
import { useAppState } from '~/composables/useAppState'
import { useWarmup } from '~/composables/useWarmup'
import { apiUrl } from '~/utils/apiUrl'

const license = useLicenseState()
const { checkHealth } = useAppState()
const { startWarmupPolling } = useWarmup()
const workspacePath = ref('')

// `unlisten` handle for the Tauri event subscription set up in onMounted.
// Cleared in onBeforeUnmount so we don't leak listeners across hot-reloads.
let unlistenFileOpened: (() => void) | null = null

// Backend health poll. The StatusBar pill (`Alive` / `Offline` /
// `Checking…`) reads `useAppState().backendStatus`, which only updates
// when `checkHealth()` runs. Previously checkHealth was invoked once
// in pages/main.vue:onMounted, which means the pill stayed `Checking…`
// on the onboarding wizard and on any other page that didn't mount
// /main (the wizard does not). Layout-level invocation fires on every
// page; the 30 s repeat catches a backend that goes away mid-session.
let healthPollTimer: ReturnType<typeof setInterval> | null = null
const HEALTH_POLL_MS = 30_000

onMounted(async () => {
  // Backend health: fire once immediately, then keep a 30 s heartbeat.
  // Status pill in StatusBar reads from this.
  checkHealth()
  healthPollTimer = setInterval(checkHealth, HEALTH_POLL_MS)

  // ML warmup poll. The sidecar warms fastembed/spaCy/HF tokenizers in a
  // background thread on boot; this composable polls /api/health/warm
  // until ready (idempotent — only one poll loop runs across the app).
  // See backend/services/warmup_service.py and ChatPanel's warmup banner.
  startWarmupPolling()

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
  if (healthPollTimer) {
    clearInterval(healthPollTimer)
    healthPollTimer = null
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
