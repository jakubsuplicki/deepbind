<script setup lang="ts">
/**
 * OnboardingLocalFlow — first-run pull pipeline UI for the bundled-Ollama
 * desktop product (ADR 005 §B). Two layered paths:
 *
 *   Layer 1 — orchestrator-driven (default in the bundled build).
 *   Drives `useFirstRun` against `/api/local/first-run/*`. Auto-kicks the
 *   pipeline on mount when there is no marker file. Releases the user into
 *   chat the moment the foreground primary pull lands; the background
 *   fallback pull and the chat-model probe continue silently.
 *
 *   Layer 2 — manual model picker.
 *   Engaged when the user clicks "Pick my own model later" (the §B Skip /
 *   opt-out path) or when Ollama is not yet reachable in dev mode (no
 *   bundled sidecar). Drives the existing `useLocalSetupFlow` state
 *   machine — kept verbatim from the legacy onboarding flow for the
 *   `LocalModelsSection.vue` (Settings) consumer.
 */

const emit = defineEmits<{
  (e: 'model-ready'): void
}>()

const localModels = useLocalModels()
const flow = useLocalSetupFlow()
const probe = useChatModelProbe()
const firstRun = useFirstRun()

const showAll = ref(false)
const showManualSetup = ref(false)
const probeFinished = ref(false)

// 'orchestrator' = ADR 005 §B Layer 1; 'manual' = legacy picker (Layer 2).
// Mode flips to 'manual' on the user's skip click or when the runtime is
// not reachable yet (dev/non-bundled mode).
type WizardMode = 'orchestrator' | 'manual'
const mode = ref<WizardMode>('orchestrator')

const linuxCommand = 'curl -fsSL https://ollama.com/install.sh | sh'
const copiedCommand = ref(false)

onMounted(async () => {
  await flow.initialize()
  await firstRun.fetchOnce()
  probe.fetchStatus()

  // Marker present (or already complete) → release immediately.
  if (firstRun.status.value.marker_present || firstRun.status.value.state === 'complete') {
    emit('model-ready')
    return
  }

  // Pure manual fallback: Ollama isn't reachable. Stay in legacy install
  // wizard until it comes up. The dev case where users `ollama serve`
  // themselves; in the bundled build the Tauri sidecar guarantees this is
  // already true when we mount.
  if (!localModels.isOllamaReady()) {
    mode.value = 'manual'
    return
  }

  // Already mid-pipeline (e.g. user navigated away and came back)? Just
  // observe — the composable's poll picks up where it left off.
  if (firstRun.active.value) return

  // Idle + reachable + no marker → kick the §B pipeline.
  if (firstRun.status.value.state === 'idle') {
    await firstRun.start()
  }
})

onUnmounted(() => {
  flow.cleanup()
})

// Once the orchestrator hits `complete`, run the probe panel for the user
// to see the validation outcome (mirrors the legacy `model_ready` watcher).
watch(
  () => firstRun.status.value.state,
  async (state) => {
    if (state !== 'complete') return
    if (probeFinished.value) return
    const status = probe.status.value ?? await probe.fetchStatus()
    if (status?.persisted && !status.needs_rerun) {
      probeFinished.value = true
      return
    }
    // The orchestrator runs the probe server-side too; we only re-run
    // here if its persisted record doesn't cover the current environment.
    if (!firstRun.status.value.probe_failed) {
      probeFinished.value = true
      return
    }
    await probe.runProbe()
    probeFinished.value = true
  },
)

// In legacy manual mode, mirror the previous auto-probe-on-model-ready
// behaviour so downloads from the picker still validate.
watch(
  () => flow.state.value,
  async (state) => {
    if (mode.value !== 'manual') return
    if (state === 'model_ready' && !probeFinished.value && !probe.running.value) {
      const status = probe.status.value ?? await probe.fetchStatus()
      if (status?.persisted && !status.needs_rerun) {
        probeFinished.value = true
        return
      }
      await probe.runProbe()
      probeFinished.value = true
    }
  },
)

function copyLinuxCommand() {
  navigator.clipboard.writeText(linuxCommand)
  copiedCommand.value = true
  setTimeout(() => { copiedCommand.value = false }, 2000)
}

async function handlePull(modelId: string) {
  await flow.downloadModel(modelId)
}

async function handleSelect(modelId: string) {
  await flow.selectModel(modelId)
}

function handleFinish() {
  emit('model-ready')
}

function handleDownloadAnother() {
  flow.state.value = 'model_selection'
  mode.value = 'manual'
}

