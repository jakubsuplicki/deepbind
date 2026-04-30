<template>
  <div class="onboarding">
    <div class="onboarding__card onboarding__card--wide">
      <div class="onboarding__brand">
        <h1 class="onboarding__title">Jarvis</h1>
        <p class="onboarding__subtitle">An AI workspace that remembers what matters</p>
      </div>

      <!-- ADR 015 — single-target local-only stack: there is no cloud branch
           to choose. The wizard goes straight into the local-model flow. -->
      <OnboardingLocalFlow
        @model-ready="handleSubmit"
      />
      <p v-if="error" class="onboarding__error">{{ error }}</p>

      <p class="onboarding__settings-hint">
        You can manage local models anytime in <strong>Settings</strong>.
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
const loading = ref(false)
const error = ref('')

const { isInitialized } = useAppState()

async function handleSubmit() {
  error.value = ''
  loading.value = true
  const { initWorkspace } = useApi()
  try {
    await initWorkspace()
  } catch (e: unknown) {
    // Non-fatal: workspace may already exist (e.g. re-running onboarding)
    const msg = e && typeof e === 'object' && 'message' in e ? (e as Error).message : ''
    if (!msg.includes('already') && !msg.includes('exists')) {
      error.value = msg || 'Connection error. Is the backend running?'
      loading.value = false
      return
    }
  }
  isInitialized.value = true
  loading.value = false
  await navigateTo('/main', { replace: true })
}
</script>

<style scoped>
.onboarding {
  display: flex;
  align-items: flex-start;
  justify-content: center;
  min-height: 100vh;
  padding: 2rem 1rem 4rem;
  overflow-y: auto;
}

.onboarding__card {
  background: var(--bg-surface, #111122);
  border: 1px solid var(--border-default, #222);
  border-radius: 10px;
  padding: 2rem 2.5rem 2.5rem;
  width: 100%;
  max-width: 520px;
  margin: auto 0;
  transition: max-width 0.3s ease;
}

.onboarding__card--wide {
  max-width: 640px;
}

.onboarding__brand {
  text-align: center;
  margin-bottom: 1.5rem;
}

.onboarding__title {
  font-size: 2rem;
  font-weight: 300;
  letter-spacing: 0.1em;
  margin-bottom: 0.25rem;
}

.onboarding__subtitle {
  color: var(--text-muted, #888);
  font-size: 0.875rem;
}

.onboarding__error {
  color: #ef4444;
  font-size: 0.8125rem;
  margin-top: 0.75rem;
  text-align: center;
}

.onboarding__settings-hint {
  font-size: 0.75rem;
  color: var(--text-muted, #666);
  text-align: center;
  margin-top: 1rem;
}

.onboarding__settings-hint strong {
  color: var(--neon-cyan-60, #5bb8b9);
}
</style>
