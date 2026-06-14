<template>
  <div class="license-wall" role="dialog" aria-modal="true">
    <div class="license-wall__panel">
      <Icon
        :name="iconName"
        class="license-wall__icon"
        :class="{ 'license-wall__icon--past-grace': isPastGrace }"
        aria-hidden="true"
      />

      <h1 class="license-wall__title">{{ title }}</h1>
      <p class="license-wall__subtitle">{{ subtitle }}</p>

      <!-- Past-grace branch: surfaces the data-folder affordance instead
           of paste-a-key (the customer's data is already on disk; ADR 019
           §"Past-grace state"). They can still paste a renewal key from
           below if they have one. -->
      <div v-if="isPastGrace" class="license-wall__past-grace-actions">
        <button class="license-wall__primary" @click="onOpenDataFolder">
          <Icon name="ph:folder-open-fill" class="icon--sm" aria-hidden="true" />
          Open my data folder
        </button>
        <p class="license-wall__data-path">{{ dataFolderPath }}</p>
      </div>

      <!-- Clock-invalid branch: no paste-a-key form, no activation —
           the user has to fix their OS clock first. ADR 006 §"Failure
           UX (not a hard refuse)". A "re-paste my license" affordance
           is preserved for the rare CMOS-battery-died case where a
           legitimate fresh-pasted license can recover the floor. -->
      <div
        v-if="isClockInvalid"
        class="license-wall__past-grace-actions"
      >
        <p class="license-wall__data-path">
          Open your OS date/time settings → enable "Set automatically".
          Then re-launch DeepBind.
        </p>
      </div>

      <!-- Paste-a-key form (always available — also the renewal entry
           point from past-grace, and the recovery path for the
           clock-invalid CMOS-battery-died case). -->
      <form class="license-wall__form" @submit.prevent="onSubmit">
        <label class="license-wall__label" for="license-wall-input">
          {{ pasteFieldLabel }}
        </label>
        <textarea
          id="license-wall-input"
          v-model="pastedText"
          class="license-wall__textarea"
          rows="4"
          placeholder="-----BEGIN LICENSE-----&#10;…&#10;-----END LICENSE-----"
          spellcheck="false"
          autocomplete="off"
          :disabled="busy"
        />
        <p
          v-if="errorMessage"
          class="license-wall__error"
          role="alert"
        >
          {{ errorMessage }}
        </p>
        <div class="license-wall__actions">
          <button
            type="submit"
            class="license-wall__primary"
            :disabled="busy || !pastedText.trim()"
          >
            <span v-if="busy">Activating…</span>
            <span v-else>Activate license</span>
          </button>
        </div>
      </form>

      <p class="license-wall__hint">
        This build includes a reference offline-licensing subsystem. See the
        <a
          href="https://github.com/jakubsuplicki/deepbind/blob/main/docs/architecture/decisions/019-licensing-operational-model.md"
          target="_blank"
          rel="noopener"
          class="license-wall__link"
        >licensing docs</a>
        for how to generate and install a license file.
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useLicenseState } from '~/composables/useLicenseState'

const props = defineProps<{
  variant: 'activation' | 'past-grace' | 'clock-invalid'
  /** Workspace path for the past-grace "Open in Finder" affordance. */
  dataFolderPath?: string
}>()

const license = useLicenseState()
const pastedText = ref('')
const busy = ref(false)
const errorMessage = ref<string | null>(null)

const isPastGrace = computed(() => props.variant === 'past-grace')
const isClockInvalid = computed(() => props.variant === 'clock-invalid')

const iconName = computed(() => {
  if (isClockInvalid.value) return 'ph:clock-countdown-fill'
  if (isPastGrace.value) return 'ph:archive-fill'
  // Trial-expired / invalid → key icon (the activation prompt).
  return 'ph:key-fill'
})

const title = computed(() => {
  const s = license.state.value?.state
  if (s === 'clock_invalid') return 'System clock issue detected'
  if (s === 'licensed_past_grace') return 'License expired'
  if (s === 'licensed_invalid') return 'License file is invalid'
  return 'Your trial has ended'
})