async function handleSkip() {
  // §B Skip / opt-out — no marker is written; user lands in the manual
  // picker and can choose to take responsibility for the chat model.
  await firstRun.start({ skip: true })
  await localModels.fetchCatalog()
  flow.state.value = 'model_selection'
  mode.value = 'manual'
}

async function handleRetry() {
  // Idempotent — concurrent /start while running returns already_running.
  await firstRun.start()
}

const downloadingModel = computed(() => {
  if (!flow.downloadingModelId.value) return null
  return localModels.catalog.value.find(m => m.model_id === flow.downloadingModelId.value)
})

const readyModel = computed(() => localModels.activeModel.value)

// Catalog entry that matches the orchestrator's primary pull, used for
// the "Downloading X — ~Y GB" subhead. Fall back to the raw ollama tag.
const primaryEntry = computed(() => {
  const tag = firstRun.status.value.primary_ollama_model
  if (!tag) return null
  return localModels.catalog.value.find(m => m.ollama_model === tag) ?? null
})

const fallbackEntry = computed(() => {
  const tag = firstRun.status.value.fallback_ollama_model
  if (!tag) return null
  return localModels.catalog.value.find(m => m.ollama_model === tag) ?? null
})

const formattedPrimaryProgress = computed(() => {
  const p = firstRun.status.value.primary
  if (p.total === 0) return null
  return `${formatBytes(p.completed)} / ${formatBytes(p.total)}`
})

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit++
  }
  return `${value.toFixed(unit >= 2 ? 1 : 0)} ${units[unit]}`
}

const tierLabel = computed(() => {
  const tier = firstRun.status.value.tier
  if (!tier) return null
  return { A: 'Tier A · Lightweight', B: 'Tier B · Balanced', C: 'Tier C · Workstation' }[tier]
})

// Step indicators: 1. Detect hardware, 2. Download primary, 3. Ready.
// Maps both orchestrator state and manual flow.state onto a single
// 1..3 progress so the indicator strip stays stable across mode flips.
const wizardStep = computed<1 | 2 | 3>(() => {
  if (mode.value === 'orchestrator') {
    const s = firstRun.status.value.state
    if (s === 'idle' || s === 'probing') return 1
    if (s === 'pulling_primary') return 2
    return 3
  }
  return flow.wizardStep.value
})
</script>

