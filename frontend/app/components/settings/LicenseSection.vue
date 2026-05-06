<template>
  <SettingsSection
    id="license"
    title="License"
    section-class="license-section"
    icon="ph:key"
    icon-active="ph:key-fill"
    :default-open="true"
  >
    <template #suffix>
      <span class="license-section__pill" :class="pillClass">{{ pillLabel }}</span>
    </template>

    <!-- Status block: who, until when, how many days. -->
    <dl class="license-section__status">
      <div class="license-section__row">
        <dt>Status</dt>
        <dd>{{ statusLabel }}</dd>
      </div>
      <div v-if="state?.customer" class="license-section__row">
        <dt>Licensed to</dt>
        <dd>{{ state.customer }}</dd>
      </div>
      <div v-if="state?.expires_at" class="license-section__row">
        <dt>Expires</dt>
        <dd>{{ formatDate(state.expires_at) }}</dd>
      </div>
      <div v-if="state?.trial_started_at && !state.customer" class="license-section__row">
        <dt>Trial started</dt>
        <dd>{{ formatDate(state.trial_started_at) }}</dd>
      </div>
      <div v-if="(state?.days_remaining ?? 0) > 0" class="license-section__row">
        <dt>{{ state?.customer ? 'Days remaining' : 'Trial days remaining' }}</dt>
        <dd>{{ state?.days_remaining }}</dd>
      </div>
      <div v-if="state?.license_id" class="license-section__row">
        <dt>License ID</dt>
        <dd class="license-section__mono">{{ state.license_id }}</dd>
      </div>
    </dl>

    <p
      v-if="state?.reason"
      class="license-section__reason"
      :class="{ 'license-section__reason--warn': !state.is_functional }"
    >
      {{ state.reason }}
    </p>

    <!-- Paste-a-key form — same flow as the wall, but can be used at any
         time during the trial to convert to paid without waiting for
         expiry. -->
    <form class="license-section__form" @submit.prevent="onSubmit">
      <label class="license-section__label" for="license-section-input">
        Paste a license to {{ state?.customer ? 'replace' : 'activate' }}
      </label>
      <textarea
        id="license-section-input"
        v-model="pastedText"
        class="license-section__textarea"
        rows="3"
        placeholder="-----BEGIN LICENSE-----…-----END LICENSE-----"
        spellcheck="false"
        autocomplete="off"
        :disabled="busy"
      />
      <p
        v-if="errorMessage"
        class="license-section__error"
        role="alert"
      >
        {{ errorMessage }}
      </p>
      <p
        v-if="successMessage"
        class="license-section__success"
        role="status"
      >
        {{ successMessage }}
      </p>
      <div class="license-section__actions">
        <button
          v-if="state?.customer"
          type="button"
          class="license-section__secondary"
          :disabled="busy"
          @click="onClear"
        >
          Reset license
        </button>
        <button
          type="submit"
          class="license-section__primary"
          :disabled="busy || !pastedText.trim()"
        >
          <span v-if="busy">Activating…</span>
          <span v-else>{{ state?.customer ? 'Replace' : 'Activate' }}</span>
        </button>
      </div>
    </form>

    <p class="license-section__hint">
      DeepFilesAI verifies licenses entirely on your machine — no network
      call. Your data never leaves your trust boundary.
      <a
        href="https://deepfilesai.com/buy"
        target="_blank"
        rel="noopener"
        class="license-section__link"
      >Buy or renew →</a>
    </p>
  </SettingsSection>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import { useLicenseState } from '~/composables/useLicenseState'

const license = useLicenseState()
const state = computed(() => license.state.value)

const pastedText = ref('')
const busy = ref(false)
const errorMessage = ref<string | null>(null)
const successMessage = ref<string | null>(null)

const STATUS_LABELS: Record<string, string> = {
  unlicensed_trial_active: 'Free trial — active',
  unlicensed_trial_expiring: 'Free trial — ending soon',
  unlicensed_trial_expired: 'Trial expired',
  licensed_active: 'Licensed',
  licensed_in_grace: 'Licensed — in grace period',
  licensed_past_grace: 'License expired (read-only)',
  licensed_invalid: 'License invalid',
}

const statusLabel = computed(() => {
  const s = state.value?.state
  if (!s) return 'Unknown'
  return STATUS_LABELS[s] ?? s
})

const pillLabel = computed(() => {
  const s = state.value?.state
  if (s?.startsWith('licensed_active')) return 'paid'
  if (s === 'licensed_in_grace') return 'grace'
  if (s === 'licensed_past_grace') return 'read-only'
  if (s === 'licensed_invalid') return 'invalid'
  if (s === 'unlicensed_trial_expired') return 'expired'
  if (s === 'unlicensed_trial_expiring') return 'trial · ending'
  if (s === 'unlicensed_trial_active') return 'trial'
  return ''
})