const subtitle = computed(() => {
  const s = license.state.value?.state
  if (s === 'clock_invalid') {
    return (
      license.state.value?.reason ??
      'Your system clock appears to be set incorrectly. Open your OS date/time settings, ' +
        'enable automatic time, then re-launch DeepBind.'
    )
  }
  if (s === 'licensed_past_grace') {
    return (
      'Your data is still on your disk and remains accessible — DeepBind ' +
      'never holds your knowledge hostage. To resume writing notes, ingesting, ' +
      'or chatting, paste a renewed license below.'
    )
  }
  if (s === 'licensed_invalid') {
    return (
      'The license file in your app data folder did not pass verification. ' +
      'Re-paste it below, or install a freshly-issued one.'
    )
  }
  return 'Activate a license below to keep using DeepBind.'
})

const pasteFieldLabel = computed(() => {
  if (isPastGrace.value) return 'Paste your renewed license'
  return 'Paste your license'
})

async function onSubmit(): Promise<void> {
  errorMessage.value = null
  busy.value = true
  try {
    const next = await license.installFromText(pastedText.value)
    if (!next) {
      errorMessage.value =
        'Could not contact the license service. Please try again in a moment.'
      return
    }
    if (next.state === 'licensed_invalid') {
      errorMessage.value = next.reason ?? 'License could not be verified.'
      return
    }
    // Valid — clear the textarea; the parent will hide the wall as the
    // computed `isWalled` flips to false.
    pastedText.value = ''
  } catch (e) {
    const err = e as Error
    errorMessage.value = err?.message ?? 'Activation failed.'
  } finally {
    busy.value = false
  }
}

async function onOpenDataFolder(): Promise<void> {
  if (!props.dataFolderPath) return
  await license.openDataFolder(props.dataFolderPath)
}
</script>

<style scoped>
.license-wall {
  position: fixed;
  inset: 0;
  z-index: 9000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1.5rem;
  background: rgba(0, 0, 0, 0.72);
  backdrop-filter: blur(8px);
}

.license-wall__panel {
  width: 100%;
  max-width: 32rem;
  padding: 2rem;
  border-radius: 12px;
  background: var(--color-surface, #1a1a1a);
  border: 1px solid var(--color-border, #2a2a2a);
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
  color: var(--color-text, #ddd);
}

.license-wall__icon {
  width: 2.5rem;
  height: 2.5rem;
  color: var(--color-accent, #6ab0ff);
  margin-bottom: 0.75rem;
  display: block;
}

.license-wall__icon--past-grace {
  color: var(--color-warning, #d99);
}

.license-wall__title {
  font-size: 1.4rem;
  font-weight: 600;
  margin: 0 0 0.5rem 0;
}

.license-wall__subtitle {
  margin: 0 0 1.25rem 0;
  font-size: 0.95rem;
  line-height: 1.5;
  color: var(--color-muted, #999);
}

.license-wall__past-grace-actions {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  padding: 0.9rem 1rem;
  margin-bottom: 1.25rem;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--color-border, #2a2a2a);
}

.license-wall__data-path {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.78rem;
  color: var(--color-muted, #888);
  margin: 0;
  word-break: break-all;
}

.license-wall__form {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.license-wall__label {
  font-size: 0.85rem;
  font-weight: 500;
  color: var(--color-muted, #aaa);
}

.license-wall__textarea {
  width: 100%;
  padding: 0.6rem 0.75rem;
  border-radius: 6px;
  border: 1px solid var(--color-border, #333);
  background: var(--color-surface-subtle, rgba(0, 0, 0, 0.3));
  color: var(--color-text, #ddd);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.78rem;
  line-height: 1.4;
  resize: vertical;
}

.license-wall__textarea:focus {
  outline: 2px solid var(--color-accent, #6ab0ff);
  outline-offset: -1px;
}

.license-wall__error {
  margin: 0;
  font-size: 0.85rem;
  color: var(--color-warning, #d99);
}

.license-wall__actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
}

.license-wall__primary {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.55rem 1.1rem;
  border-radius: 6px;
  border: 0;
  background: var(--color-accent, #6ab0ff);
  color: var(--color-on-accent, #fff);
  font-weight: 600;
  font-size: 0.9rem;
  cursor: pointer;
}

.license-wall__primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.license-wall__hint {
  margin-top: 1.25rem;
  font-size: 0.85rem;
  color: var(--color-muted, #888);
}

.license-wall__link {
  color: var(--color-link, #6ab0ff);
  text-decoration: none;
}

.license-wall__link:hover {
  text-decoration: underline;
}
</style>