<template>
  <div class="local-flow">
    <h2 class="local-flow__title">Run Jarvis locally</h2>

    <!-- Step indicators -->
    <div class="local-flow__steps">
      <div class="local-flow__step" :class="{
        'local-flow__step--active': wizardStep === 1,
        'local-flow__step--done': wizardStep > 1
      }">
        <span class="local-flow__step-num">{{ wizardStep > 1 ? '✓' : '1' }}</span>
        <span class="local-flow__step-label">Detect hardware</span>
      </div>
      <div class="local-flow__step-line" :class="{ 'local-flow__step-line--done': wizardStep > 1 }" />
      <div class="local-flow__step" :class="{
        'local-flow__step--active': wizardStep === 2,
        'local-flow__step--done': wizardStep > 2
      }">
        <span class="local-flow__step-num">{{ wizardStep > 2 ? '✓' : '2' }}</span>
        <span class="local-flow__step-label">Download model</span>
      </div>
      <div class="local-flow__step-line" :class="{ 'local-flow__step-line--done': wizardStep > 2 }" />
      <div class="local-flow__step" :class="{ 'local-flow__step--active': wizardStep === 3 }">
        <span class="local-flow__step-num">3</span>
        <span class="local-flow__step-label">Start using Jarvis</span>
      </div>
    </div>

    <!-- Loading state — initial hardware/runtime detection -->
    <div v-if="localModels.loading.value" class="local-flow__loading">
      <div class="local-flow__spinner" />
      Detecting your hardware...
    </div>

    <!-- ============================================================ -->
    <!-- Layer 1 — Orchestrator (ADR 005 §B)                          -->
    <!-- ============================================================ -->
    <template v-else-if="mode === 'orchestrator'">

      <!-- probing / starting -->
      <template v-if="firstRun.status.value.state === 'probing' || firstRun.status.value.state === 'idle'">
        <div class="local-flow__downloading">
          <h3 class="local-flow__section-title">Detecting your hardware</h3>
          <p class="local-flow__section-desc">Picking the right local model for your machine.</p>
          <div class="local-flow__download-progress">
            <div class="local-flow__download-starting">
              <div class="local-flow__spinner" />
              <span>{{ firstRun.stageLabel.value || 'Starting…' }}</span>
            </div>
          </div>
        </div>
      </template>

      <!-- pulling_primary — foreground, blocking -->
      <template v-else-if="firstRun.status.value.state === 'pulling_primary'">
        <div class="local-flow__downloading">
          <div v-if="tierLabel" class="local-flow__hw-badge">{{ tierLabel }}</div>
          <h3 class="local-flow__section-title">
            Downloading {{ primaryEntry?.label ?? firstRun.status.value.primary_ollama_model ?? 'model' }}
          </h3>
          <p class="local-flow__section-desc">
            Jarvis runs entirely on your machine — this happens once.
          </p>

          <div class="local-flow__download-progress">
            <div class="local-flow__progress-bar">
              <div
                class="local-flow__progress-fill"
                :style="{ width: `${firstRun.status.value.primary.progress_pct}%` }"
              />
            </div>
            <div class="local-flow__progress-meta">
              <span class="local-flow__progress-status">{{ firstRun.status.value.primary.status }}</span>
              <span v-if="formattedPrimaryProgress" class="local-flow__progress-bytes">
                {{ formattedPrimaryProgress }}
                ·
                {{ firstRun.status.value.primary.progress_pct }}%
              </span>
            </div>
          </div>

          <button class="local-flow__link-btn local-flow__skip-btn" @click="handleSkip">
            I'll pick my own model later
          </button>
        </div>
      </template>

      <!-- chatReady but pipeline still has fallback / probe to do -->
      <!-- OR pipeline complete — both render the same ready surface; the   -->
      <!-- background indicator differs.                                    -->
      <template v-else-if="firstRun.chatReady.value">
        <div class="local-flow__ready">
          <div class="local-flow__ready-check">✓</div>
          <h3 class="local-flow__section-title">
            {{ firstRun.status.value.state === 'complete' ? 'Jarvis is ready' : 'Chat is ready' }}
          </h3>
          <p class="local-flow__ready-model">
            Using {{ primaryEntry?.label ?? firstRun.status.value.primary_ollama_model }} locally on this computer
          </p>

          <!-- Probe (running): live terminal-style readout above the CTA so
               the user sees the ongoing self-test. Once the probe finishes
               this unmounts and the post-run summary appears under the CTA
               instead — quieter, doesn't compete with "Jarvis is ready". -->
          <ChatModelProbePanel
            v-if="probe.running.value"
            variant="onboarding"
            externally-driven
          />

          <!-- Background fallback / probe indicator. Non-blocking. -->
          <div
            v-if="firstRun.status.value.state === 'pulling_fallback' || firstRun.status.value.state === 'running_probe'"
            class="local-flow__bg-indicator"
          >
            <div class="local-flow__spinner local-flow__spinner--small" />
            <span>{{ firstRun.stageLabel.value }}</span>
          </div>

          <div
            v-else-if="firstRun.status.value.state === 'complete' && firstRun.status.value.fallback_failed"
            class="local-flow__warn-indicator"
          >
            Background fallback model failed to download — chat works, but the runtime ladder is not fully primed.
          </div>

          <div class="local-flow__ready-details">
            <div class="local-flow__ready-detail">
              <span class="local-flow__ready-detail-label">Runtime</span>
              <span class="local-flow__ready-detail-value">Ollama</span>
            </div>
            <div class="local-flow__ready-detail">
              <span class="local-flow__ready-detail-label">Primary</span>
              <span class="local-flow__ready-detail-value">{{ primaryEntry?.label ?? '—' }}</span>
            </div>
            <div class="local-flow__ready-detail">
              <span class="local-flow__ready-detail-label">Fallback</span>
              <span class="local-flow__ready-detail-value">
                {{ fallbackEntry?.label ?? '—'
                  }}<template v-if="firstRun.status.value.state === 'pulling_fallback'"> · downloading</template>
              </span>
            </div>
            <div class="local-flow__ready-detail">
              <span class="local-flow__ready-detail-label">Privacy</span>
              <span class="local-flow__ready-detail-value">On-device</span>
            </div>
          </div>

          <button class="local-flow__primary-btn" @click="handleFinish">Open Jarvis</button>

          <!-- Post-run probe summary: collapsed single-line diagnostic.
               Sits below the CTA so it doesn't outshout the headline. -->
          <ChatModelProbePanel
            v-if="!probe.running.value && probe.status.value?.persisted"
            variant="onboarding"
            externally-driven
          />

          <div class="local-flow__ready-actions">
            <button class="local-flow__link-btn" @click="handleDownloadAnother">
              Download another model
            </button>
            <span class="local-flow__ready-sep">·</span>
            <span class="local-flow__ready-hint">Change later in Settings</span>
          </div>
        </div>
      </template>

      <!-- failed — fatal pre-marker error (primary pull errored) -->
      <template v-else-if="firstRun.status.value.state === 'failed'">
        <div class="local-flow__downloading">
          <h3 class="local-flow__section-title">First-run setup failed</h3>
          <p class="local-flow__section-desc">
            {{ firstRun.status.value.last_error ?? 'The primary model could not be downloaded.' }}
          </p>
          <div class="local-flow__install-actions">
            <button class="local-flow__secondary-btn" @click="handleRetry">Retry</button>
            <button class="local-flow__link-btn" @click="handleSkip">Pick a model manually</button>
          </div>
        </div>
      </template>

      <!-- skipped — fall through into manual picker (mode flips on skip) -->
    </template>

    <!-- ============================================================ -->
    <!-- Layer 2 — Manual picker (legacy)                              -->
    <!-- ============================================================ -->
    <template v-else-if="mode === 'manual'">

      <!-- runtime_missing — Ollama not bundled (dev mode) -->
      <template v-if="flow.state.value === 'runtime_missing'">
        <div class="local-flow__install">
          <h3 class="local-flow__section-title">Ollama isn't running yet</h3>
          <p class="local-flow__section-desc">
            <template v-if="flow.detectedOS.value === 'macos'">Open Ollama.app once after installing — Jarvis will detect it automatically.</template>
            <template v-else-if="flow.detectedOS.value === 'windows'">Install it from the official website. It runs automatically in the background after install.</template>
            <template v-else>Install it using the command below, then run <code>ollama serve</code> to start it.</template>
          </p>

          <div v-if="flow.hardwareSummary.value" class="local-flow__hw-badge">
            {{ flow.hardwareSummary.value.label }}
          </div>

          <div class="local-flow__platform">
            <button class="local-flow__install-btn" @click="flow.openOllamaDownload()">
              Open official Ollama download
              <svg class="local-flow__external-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
            </button>
          </div>

          <div v-if="localModels.runtime.value?.installed && !localModels.runtime.value?.running" class="local-flow__start-hint">
            <span class="local-flow__dot local-flow__dot--yellow" />
            <div>
              <p class="local-flow__start-hint-title">Ollama is installed but not running</p>
              <p class="local-flow__start-hint-desc">Open the Ollama app or run <code>ollama serve</code></p>
            </div>
          </div>

          <div class="local-flow__install-actions">
            <button class="local-flow__secondary-btn" @click="flow.checkAgain()">
              I've installed Ollama
            </button>
            <button class="local-flow__link-btn" @click="showManualSetup = !showManualSetup">
              Manual setup
            </button>
          </div>

          <div v-if="showManualSetup" class="local-flow__manual">
            <template v-if="flow.detectedOS.value === 'linux'">
              <p class="local-flow__manual-label">Run this command in your terminal:</p>
              <div class="local-flow__cmd-block">
                <code class="local-flow__cmd-text">{{ linuxCommand }}</code>
                <button class="local-flow__cmd-copy" @click="copyLinuxCommand" :title="copiedCommand ? 'Copied!' : 'Copy command'">
                  <template v-if="copiedCommand">✓</template>
                  <template v-else>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                  </template>
                </button>
              </div>
            </template>
            <p class="local-flow__manual-note">
              After installing, open Ollama once so it starts its local server at
              <code>http://localhost:11434</code>
            </p>
          </div>
        </div>
      </template>

      <!-- runtime_waiting -->
      <template v-else-if="flow.state.value === 'runtime_waiting'">
        <div class="local-flow__waiting">
          <h3 class="local-flow__section-title">Waiting for Ollama</h3>
          <p class="local-flow__section-desc">
            <template v-if="flow.detectedOS.value === 'macos'">Open <strong>Ollama.app</strong> once after installing — Jarvis will detect it automatically.</template>
            <template v-else-if="flow.detectedOS.value === 'windows'">Ollama should be running in the background. If not, try restarting it from the system tray.</template>
            <template v-else>Run <code>ollama serve</code> in your terminal, then click Check again.</template>
          </p>

          <div class="local-flow__status-box">
            <div class="local-flow__status-row">
              <div class="local-flow__spinner local-flow__spinner--small" />
              <span class="local-flow__status-label">Checking localhost:11434</span>
            </div>
            <span class="local-flow__status-value">Not detected yet</span>
          </div>

          <div class="local-flow__install-actions">
            <button class="local-flow__secondary-btn" @click="flow.checkAgain()">
              Check again
            </button>
            <button class="local-flow__link-btn" @click="flow.state.value = 'runtime_missing'">
              Back
            </button>
          </div>
        </div>
      </template>

      <!-- model_selection — manual picker -->
      <template v-else-if="flow.state.value === 'model_selection' || flow.state.value === 'runtime_ready'">
        <div class="local-flow__choose">
          <div v-if="flow.hardwareSummary.value" class="local-flow__hw-card">
            <div class="local-flow__hw-card-header">
              <span class="local-flow__dot local-flow__dot--green" />
              <span class="local-flow__hw-card-title">Your computer</span>
              <span class="local-flow__hw-card-version">
                <template v-if="localModels.runtime.value?.version">Ollama v{{ localModels.runtime.value.version }}</template>
              </span>
            </div>
            <p class="local-flow__hw-card-specs">{{ flow.hardwareSummary.value.label }}</p>
            <p class="local-flow__hw-card-rec">
              Runs comfortably: {{ flow.hardwareSummary.value.runsComfortably }}
            </p>
            <p v-if="flow.bestPicks.value.length" class="local-flow__hw-card-picks">
              Best picks: {{ flow.bestPicks.value.join(', ') }}
            </p>
          </div>

          <h3 class="local-flow__section-title">Choose a local model</h3>
          <p class="local-flow__section-desc">You can download more models later in Settings.</p>

          <div v-if="localModels.recommendedModels.value.length > 0" class="local-flow__model-list">
            <LocalModelCard
              v-for="m in localModels.recommendedModels.value.slice(0, 3)"
              :key="m.model_id"
              :model="m"
              :pulling="localModels.pulling.value === m.model_id"
              :progress="localModels.pulling.value === m.model_id ? localModels.pullProgress.value : null"
              @pull="handlePull"
              @select="handleSelect"
              @cancel="flow.cancelDownload()"
            />
          </div>

          <details v-if="localModels.catalog.value.length > 3" class="local-flow__all-models">
            <summary class="local-flow__show-all">
              Show all local models ({{ localModels.catalog.value.length }})
            </summary>
            <div class="local-flow__model-list local-flow__model-list--compact">
              <LocalModelCard
                v-for="m in localModels.catalog.value"
                :key="m.model_id"
                :model="m"
                :pulling="localModels.pulling.value === m.model_id"
                :progress="localModels.pulling.value === m.model_id ? localModels.pullProgress.value : null"
                compact
                @pull="handlePull"
                @select="handleSelect"
                @cancel="flow.cancelDownload()"
              />
            </div>
          </details>
        </div>
      </template>

      <!-- model_downloading -->
      <template v-else-if="flow.state.value === 'model_downloading'">
        <div class="local-flow__downloading">
          <h3 class="local-flow__section-title">
            Downloading {{ downloadingModel?.label ?? 'model' }}
          </h3>
          <p class="local-flow__section-desc">
            Jarvis is downloading the model to your computer.
          </p>

          <div class="local-flow__download-progress">
            <PullProgress
              v-if="localModels.pullProgress.value"
              :model-name="downloadingModel?.ollama_model ?? ''"
              :progress="localModels.pullProgress.value"
            />
            <div v-else class="local-flow__download-starting">
              <div class="local-flow__spinner" />
              <span>Preparing download...</span>
            </div>
          </div>

          <button class="local-flow__cancel-btn" @click="flow.cancelDownload()">
            Cancel download
          </button>
        </div>
      </template>

      <!-- model_ready / local_active -->
      <template v-else-if="flow.state.value === 'model_ready' || flow.state.value === 'local_active'">
        <div class="local-flow__ready">
          <div class="local-flow__ready-check">✓</div>
          <h3 class="local-flow__section-title">
            {{ probe.running.value ? 'Validating your setup…' : 'Jarvis is ready' }}
          </h3>
          <p class="local-flow__ready-model">
            {{ probe.running.value
              ? 'Running quick self-test — this takes 30–60 seconds.'
              : `Using ${readyModel?.label ?? 'local model'} locally on this computer` }}
          </p>

          <ChatModelProbePanel
            v-if="probe.running.value || probe.events.value.length > 0"
            variant="onboarding"
            externally-driven
          />

          <button
            class="local-flow__primary-btn"
            :disabled="probe.running.value"
            @click="handleFinish"
          >
            {{ probe.running.value ? 'Probing…' : 'Open Jarvis' }}
          </button>

          <div v-if="!probe.running.value" class="local-flow__ready-actions">
            <button class="local-flow__link-btn" @click="handleDownloadAnother">
              Download another model
            </button>
            <span class="local-flow__ready-sep">·</span>
            <span class="local-flow__ready-hint">Change later in Settings</span>
          </div>
        </div>
      </template>
    </template>

    <!-- Error -->
    <p v-if="localModels.error.value" class="local-flow__error">
      {{ localModels.error.value }}
    </p>
  </div>
