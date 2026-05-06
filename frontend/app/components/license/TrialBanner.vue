<template>
  <div
    v-if="visible"
    class="trial-banner"
    :class="{
      'trial-banner--expiring': isExpiring,
      'trial-banner--grace': isInGrace,
    }"
    role="status"
  >
    <Icon
      :name="iconName"
      class="trial-banner__icon icon--sm"
      aria-hidden="true"
    />
    <span class="trial-banner__text">{{ message }}</span>
    <NuxtLink to="/settings#license" class="trial-banner__link">
      {{ ctaLabel }}
    </NuxtLink>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useLicenseState } from '~/composables/useLicenseState'

const license = useLicenseState()

const isExpiring = computed(
  () => license.state.value?.state === 'unlicensed_trial_expiring',
)
const isInGrace = computed(
  () => license.state.value?.state === 'licensed_in_grace',
)
const isTrial = computed(
  () =>
    license.state.value?.state === 'unlicensed_trial_active' ||
    isExpiring.value,
)

const visible = computed(() => isTrial.value || isInGrace.value)

const iconName = computed(() => {
  if (isInGrace.value) return 'ph:warning-circle-fill'
  if (isExpiring.value) return 'ph:hourglass-medium-fill'
  return 'ph:hourglass-medium'
})

const message = computed(() => {
  const days = license.state.value?.days_remaining ?? 0
  if (isInGrace.value) {
    return `License expired — grace period ends in ${days} day${
      days === 1 ? '' : 's'
    }.`
  }
  if (isExpiring.value) {
    return `Trial ends in ${days} day${days === 1 ? '' : 's'}.`
  }
  return `Trial: ${days} day${days === 1 ? '' : 's'} left.`
})

const ctaLabel = computed(() => {
  if (isInGrace.value) return 'Renew now'
  return 'Get a license'
})
</script>

<style scoped>
.trial-banner {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0.8rem;
  font-size: 0.8rem;
  background: rgba(106, 176, 255, 0.12);
  border-bottom: 1px solid rgba(106, 176, 255, 0.25);
  color: var(--color-text, #ddd);
}

.trial-banner--expiring {
  background: rgba(255, 165, 0, 0.12);
  border-color: rgba(255, 165, 0, 0.3);
}

.trial-banner--grace {
  background: rgba(217, 153, 153, 0.12);
  border-color: rgba(217, 153, 153, 0.3);
}

.trial-banner__icon {
  flex-shrink: 0;
  color: var(--color-accent, #6ab0ff);
}

.trial-banner--expiring .trial-banner__icon {
  color: orange;
}

.trial-banner--grace .trial-banner__icon {
  color: var(--color-warning, #d99);
}

.trial-banner__text {
  flex: 1;
}

.trial-banner__link {
  color: var(--color-link, #6ab0ff);
  text-decoration: none;
  font-weight: 600;
}

.trial-banner__link:hover {
  text-decoration: underline;
}
</style>
