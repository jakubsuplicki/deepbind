<template>
  <div class="boot-host">
    <!-- Calibration-boot splash. Renders on every Tauri-shell launch until
         the boot sequence (Ollama → sidecar → license probe) completes.
         In browser-dev mode `useBoot` short-circuits to ready=true and the
         splash never paints.

         The splash unmounts ~460 ms AFTER `boot.ready` flips, so it has
         time to crossfade out (its own 420 ms `.splash--ready` opacity
         transition) over the real layout below. -->
    <SplashScreen v-if="!hideSplash" />

    <!-- Real layout — mounts only after boot completes. By that point the
         splash has injected `__JARVIS_CONFIG__` and `__JARVIS_LICENSE_STATE__`
         onto window, so `useLicenseState` and `useApi` see populated globals
         on their first read. ADR 019's first-paint contract holds: no gated
         surface paints until license state is known. -->
    <div v-if="boot.ready.value" class="default-layout">
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
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import TrialBanner from '~/components/license/TrialBanner.vue'
import LicenseWall from '~/components/license/LicenseWall.vue'
import SplashScreen from '~/components/SplashScreen.vue'
import { useLicenseState } from '~/composables/useLicenseState'
import { useAppState } from '~/composables/useAppState'
import { useWarmup } from '~/composables/useWarmup'
import { useBoot } from '~/composables/useBoot'
import { apiUrl } from '~/utils/apiUrl'

const license = useLicenseState()
const { checkHealth } = useAppState()
const { startWarmupPolling } = useWarmup()
const boot = useBoot()
const workspacePath = ref('')

// Splash unmount is delayed ~460 ms after boot.ready flips so the splash's
// own opacity transition (420 ms) finishes before the DOM node disappears.
const hideSplash = ref(false)

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

// Hydrate the boot-state subscription as early as possible — before
// onMounted's other side-effects, since they don't matter until boot
// completes. initBoot is idempotent.
boot.initBoot()

// Drive the splash unmount once the boot completes. The 460 ms delay is
// >= the splash's own 420 ms opacity transition; if either changes, keep
// this strictly larger so the visual fade always finishes first.
watch(
  () => boot.ready.value,
  (ready) => {
    if (ready) setTimeout(() => { hideSplash.value = true }, 460)
  },
  { immediate: true },
)

onMounted(async () => {
  // Defer everything that depends on the backend being up until boot
  // completes. Without this gate, `checkHealth()` would race the sidecar
  // boot and report "Offline" briefly even on healthy launches.
  if (!boot.ready.value) {
    await new Promise<void>((resolve) => {
      const stop = watch(
        () => boot.ready.value,
        (ready) => { if (ready) { stop(); resolve() } },
      )
    })
  }

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
.boot-host {
  height: 100vh;
  position: relative;
}

.default-layout {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}
</style>