</template>

<style scoped>
.local-flow {
  width: 100%;
}

.local-flow__title {
  font-size: 1.15rem;
  font-weight: 600;
  text-align: center;
  color: var(--text-primary);
  margin-bottom: 1.25rem;
}

/* ---- Step indicators ---- */
.local-flow__steps {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0;
  margin-bottom: 1.75rem;
  padding: 0 0.5rem;
}

.local-flow__step {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  opacity: 0.4;
  transition: opacity 0.3s;
}

.local-flow__step--active {
  opacity: 1;
}

.local-flow__step--done {
  opacity: 0.7;
}

.local-flow__step-num {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.7rem;
  font-weight: 700;
  border: 1.5px solid var(--border-default);
  color: var(--text-secondary);
  flex-shrink: 0;
}

.local-flow__step--active .local-flow__step-num {
  border-color: var(--neon-cyan);
  color: var(--neon-cyan);
  box-shadow: 0 0 8px var(--neon-cyan-08);
}

.local-flow__step--done .local-flow__step-num {
  border-color: #34d399;
  color: #34d399;
  background: rgba(52, 211, 153, 0.08);
}

.local-flow__step-label {
  font-size: 0.72rem;
  color: var(--text-secondary);
  white-space: nowrap;
}

.local-flow__step--active .local-flow__step-label {
  color: var(--text-primary);
  font-weight: 600;
}

