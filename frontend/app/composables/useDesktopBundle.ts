/**
 * useDesktopBundle — read the ADR 014 build-time flag.
 *
 * Single source of truth for the cloud-provider UI gate. Components that
 * render cloud-provider surfaces (the API-keys settings panel, the
 * Add-Key modal, the cloud entries in the chat-model picker, the cloud
 * branch of the onboarding choice) read this and skip rendering when
 * the build was produced with `JARVIS_DESKTOP_BUNDLE=1`.
 *
 * Why a composable rather than a direct `useRuntimeConfig()` call: keeps
 * the Nuxt-config key name (`desktopBundle`) and its string-vs-bool
 * normalisation in one place. Callers see a clean reactive `isDesktopBundle`
 * boolean.
 */

import { computed } from 'vue'

export function useDesktopBundle() {
  const config = useRuntimeConfig()
  // Cast through unknown so the typescript-strict compiler accepts the
  // env-var-shaped string. Nuxt's runtime config types vary by version.
  const flag = (config.public.desktopBundle ?? '1') as unknown as string
  const isDesktopBundle = computed(() => flag === '1')
  return { isDesktopBundle }
}
