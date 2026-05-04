<script setup lang="ts">
import type { ChatModelProbeEvidence, ChatModelProbeRerunReason } from '~/types'

const props = withDefaults(defineProps<{
  /** Compact = onboarding (auto-runs, minimal chrome). Full = settings. */
  variant?: 'onboarding' | 'settings'
  /** When true, parent has already triggered runProbe — don't re-trigger. */
  externallyDriven?: boolean
}>(), {
  variant: 'settings',
  externallyDriven: false,
})

const emit = defineEmits<{
  (e: 'completed', recommended: string | null): void
}>()

const probe = useChatModelProbe()
const localModels = useLocalModels()

const isOnboarding = computed(() => props.variant === 'onboarding')

const verdictLabel: Record<string, string> = {
  pass: 'Pass',
  fail_hardware_fit: 'Too large for available RAM',
  fail_correctness: 'Leaks chain-of-thought',
  fail_speed: 'Too slow',
  fail_unreachable: 'Unreachable',
}

const rerunReasonLabel: Record<ChatModelProbeRerunReason, string> = {
  no_prior_probe: 'never run',
  ollama_version_changed: 'Ollama version changed',
  platform_changed: 'OS changed',
  catalog_added_models: 'new model in catalog',
  fresh: 'up to date',
}

onMounted(async () => {
  if (!probe.status.value) {
    await probe.fetchStatus()
  }
})

async function handleRunProbe() {
  const result = await probe.runProbe()
  emit('completed', result?.recommended_model ?? null)
}

async function handleClearOverride() {
  await probe.setOverride(null)
}

async function handleSetOverride(event: Event) {
  const target = event.target as HTMLSelectElement
  const value = target.value || null
  await probe.setOverride(value)
}

const installedCandidates = computed(() =>
  localModels.catalog.value.filter(m => m.installed),
)

const persistedRecord = computed(() => probe.status.value?.persisted ?? null)

const hasResult = computed(() => persistedRecord.value !== null)

// Onboarding verdict pulled out of the dominant verdict-card and re-rendered
// as a single discreet line under the "Open Jarvis" CTA. The user's chat
// model is already pinned by the orchestrator's primary pull, so the probe
// is supplementary — it shouldn't compete with the "Jarvis is ready" headline.
const onboardingSummaryText = computed(() => {
  const r = persistedRecord.value
  if (!r) return ''
  const total = r.candidates_evaluated?.length ?? 0
  const passed = r.candidates_evaluated?.filter(c => c.verdict === 'pass').length ?? 0
  if (r.recommended_model) {
    return `${r.recommended_model} passed · ${passed}/${total} candidates`
  }
  if (r.safe_fallback_used) {
    return `0/${total} outperformed default — using safe fallback`
  }
  return `${passed}/${total} candidates passed`
})

const onboardingSummaryMeta = computed(() => {
  const r = persistedRecord.value
  if (!r) return ''
  const parts: string[] = []
  if (r.timestamp_utc) {
    parts.push(new Date(r.timestamp_utc).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    }))
  }
  if (r.ollama_version) parts.push(`Ollama ${r.ollama_version}`)
  if (r.ram_gb) parts.push(`${r.ram_gb} GB`)
  return parts.join(' · ')
})

function evidenceLine(e: ChatModelProbeEvidence): string {
  const parts: string[] = []
  if (e.warm_short_total_ms != null) {
    parts.push(`warm-short ${Math.round(e.warm_short_total_ms)}ms`)
  }
  if (e.realistic_tps != null) {
    parts.push(`${e.realistic_tps.toFixed(1)} TPS`)
  }
  if (e.hardware_fit_bytes != null) {
    parts.push(`${(e.hardware_fit_bytes / (1024 ** 3)).toFixed(1)} GB`)
  }
  if (e.correctness_response) {
    const trimmed = e.correctness_response.length > 60
      ? `${e.correctness_response.slice(0, 60)}…`
      : e.correctness_response
    parts.push(`response: "${trimmed}"`)
  }
  if (e.error_message) parts.push(e.error_message)
  return parts.join(' · ')
}
</script>