.local-flow__step-line {
  width: 28px;
  height: 1px;
  background: var(--border-default);
  margin: 0 0.35rem;
  flex-shrink: 0;
  transition: background 0.3s;
}

.local-flow__step-line--done {
  background: #34d399;
}

/* ---- Shared section headings ---- */
.local-flow__section-title {
  font-size: 1rem;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 0.3rem;
  text-align: center;
}

.local-flow__section-desc {
  font-size: 0.82rem;
  color: var(--text-secondary);
  margin-bottom: 1.25rem;
  text-align: center;
}

/* ---- Loading ---- */
.local-flow__loading {
  text-align: center;
  padding: 2rem;
  color: var(--text-muted);
  font-size: 0.88rem;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.6rem;
}

.local-flow__spinner {
  width: 18px;
  height: 18px;
  border: 2px solid var(--border-default);
  border-top-color: var(--neon-cyan);
  border-radius: 50%;
  animation: local-spin 0.8s linear infinite;
}

.local-flow__spinner--small {
  width: 14px;
  height: 14px;
  border-width: 1.5px;
}

@keyframes local-spin {
  to { transform: rotate(360deg); }
}

/* ---- Step 1: Install / runtime_missing ---- */
.local-flow__install {
  text-align: center;
}

.local-flow__hw-badge {
  display: inline-block;
  font-size: 0.72rem;
  padding: 0.2rem 0.6rem;
  border-radius: 4px;
  background: var(--neon-cyan-08);
  color: var(--neon-cyan-60);
  border: 1px solid var(--neon-cyan-15);
  margin-bottom: 1.25rem;
}