const pillClass = computed(() => {
  const s = state.value?.state
  if (s === 'licensed_active') return 'license-section__pill--ok'
  if (s === 'licensed_in_grace' || s === 'unlicensed_trial_expiring')
    return 'license-section__pill--warn'
  if (
    s === 'unlicensed_trial_expired' ||
    s === 'licensed_past_grace' ||
    s === 'licensed_invalid'
  )
    return 'license-section__pill--bad'
  return 'license-section__pill--info'
})

function formatDate(iso: string): string {
  try {
    return new Intl.DateTimeFormat(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    }).format(new Date(iso))
  } catch {
    return iso
  }
}

async function onSubmit(): Promise<void> {
  errorMessage.value = null
  successMessage.value = null
  busy.value = true
  try {
    const next = await license.installFromText(pastedText.value)
    if (!next) {
      errorMessage.value = 'Could not contact the license service.'
      return
    }
    if (next.state === 'licensed_invalid') {
      errorMessage.value = next.reason ?? 'License could not be verified.'
      return
    }
    pastedText.value = ''
    successMessage.value = `Activated for ${next.customer ?? 'this workspace'}.`
  } catch (e) {
    const err = e as Error
    errorMessage.value = err?.message ?? 'Activation failed.'
  } finally {
    busy.value = false
  }
}

async function onClear(): Promise<void> {
  errorMessage.value = null
  successMessage.value = null
  busy.value = true
  try {
    await license.clearLicense()
    successMessage.value = 'License removed. Trial state restored.'
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.license-section__status {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.35rem 1rem;
  margin: 0 0 0.75rem 0;
  font-size: 0.88rem;
}

.license-section__row {
  display: contents;
}

.license-section__row dt {
  color: var(--color-muted, #888);
  font-weight: 500;
}

.license-section__row dd {
  margin: 0;
  color: var(--color-text, #ddd);
}

.license-section__mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.78rem;
}

.license-section__reason {
  margin: 0.5rem 0 1rem 0;
  font-size: 0.85rem;
  color: var(--color-muted, #999);
}

.license-section__reason--warn {
  color: var(--color-warning, #d99);
}

.license-section__form {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-top: 0.5rem;
}

.license-section__label {
  font-size: 0.85rem;
  font-weight: 500;
  color: var(--color-muted, #aaa);
}

.license-section__textarea {
  width: 100%;
  padding: 0.55rem 0.7rem;
  border-radius: 6px;
  border: 1px solid var(--color-border, #333);
  background: var(--color-surface-subtle, rgba(0, 0, 0, 0.25));
  color: var(--color-text, #ddd);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.78rem;
  line-height: 1.4;
  resize: vertical;
}

.license-section__textarea:focus {
  outline: 2px solid var(--color-accent, #6ab0ff);
  outline-offset: -1px;
}

.license-section__error {
  margin: 0;
  font-size: 0.85rem;
  color: var(--color-warning, #d99);
}

.license-section__success {
  margin: 0;
  font-size: 0.85rem;
  color: var(--color-success, #6ad8a0);
}

.license-section__actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
}

.license-section__primary,
.license-section__secondary {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.45rem 0.95rem;
  border-radius: 6px;
  font-weight: 600;
  font-size: 0.85rem;
  cursor: pointer;
}

.license-section__primary {
  border: 0;
  background: var(--color-accent, #6ab0ff);
  color: var(--color-on-accent, #fff);
}

.license-section__secondary {
  border: 1px solid var(--color-border, #333);
  background: transparent;
  color: var(--color-text, #ddd);
}

.license-section__primary:disabled,
.license-section__secondary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.license-section__hint {
  margin-top: 0.85rem;
  font-size: 0.82rem;
  color: var(--color-muted, #888);
  line-height: 1.5;
}

.license-section__link {
  color: var(--color-link, #6ab0ff);
  text-decoration: none;
}

.license-section__link:hover {
  text-decoration: underline;
}

.license-section__pill {
  display: inline-flex;
  padding: 0.15rem 0.5rem;
  border-radius: 999px;
  font-size: 0.7rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.license-section__pill--ok {
  background: rgba(106, 216, 160, 0.15);
  color: var(--color-success, #6ad8a0);
}

.license-section__pill--warn {
  background: rgba(255, 165, 0, 0.15);
  color: orange;
}

.license-section__pill--bad {
  background: rgba(217, 153, 153, 0.15);
  color: var(--color-warning, #d99);
}

.license-section__pill--info {
  background: rgba(106, 176, 255, 0.15);
  color: var(--color-accent, #6ab0ff);
}
</style>