<template>
  <div class="probe-panel" :class="{ 'probe-panel--onboarding': isOnboarding }">
    <div v-if="!isOnboarding" class="probe-panel__header">
      <h3 class="probe-panel__title">Chat-model self-test</h3>
      <p class="probe-panel__subtitle">
        Validates the local model produces clean output, fits in RAM, and feels snappy.
      </p>
    </div>

    <!-- Running / progress -->
    <div v-if="probe.running.value" class="probe-panel__running">
      <div class="probe-panel__spinner" />
      <div class="probe-panel__running-info">
        <p class="probe-panel__running-label">{{ probe.progressLabel.value ?? 'Probing…' }}</p>
        <p class="probe-panel__running-hint">This usually takes 30–60 seconds.</p>
      </div>
      <button
        v-if="!isOnboarding"
        class="probe-panel__cancel-btn"
        @click="probe.cancelProbe()"
      >Cancel</button>
    </div>

    <!-- Live evidence — only WHILE running. Stops accumulating once the
         probe finishes; the post-run summary supersedes it. -->
    <div
      v-if="probe.running.value && isOnboarding && probe.events.value.length"
      class="probe-panel__live-events"
    >
      <p
        v-for="(event, idx) in probe.events.value"
        :key="idx"
        class="probe-panel__live-event"
      >
        <template v-if="event.event === 'candidate_start'">
          <span class="probe-panel__live-arrow">→</span>
          <span class="probe-panel__live-model">{{ event.model }}</span>
        </template>
        <template v-else-if="event.event === 'candidate_evidence'">
          <Icon
            :name="event.evidence.verdict === 'pass' ? 'ph:check-bold' : 'ph:x-bold'"
            :class="event.evidence.verdict === 'pass' ? 'probe-panel__live-pass' : 'probe-panel__live-fail'"
          />
          <span class="probe-panel__live-model">{{ event.evidence.model }}</span>
          <span class="probe-panel__live-sep">—</span>
          <span class="probe-panel__live-verdict">{{ verdictLabel[event.evidence.verdict] ?? event.evidence.verdict }}</span>
        </template>
      </p>
    </div>

    <!-- Onboarding post-run summary: a single quiet line under the CTA.
         Click to expand the candidate evidence list. The user's chat model
         is already pinned by the orchestrator; this is supplementary diagnostics. -->
    <details
      v-if="!probe.running.value && isOnboarding && hasResult"
      class="probe-panel__summary"
    >
      <summary class="probe-panel__summary-row">
        <Icon name="ph:caret-right" class="probe-panel__summary-caret" aria-hidden="true" />
        <span class="probe-panel__summary-label">Probe</span>
        <span class="probe-panel__summary-text">{{ onboardingSummaryText }}</span>
        <span v-if="onboardingSummaryMeta" class="probe-panel__summary-meta">{{ onboardingSummaryMeta }}</span>
      </summary>
      <ul
        v-if="persistedRecord?.candidates_evaluated.length"
        class="probe-panel__evidence-list probe-panel__evidence-list--onboarding"
      >
        <li
          v-for="ev in persistedRecord.candidates_evaluated"
          :key="ev.model"
          class="probe-panel__evidence-line"
        >
          <Icon
            :name="ev.verdict === 'pass' ? 'ph:check-bold' : 'ph:x-bold'"
            :class="ev.verdict === 'pass' ? 'probe-panel__live-pass' : 'probe-panel__live-fail'"
          />
          <span class="probe-panel__evidence-model">{{ ev.model }}</span>
          <span class="probe-panel__evidence-sep">·</span>
          <span class="probe-panel__evidence-verdict">{{ verdictLabel[ev.verdict] ?? ev.verdict }}</span>
          <span v-if="evidenceLine(ev)" class="probe-panel__evidence-detail">{{ evidenceLine(ev) }}</span>
        </li>
      </ul>
    </details>

    <!-- Settings variant — verdict card + evidence + override + actions. -->
    <template v-if="!probe.running.value && !isOnboarding">
      <div v-if="hasResult" class="probe-panel__verdict-card" :class="{
        'probe-panel__verdict-card--pass': persistedRecord?.recommended_model,
        'probe-panel__verdict-card--fallback': persistedRecord?.safe_fallback_used,
      }">
        <div class="probe-panel__verdict-row">
          <span class="probe-panel__verdict-label">Recommended</span>
          <span class="probe-panel__verdict-model">
            {{ persistedRecord?.recommended_model ?? 'No candidate passed' }}
          </span>
        </div>
        <div v-if="persistedRecord?.user_override" class="probe-panel__verdict-row probe-panel__verdict-row--override">
          <span class="probe-panel__verdict-label">Override active</span>
          <span class="probe-panel__verdict-model">{{ persistedRecord.user_override }}</span>
          <button class="probe-panel__inline-btn" @click="handleClearOverride">Clear</button>
        </div>
        <div class="probe-panel__verdict-meta">
          <span v-if="persistedRecord?.timestamp_utc">
            Last run {{ new Date(persistedRecord.timestamp_utc).toLocaleString() }}
          </span>
          <span v-if="persistedRecord?.ollama_version">
            · Ollama v{{ persistedRecord.ollama_version }}
          </span>
          <span v-if="persistedRecord?.ram_gb">
            · {{ persistedRecord.ram_gb }} GB RAM
          </span>
        </div>
      </div>

      <div v-else class="probe-panel__empty">
        <p class="probe-panel__empty-text">No probe has run yet on this machine.</p>
      </div>

      <div v-if="probe.needsRerun.value" class="probe-panel__rerun-banner">
        <Icon name="ph:warning-fill" class="icon--md icon--warning probe-panel__rerun-icon" />
        <div class="probe-panel__rerun-info">
          <p class="probe-panel__rerun-title">A re-test is recommended</p>
          <p class="probe-panel__rerun-reason">
            {{ rerunReasonLabel[probe.rerunReason.value ?? 'fresh'] }}
          </p>
        </div>
      </div>

      <div v-if="hasResult && persistedRecord?.candidates_evaluated.length" class="probe-panel__evidence">
        <h4 class="probe-panel__evidence-title">Candidates evaluated</h4>
        <ul class="probe-panel__evidence-list">
          <li
            v-for="ev in persistedRecord.candidates_evaluated"
            :key="ev.model"
            class="probe-panel__evidence-row"
            :class="{
              'probe-panel__evidence-row--pass': ev.verdict === 'pass',
              'probe-panel__evidence-row--fail': ev.verdict !== 'pass',
            }"
          >
            <span class="probe-panel__evidence-model">{{ ev.model }}</span>
            <span class="probe-panel__evidence-verdict">{{ verdictLabel[ev.verdict] ?? ev.verdict }}</span>
            <span v-if="evidenceLine(ev)" class="probe-panel__evidence-detail">{{ evidenceLine(ev) }}</span>
          </li>
        </ul>
      </div>

      <div v-if="hasResult && installedCandidates.length > 1" class="probe-panel__override">
        <label class="probe-panel__override-label">Override recommendation</label>
        <select
          class="probe-panel__override-select"
          :value="persistedRecord?.user_override ?? ''"
          @change="handleSetOverride"
        >
          <option value="">Use recommendation</option>
          <option
            v-for="m in installedCandidates"
            :key="m.ollama_model"
            :value="m.ollama_model"
          >{{ m.label }} ({{ m.ollama_model }})</option>
        </select>
      </div>

      <div class="probe-panel__actions">
        <button
          class="probe-panel__primary-btn"
          :disabled="probe.running.value"
          @click="handleRunProbe"
        >
          {{ hasResult ? 'Re-run probe' : 'Run probe' }}
        </button>
      </div>
    </template>

    <p v-if="probe.error.value" class="probe-panel__error">
      {{ probe.error.value }}
    </p>
  </div>