.local-flow__platform {
  margin-bottom: 1rem;
}

.local-flow__install-btn {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.65rem 1.5rem;
  border: 1px solid var(--neon-cyan-30);
  border-radius: 8px;
  background: var(--neon-cyan-08);
  color: var(--neon-cyan);
  font-size: 0.88rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
}

.local-flow__install-btn:hover {
  background: rgba(2, 254, 255, 0.15);
  box-shadow: 0 0 16px var(--neon-cyan-08);
}

.local-flow__external-icon {
  opacity: 0.6;
}

.local-flow__helper-text {
  font-size: 0.78rem;
  color: var(--text-muted);
  margin-bottom: 1rem;
  line-height: 1.5;
}

/* ---- Installed-but-not-running hint ---- */
.local-flow__start-hint {
  display: flex;
  align-items: flex-start;
  gap: 0.65rem;
  margin: 0.75rem 0;
  padding: 0.65rem 0.85rem;
  border-radius: 8px;
  background: rgba(251, 191, 36, 0.04);
  border: 1px solid rgba(251, 191, 36, 0.15);
  text-align: left;
}

.local-flow__start-hint-title {
  font-size: 0.82rem;
  font-weight: 600;
  color: #fbbf24;
  margin-bottom: 0.1rem;
}

.local-flow__start-hint-desc {
  font-size: 0.78rem;
  color: var(--text-secondary);
}

.local-flow__start-hint-desc code {
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.72rem;
  background: var(--bg-elevated);
  padding: 0.1rem 0.35rem;
  border-radius: 3px;
  color: var(--neon-cyan);
}

.local-flow__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-top: 0.2rem;
  flex-shrink: 0;
}

.local-flow__dot--green {
  background: #34d399;
  box-shadow: 0 0 6px rgba(52, 211, 153, 0.4);
}

.local-flow__dot--yellow {
  background: #fbbf24;
  box-shadow: 0 0 6px rgba(251, 191, 36, 0.4);
}

/* ---- Secondary actions row ---- */
.local-flow__install-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  margin-top: 0.75rem;
  flex-wrap: wrap;
}

.local-flow__secondary-btn {
  padding: 0.3rem 0.65rem;
  border: 1px solid var(--border-default);
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 0.78rem;
  cursor: pointer;
  transition: all 0.15s;
}

.local-flow__secondary-btn:hover:not(:disabled) {
  border-color: var(--neon-cyan-30);
  color: var(--text-primary);
}

.local-flow__secondary-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.local-flow__link-btn {
  padding: 0.3rem 0.4rem;
  border: none;
  background: transparent;
  color: var(--text-muted);
  font-size: 0.75rem;
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.local-flow__link-btn:hover {
  color: var(--neon-cyan-60);
}

.local-flow__skip-btn {
  display: block;
  margin: 0.85rem auto 0;
  font-size: 0.78rem;
}

/* ---- Manual setup expandable ---- */
.local-flow__manual {
  margin-top: 0.85rem;
  padding: 0.75rem;
  border-radius: 6px;
  background: var(--bg-base);
  border: 1px solid var(--border-subtle);
  text-align: left;
}

.local-flow__manual-label {
  font-size: 0.78rem;
  color: var(--text-secondary);
  margin-bottom: 0.4rem;
}

.local-flow__manual-note {
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-top: 0.4rem;
  line-height: 1.5;
}

.local-flow__manual-note code {
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.7rem;
  background: var(--bg-elevated);
  padding: 0.1rem 0.3rem;
  border-radius: 3px;
  color: var(--neon-cyan);
}

.local-flow__cmd-block {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  padding: 0.45rem 0.65rem;
}

.local-flow__cmd-text {
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.75rem;
  color: var(--neon-cyan);
  user-select: all;
}

.local-flow__cmd-copy {
  padding: 0.2rem 0.35rem;
  border: 1px solid var(--border-default);
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 0.78rem;
  display: flex;
  align-items: center;
  transition: all 0.15s;
}

.local-flow__cmd-copy:hover {
  border-color: var(--neon-cyan-30);
  color: var(--neon-cyan);
}

/* ---- Step 1A: Waiting / runtime_waiting ---- */
.local-flow__waiting {
  text-align: center;
}

.local-flow__status-box {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.65rem 0.85rem;
  border-radius: 8px;
  border: 1px solid var(--border-default);
  background: var(--bg-base);
  margin-bottom: 0.85rem;
}

.local-flow__status-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.local-flow__status-label {
  font-size: 0.78rem;
  color: var(--text-secondary);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

.local-flow__status-value {
  font-size: 0.78rem;
  color: var(--text-muted);
}

/* ---- Step 2: Choose model / model_selection ---- */
.local-flow__choose {
  /* container */
}

.local-flow__hw-card {
  padding: 0.65rem 0.85rem;
  border-radius: 8px;
  border: 1px solid rgba(52, 211, 153, 0.15);
  background: rgba(52, 211, 153, 0.03);
  margin-bottom: 1.25rem;
}

.local-flow__hw-card-header {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  margin-bottom: 0.2rem;
}

.local-flow__hw-card-title {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--text-primary);
}

.local-flow__hw-card-version {
  margin-left: auto;
  font-size: 0.68rem;
  color: #34d399;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  white-space: nowrap;
}

.local-flow__hw-card-specs {
  font-size: 0.78rem;
  color: var(--text-secondary);
}

.local-flow__hw-card-rec {
  font-size: 0.72rem;
  color: var(--text-muted);
  margin-top: 0.1rem;
}

.local-flow__hw-card-picks {
  font-size: 0.72rem;
  color: #34d399;
  margin-top: 0.1rem;
  opacity: 0.85;
}

.local-flow__model-list {
  display: flex;
  flex-direction: column;
  gap: 0.65rem;
}

.local-flow__model-list--compact {
  margin-top: 0.65rem;
}

.local-flow__all-models {
  margin-top: 0.85rem;
}

.local-flow__show-all {
  font-size: 0.82rem;
  color: var(--text-secondary);
  cursor: pointer;
  padding: 0.45rem;
  user-select: none;
  list-style: none;
  text-align: center;
  border: 1px dashed var(--border-default);
  border-radius: 6px;
  transition: all 0.15s;
}

.local-flow__show-all:hover {
  border-color: var(--neon-cyan-30);
  color: var(--text-primary);
}

.local-flow__show-all::-webkit-details-marker {
  display: none;
}

/* ---- Step 2A: Downloading / model_downloading ---- */
.local-flow__downloading {
  text-align: center;
}

.local-flow__download-progress {
  margin-top: 0.5rem;
  padding: 1rem;
  border-radius: 8px;
  border: 1px solid var(--border-default);
  background: var(--bg-base);
  text-align: left;
}

.local-flow__download-starting {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  padding: 1rem;
  color: var(--text-muted);
  font-size: 0.85rem;
}

.local-flow__cancel-btn {
  display: block;
  width: 100%;
  margin-top: 1rem;
  padding: 0.5rem;
  background: transparent;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  color: var(--text-muted);
  font-size: 0.8rem;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}

.local-flow__cancel-btn:hover {
  color: rgba(248, 113, 113, 0.9);
  border-color: rgba(248, 113, 113, 0.3);
}

/* ---- Orchestrator pull-progress bar (ADR 005 §B) ---- */
.local-flow__progress-bar {
  position: relative;
  width: 100%;
  height: 8px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  overflow: hidden;
}

.local-flow__progress-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--neon-cyan-30), var(--neon-cyan));
  transition: width 0.4s ease;
  box-shadow: 0 0 8px var(--neon-cyan-08);
}