</template>

<style scoped>
.probe-panel {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  margin-top: 0.85rem;
  /* The onboarding card centers its prose; the probe panel is a technical
     readout and must not inherit that — undo here so descendants ground left. */
  text-align: left;
}

.probe-panel--onboarding {
  margin-top: 0;
  gap: 0.5rem;
}

.probe-panel__header {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.probe-panel__title {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}

.probe-panel__subtitle {
  font-size: 0.78rem;
  color: var(--text-secondary);
}

/* Running */
.probe-panel__running {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  padding: 0.75rem 0.85rem;
  border-radius: 8px;
  border: 1px solid var(--neon-cyan-15);
  background: var(--neon-cyan-08);
}

.probe-panel__spinner {
  width: 16px;
  height: 16px;
  border: 2px solid var(--border-default);
  border-top-color: var(--neon-cyan);
  border-radius: 50%;
  animation: probe-spin 0.8s linear infinite;
  flex-shrink: 0;
}

@keyframes probe-spin {
  to { transform: rotate(360deg); }
}

.probe-panel__running-info {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
}

.probe-panel__running-label {
  font-size: 0.85rem;
  color: var(--text-primary);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

.probe-panel__running-hint {
  font-size: 0.72rem;
  color: var(--text-muted);
}

.probe-panel__cancel-btn {
  padding: 0.3rem 0.65rem;
  border: 1px solid var(--border-default);
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
  font-size: 0.78rem;
  cursor: pointer;
  transition: all 0.15s;
}

.probe-panel__cancel-btn:hover {
  color: rgba(248, 113, 113, 0.9);
  border-color: rgba(248, 113, 113, 0.3);
}

/* Verdict */
.probe-panel__verdict-card {
  padding: 0.75rem 0.85rem;
  border-radius: 8px;
  border: 1px solid var(--border-default);
  background: var(--bg-base);
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.probe-panel__verdict-card--pass {
  border-color: rgba(52, 211, 153, 0.25);
  background: rgba(52, 211, 153, 0.04);
}

.probe-panel__verdict-card--fallback {
  border-color: rgba(251, 191, 36, 0.25);
  background: rgba(251, 191, 36, 0.04);
}

.probe-panel__verdict-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.probe-panel__verdict-row--override {
  padding-top: 0.35rem;
  border-top: 1px dashed var(--border-subtle);
}

.probe-panel__verdict-label {
  font-size: 0.7rem;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  min-width: 92px;
}

.probe-panel__verdict-model {
  font-size: 0.88rem;
  font-weight: 600;
  color: var(--text-primary);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

.probe-panel__inline-btn {
  margin-left: auto;
  padding: 0.2rem 0.5rem;
  border: 1px solid var(--border-default);
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 0.72rem;
  cursor: pointer;
  transition: all 0.15s;
}

.probe-panel__inline-btn:hover {
  border-color: var(--neon-cyan-30);
  color: var(--neon-cyan);
}

.probe-panel__verdict-meta {
  font-size: 0.7rem;
  color: var(--text-muted);
}

/* Empty */
.probe-panel__empty {
  padding: 0.65rem 0.85rem;
  border-radius: 6px;
  border: 1px dashed var(--border-default);
  background: var(--bg-base);
}

.probe-panel__empty-text {
  font-size: 0.82rem;
  color: var(--text-secondary);
  margin: 0;
}

/* Rerun banner */
.probe-panel__rerun-banner {
  display: flex;
  align-items: flex-start;
  gap: 0.6rem;
  padding: 0.6rem 0.75rem;
  border-radius: 6px;
  background: rgba(251, 191, 36, 0.06);
  border: 1px solid rgba(251, 191, 36, 0.25);
}

.probe-panel__rerun-icon {
  font-size: 0.95rem;
}

.probe-panel__rerun-info {
  flex: 1;
}

.probe-panel__rerun-title {
  font-size: 0.82rem;
  font-weight: 600;
  color: #fbbf24;
}

.probe-panel__rerun-reason {
  font-size: 0.74rem;
  color: var(--text-secondary);
}

/* Evidence list */
.probe-panel__evidence {
  margin-top: 0.5rem;
}

.probe-panel__evidence-title {
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 0.4rem;
}

.probe-panel__evidence-list {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  list-style: none;
  padding: 0;
  margin: 0;
}

.probe-panel__evidence-row {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0.4rem 0.6rem;
  padding: 0.45rem 0.65rem;
  border-radius: 6px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-base);
  font-size: 0.78rem;
}

.probe-panel__evidence-row--pass {
  border-color: rgba(52, 211, 153, 0.2);
  background: rgba(52, 211, 153, 0.03);
}

.probe-panel__evidence-row--fail {
  border-color: rgba(248, 113, 113, 0.18);
  background: rgba(248, 113, 113, 0.03);
}

.probe-panel__evidence-model {
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  color: var(--text-primary);
}

.probe-panel__evidence-verdict {
  font-size: 0.72rem;
  color: var(--text-muted);
  text-align: right;
}

.probe-panel__evidence-detail {
  grid-column: 1 / -1;
  font-size: 0.7rem;
  color: var(--text-muted);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

/* Live events (onboarding, during run only) — terminal-style readout
   with a left rule reading like a code-fence so the diagnostic block
   reads as console output, not body prose. */
.probe-panel__live-events {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  font-size: 0.76rem;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  color: var(--text-secondary);
  padding: 0.55rem 0.75rem 0.55rem 0.85rem;
  border-radius: 6px;
  background: rgba(2, 254, 255, 0.025);
  border: 1px solid var(--neon-cyan-15);
  border-left: 2px solid var(--neon-cyan-30);
  max-height: 160px;
  overflow-y: auto;
  text-align: left;
}

.probe-panel__live-event {
  margin: 0;
  line-height: 1.55;
  display: flex;
  align-items: baseline;
  gap: 0.45rem;
  white-space: nowrap;
}

.probe-panel__live-arrow {
  color: var(--neon-cyan-60);
  width: 0.85em;
  display: inline-block;
}

.probe-panel__live-model {
  color: var(--text-primary);
}

.probe-panel__live-sep {
  color: var(--text-muted);
  opacity: 0.5;
}

.probe-panel__live-verdict {
  color: var(--text-muted);
  font-style: italic;
  font-size: 0.72rem;
}

.probe-panel__live-pass {
  color: #34d399;
  font-size: 0.85em;
  flex-shrink: 0;
}

.probe-panel__live-fail {
  color: rgba(248, 113, 113, 0.85);
  font-size: 0.85em;
  flex-shrink: 0;
}

/* Onboarding post-run summary — single line under the CTA, expandable.
   Quiet by default so it doesn't compete with "Jarvis is ready". */
.probe-panel__summary {
  border: none;
  margin-top: 0.15rem;
  text-align: left;
}

.probe-panel__summary-row {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  padding: 0.45rem 0.7rem 0.45rem 0.6rem;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--bg-base);
  cursor: pointer;
  font-size: 0.74rem;
  color: var(--text-secondary);
  list-style: none;
  user-select: none;
  transition: background 0.15s, border-color 0.15s;
}

.probe-panel__summary-row::-webkit-details-marker { display: none; }
.probe-panel__summary-row::marker { content: ''; }

.probe-panel__summary-row:hover {
  background: var(--neon-cyan-08);
  border-color: var(--neon-cyan-15);
}

.probe-panel__summary-caret {
  color: var(--neon-cyan-60);
  font-size: 0.7rem;
  transition: transform 0.15s ease;
  display: inline-block;
  width: 0.7em;
}

.probe-panel__summary[open] .probe-panel__summary-caret {
  transform: rotate(90deg);
}

.probe-panel__summary-label {
  font-size: 0.6rem;
  letter-spacing: 0.14em;
  font-weight: 700;
  text-transform: uppercase;
  color: var(--neon-cyan-60);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

.probe-panel__summary-text {
  flex: 1;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  color: var(--text-secondary);
  font-size: 0.74rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.probe-panel__summary-meta {
  font-size: 0.66rem;
  color: var(--text-muted);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  letter-spacing: 0.01em;
  white-space: nowrap;
}

.probe-panel__evidence-list--onboarding {
  margin: 0.55rem 0 0;
  padding: 0.55rem 0 0.55rem 0.85rem;
  border-left: 2px solid var(--neon-cyan-15);
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.72rem;
}

.probe-panel__evidence-line {
  display: grid;
  grid-template-columns: 0.85em auto auto 1fr;
  align-items: baseline;
  gap: 0.45rem;
  color: var(--text-secondary);
  line-height: 1.55;
}

.probe-panel__evidence-sep {
  color: var(--text-muted);
  opacity: 0.5;
}

.probe-panel__evidence-list--onboarding .probe-panel__evidence-model {
  color: var(--text-primary);
}

.probe-panel__evidence-list--onboarding .probe-panel__evidence-verdict {
  color: var(--text-muted);
  font-style: italic;
}

.probe-panel__evidence-list--onboarding .probe-panel__evidence-detail {
  grid-column: 1 / -1;
  margin-left: 1.3rem;
  color: var(--text-muted);
  font-size: 0.68rem;
  opacity: 0.75;
}

/* Override */
.probe-panel__override {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  margin-top: 0.4rem;
}

.probe-panel__override-label {
  font-size: 0.74rem;
  color: var(--text-secondary);
  font-weight: 500;
}

.probe-panel__override-select {
  padding: 0.4rem 0.55rem;
  border: 1px solid var(--border-default);
  border-radius: 6px;
  background: var(--bg-base);
  color: var(--text-primary);
  font-size: 0.82rem;
  cursor: pointer;
}

.probe-panel__override-select:focus {
  outline: none;
  border-color: var(--neon-cyan-30);
}

/* Actions */
.probe-panel__actions {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.probe-panel__primary-btn {
  padding: 0.45rem 0.95rem;
  border: 1px solid var(--neon-cyan-30);
  border-radius: 6px;
  background: var(--neon-cyan-08);
  color: var(--neon-cyan);
  font-size: 0.82rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}

.probe-panel__primary-btn:hover:not(:disabled) {
  background: rgba(2, 254, 255, 0.15);
  box-shadow: 0 0 12px var(--neon-cyan-08);
}

.probe-panel__primary-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.probe-panel__error {
  font-size: 0.78rem;
  color: rgba(248, 113, 113, 0.9);
  background: rgba(248, 113, 113, 0.06);
  border: 1px solid rgba(248, 113, 113, 0.2);
  border-radius: 6px;
  padding: 0.45rem 0.7rem;
  margin: 0;
}
</style>