.local-flow__progress-meta {
  display: flex;
  justify-content: space-between;
  margin-top: 0.5rem;
  font-size: 0.72rem;
  color: var(--text-muted);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

.local-flow__progress-status {
  text-transform: lowercase;
}

.local-flow__progress-bytes {
  text-align: right;
}

/* ---- Background fallback / probe indicator ---- */
.local-flow__bg-indicator {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  margin: 0.75rem 0;
  padding: 0.5rem 0.85rem;
  border-radius: 6px;
  background: var(--neon-cyan-08);
  border: 1px solid var(--neon-cyan-15);
  font-size: 0.78rem;
  color: var(--neon-cyan-60);
}

.local-flow__warn-indicator {
  margin: 0.75rem 0;
  padding: 0.55rem 0.85rem;
  border-radius: 6px;
  background: rgba(251, 191, 36, 0.06);
  border: 1px solid rgba(251, 191, 36, 0.18);
  font-size: 0.78rem;
  color: #fbbf24;
  text-align: left;
  line-height: 1.4;
}

/* ---- Step 3: Ready / model_ready ---- */
.local-flow__ready {
  text-align: center;
  padding: 0.5rem 0;
}

.local-flow__ready-check {
  width: 40px;
  height: 40px;
  margin: 0 auto 0.85rem;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.2rem;
  color: #34d399;
  background: rgba(52, 211, 153, 0.08);
  border: 1.5px solid rgba(52, 211, 153, 0.3);
}

.local-flow__ready-model {
  font-size: 0.88rem;
  color: var(--text-secondary);
  margin-bottom: 1.25rem;
}

.local-flow__ready-details {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.5rem;
  margin-bottom: 1.5rem;
  padding: 0.75rem;
  border-radius: 8px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-base);
  text-align: left;
}

.local-flow__ready-detail {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
}

.local-flow__ready-detail-label {
  font-size: 0.68rem;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.local-flow__ready-detail-value {
  font-size: 0.82rem;
  color: var(--text-primary);
  font-weight: 500;
}

.local-flow__primary-btn {
  display: block;
  width: 100%;
  padding: 0.7rem 1.25rem;
  background: var(--neon-cyan, #02feff);
  color: var(--bg-deep, #06080d);
  border: none;
  border-radius: 6px;
  font-weight: 700;
  font-size: 0.9rem;
  cursor: pointer;
  transition: all 0.2s;
  letter-spacing: 0.02em;
}

.local-flow__primary-btn:hover {
  box-shadow: 0 0 20px rgba(2, 254, 255, 0.25), 0 0 4px rgba(2, 254, 255, 0.4);
}

.local-flow__ready-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  margin-top: 0.85rem;
}

.local-flow__ready-sep {
  color: var(--text-muted);
  font-size: 0.72rem;
}

.local-flow__ready-hint {
  font-size: 0.75rem;
  color: var(--text-muted);
}

/* ---- Error ---- */
.local-flow__error {
  margin-top: 0.65rem;
  font-size: 0.8rem;
  color: rgba(248, 113, 113, 0.9);
  background: rgba(248, 113, 113, 0.06);
  border: 1px solid rgba(248, 113, 113, 0.2);
  border-radius: 6px;
  padding: 0.45rem 0.7rem;
}

</style>
