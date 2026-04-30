<template>
  <header class="status-bar">
    <span class="status-bar__label" :class="{ 'status-bar__label--hidden': chatActive }">Jarvis</span>

    <nav class="status-bar__nav" :class="{ 'status-bar__nav--open': menuOpen }">
      <NuxtLink to="/main" class="status-bar__link" @click="menuOpen = false">Chat</NuxtLink>
      <NuxtLink to="/memory" class="status-bar__link" @click="menuOpen = false">Memory</NuxtLink>
      <NuxtLink to="/graph" class="status-bar__link" @click="menuOpen = false">Graph</NuxtLink>
      <NuxtLink to="/specialists" class="status-bar__link" @click="menuOpen = false">Specialists</NuxtLink>
      <NuxtLink to="/settings" class="status-bar__link" @click="menuOpen = false">Settings</NuxtLink>
    </nav>

    <!-- Backdrop to close menu when tapping outside -->
    <div
      v-if="menuOpen"
      class="status-bar__backdrop"
      @click="menuOpen = false"
    />

    <span
      v-if="ingest.hasActivity.value"
      class="status-bar__ingest"
      :class="{ 'status-bar__ingest--idle': ingest.activeCount.value === 0 }"
      :title="ingestTooltip"
    >
      <span class="status-bar__ingest-spinner" v-if="ingest.activeCount.value > 0" />
      <span class="status-bar__ingest-check" v-else>✓</span>
      <span class="status-bar__ingest-text">{{ ingestText }}</span>
      <span
        v-if="ingest.activeCount.value > 0 && ingest.totalBytes.value > 0"
        class="status-bar__ingest-bar"
        :aria-label="`Upload progress ${ingest.overallPercent.value}%`"
      >
        <span
          class="status-bar__ingest-bar-fill"
          :style="{ width: ingest.overallPercent.value + '%' }"
        />
      </span>
    </span>

    <span
      v-if="reindex.visible.value"
      class="status-bar__reindex"
      :class="{
        'status-bar__reindex--failed': reindex.status.value.state === 'failed',
        'status-bar__reindex--idle': reindex.status.value.state === 'idle',
      }"
      :title="reindexTooltip"
    >
      <span
        v-if="reindex.status.value.state === 'running'"
        class="status-bar__reindex-spinner"
      />
      <span
        v-else-if="reindex.status.value.state === 'failed'"
        class="status-bar__reindex-icon"
      >!</span>
      <span v-else class="status-bar__reindex-icon">✓</span>
      <span class="status-bar__reindex-text">{{ reindexText }}</span>
      <button
        v-if="reindex.status.value.state === 'failed'"
        class="status-bar__reindex-retry"
        @click="reindex.retry"
      >Retry</button>
      <button
        class="status-bar__reindex-dismiss"
        aria-label="Dismiss reindex notice"
        @click="reindex.dismiss"
      >×</button>
    </span>

    <span
      class="status-bar__indicator"
      :class="backendStatus"
    >
      {{ statusText }}
    </span>

    <button
      class="status-bar__hamburger"
      :class="{ 'status-bar__hamburger--open': menuOpen }"
      aria-label="Toggle navigation"
      @click="menuOpen = !menuOpen"
    >
      <span class="status-bar__hamburger-line" />
      <span class="status-bar__hamburger-line" />
      <span class="status-bar__hamburger-line" />
    </button>
  </header>
</template>

<script setup lang="ts">
import { useLocalModels } from '~/composables/useLocalModels'
import { useIngestStatus } from '~/composables/useIngestStatus'
import { useReindexStatus } from '~/composables/useReindexStatus'

const { backendStatus, chatActive } = useAppState()
const menuOpen = ref(false)
const localModels = useLocalModels()
const ingest = useIngestStatus()
const reindex = useReindexStatus()

const reindexText = computed(() => {
  const s = reindex.status.value
  if (s.state === 'running') {
    if (s.total > 0) return `Indexing ${s.scanned} / ${s.total}`
    return 'Indexing your vault…'
  }
  if (s.state === 'failed') return 'Index failed'
  if (s.state === 'idle' && s.last_run_count > 0) {
    return `Indexed ${s.last_run_count}`
  }
  return 'Indexing'
})

const reindexTooltip = computed(() => {
  const s = reindex.status.value
  if (s.state === 'failed' && s.last_error) {
    return `Reindex failed: ${s.last_error}`
  }
  if (s.state === 'running') {
    return `Embedding ${s.scanned} of ${s.total} notes (${s.progress_pct}%)`
  }
  return ''
})

const route = useRoute()
watch(() => route.path, () => {
  menuOpen.value = false
})

const statusText = computed(() => {
  const base = backendStatus.value === 'online' ? 'Alive' : backendStatus.value === 'offline' ? 'Offline' : 'Checking...'
  if (localModels.activeModel.value) {
    const modelName = localModels.activeModel.value.label
    const ollamaOk = localModels.runtime.value?.reachable && !localModels.ollamaDown.value
    return `${base} · ${modelName} (local) · ${ollamaOk ? '🟢' : '🔴'}`
  }
  return base
})

const ingestText = computed(() => {
  if (ingest.activeCount.value > 0) return ingest.label.value
  if (ingest.recent.value.length > 0) {
    const failed = ingest.recent.value.filter((r) => r.status === 'failed').length
    if (failed > 0) return `${failed} failed`
    return 'Done'
  }
  return ''
})

const ingestTooltip = computed(() => {
  const parts: string[] = []
  for (const u of ingest.activeUploads.value) {
    const pct = u.size > 0 ? Math.floor((u.uploaded / u.size) * 100) : 0
    if (u.state === 'uploading') {
      parts.push(`• ${u.name} — uploading ${pct}%`)
    } else {
      parts.push(`• ${u.name} — ${u.stage || 'processing'}`)
    }
  }
  // Server-only jobs (started elsewhere)
  const localNames = new Set(ingest.activeUploads.value.map(u => u.name))
  for (const j of ingest.active.value) {
    if (localNames.has(j.name)) continue
    parts.push(`• ${j.name} — ${j.stage || 'running'}`)
  }
  for (const j of ingest.recent.value) {
    const tag = j.status === 'failed' ? `failed: ${j.error || 'error'}` : 'done'
    parts.push(`• ${j.name} (${tag})`)
  }
  return parts.join('\n')
})
</script>

<style scoped>
.status-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.6rem 1.25rem;
  background-color: var(--bg-base);
  border-bottom: 1px solid var(--border-default);
  backdrop-filter: blur(12px);
  position: relative;
  z-index: 100;
}

.status-bar__label {
  font-weight: 700;
  font-size: 0.9rem;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--neon-cyan);
  text-shadow: 0 0 10px var(--neon-cyan-30);
  transition: opacity 0.5s ease, transform 0.5s ease;
  min-width: 56px;
}

.status-bar__label--hidden {
  opacity: 0;
  pointer-events: none;
}

/* ── Hamburger button ── */
.status-bar__hamburger {
  display: none;
  flex-direction: column;
  justify-content: center;
  gap: 4px;
  width: 32px;
  height: 32px;
  padding: 4px;
  background: none;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
  transition: border-color 0.2s, background-color 0.2s;
}

.status-bar__hamburger:hover {
  border-color: var(--border-subtle);
  background-color: var(--neon-cyan-08);
}

.status-bar__hamburger-line {
  display: block;
  width: 100%;
  height: 2px;
  background-color: var(--text-secondary);
  border-radius: 1px;
  transition: transform 0.3s ease, opacity 0.3s ease, background-color 0.2s;
}

.status-bar__hamburger:hover .status-bar__hamburger-line {
  background-color: var(--neon-cyan);
}

/* Hamburger → X animation */
.status-bar__hamburger--open .status-bar__hamburger-line:nth-child(1) {
  transform: translateY(6px) rotate(45deg);
  background-color: var(--neon-cyan);
}

.status-bar__hamburger--open .status-bar__hamburger-line:nth-child(2) {
  opacity: 0;
}

.status-bar__hamburger--open .status-bar__hamburger-line:nth-child(3) {
  transform: translateY(-6px) rotate(-45deg);
  background-color: var(--neon-cyan);
}

/* ── Navigation ── */
.status-bar__nav {
  display: flex;
  gap: 0.25rem;
  flex: 1;
  justify-content: center;
}

.status-bar__link {
  color: var(--text-secondary);
  text-decoration: none;
  font-size: 0.85rem;
  padding: 0.3rem 0.75rem;
  border-radius: 6px;
  transition: all 0.2s;
  border: 1px solid transparent;
}

.status-bar__link:hover {
  color: var(--text-primary);
  background-color: var(--neon-cyan-08);
  border-color: var(--border-subtle);
  text-shadow: 0 0 6px var(--neon-cyan-15);
}

.status-bar__link.router-link-active {
  color: var(--neon-cyan);
  background-color: var(--neon-cyan-08);
  border-color: var(--neon-cyan-30);
  text-shadow: 0 0 8px var(--neon-cyan-30);
  box-shadow: 0 0 12px var(--neon-cyan-08);
}

/* ── Status indicator ── */
.status-bar__indicator {
  font-size: 0.75rem;
  padding: 0.15rem 0.6rem;
  border-radius: 9999px;
  border: 1px solid transparent;
}

.status-bar__indicator.online {
  color: var(--neon-green);
  background-color: rgba(34, 197, 94, 0.08);
  border-color: rgba(34, 197, 94, 0.2);
  text-shadow: 0 0 6px rgba(34, 197, 94, 0.3);
}

.status-bar__indicator.offline {
  color: var(--neon-red);
  background-color: rgba(239, 68, 68, 0.08);
  border-color: rgba(239, 68, 68, 0.2);
}

.status-bar__indicator.unknown {
  color: var(--neon-yellow);
  background-color: rgba(234, 179, 8, 0.08);
  border-color: rgba(234, 179, 8, 0.2);
}

/* ── Ingest progress badge ── */
.status-bar__ingest {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  margin-right: 0.5rem;
  padding: 0.18rem 0.65rem;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--neon-cyan);
  background-color: var(--neon-cyan-08);
  border: 1px solid var(--neon-cyan-30);
  border-radius: 9999px;
  white-space: nowrap;
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  text-shadow: 0 0 6px var(--neon-cyan-30);
}

.status-bar__ingest-bar {
  position: relative;
  display: inline-block;
  width: 60px;
  height: 4px;
  background-color: var(--neon-cyan-08);
  border: 1px solid var(--neon-cyan-30);
  border-radius: 9999px;
  overflow: hidden;
  flex-shrink: 0;
}

.status-bar__ingest-bar-fill {
  position: absolute;
  inset: 0 auto 0 0;
  width: 0%;
  background: linear-gradient(90deg, var(--neon-cyan), var(--neon-cyan));
  box-shadow: 0 0 8px var(--neon-cyan);
  transition: width 0.25s ease-out;
}

.status-bar__ingest--idle {
  color: var(--neon-green);
  background-color: rgba(34, 197, 94, 0.08);
  border-color: rgba(34, 197, 94, 0.25);
  text-shadow: 0 0 6px rgba(34, 197, 94, 0.3);
}

.status-bar__ingest-spinner {
  width: 10px;
  height: 10px;
  border: 2px solid var(--neon-cyan-30);
  border-top-color: var(--neon-cyan);
  border-radius: 50%;
  animation: status-bar__spin 0.9s linear infinite;
}

.status-bar__ingest-check {
  font-size: 0.85rem;
  line-height: 1;
}

.status-bar__ingest-text {
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── Reindex (cold-start embedding) pill ── */
.status-bar__reindex {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  margin-right: 0.5rem;
  padding: 0.18rem 0.45rem 0.18rem 0.65rem;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--neon-cyan);
  background-color: var(--neon-cyan-08);
  border: 1px solid var(--neon-cyan-30);
  border-radius: 9999px;
  white-space: nowrap;
  text-shadow: 0 0 6px var(--neon-cyan-30);
}

.status-bar__reindex--idle {
  color: var(--neon-green);
  background-color: rgba(34, 197, 94, 0.08);
  border-color: rgba(34, 197, 94, 0.25);
  text-shadow: 0 0 6px rgba(34, 197, 94, 0.3);
}

.status-bar__reindex--failed {
  color: var(--neon-red, #ef4444);
  background-color: rgba(239, 68, 68, 0.08);
  border-color: rgba(239, 68, 68, 0.3);
  text-shadow: 0 0 6px rgba(239, 68, 68, 0.3);
}

.status-bar__reindex-spinner {
  width: 10px;
  height: 10px;
  border: 2px solid var(--neon-cyan-30);
  border-top-color: var(--neon-cyan);
  border-radius: 50%;
  animation: status-bar__spin 0.9s linear infinite;
}

.status-bar__reindex-icon {
  font-size: 0.85rem;
  line-height: 1;
}

.status-bar__reindex-text {
  overflow: hidden;
  text-overflow: ellipsis;
  font-variant-numeric: tabular-nums;
}

.status-bar__reindex-retry,
.status-bar__reindex-dismiss {
  background: none;
  border: 1px solid currentColor;
  color: inherit;
  font-size: 0.7rem;
  font-weight: 700;
  padding: 0 0.45rem;
  line-height: 1.4;
  border-radius: 9999px;
  cursor: pointer;
  opacity: 0.8;
  transition: opacity 0.15s ease;
}

.status-bar__reindex-retry:hover,
.status-bar__reindex-dismiss:hover {
  opacity: 1;
}

.status-bar__reindex-dismiss {
  border: none;
  font-size: 1rem;
  padding: 0 0.3rem;
}

@keyframes status-bar__spin {
  to { transform: rotate(360deg); }
}

/* ── MCP toggle pill ── */
.status-bar__mcp {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  margin-left: 0.5rem;
  padding: 0.18rem 0.6rem 0.18rem 0.5rem;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-secondary);
  background-color: rgba(148, 163, 184, 0.08);
  border: 1px solid var(--border-subtle);
  border-radius: 9999px;
  cursor: pointer;
  transition: all 0.18s ease;
  user-select: none;
}

.status-bar__mcp:hover:not(:disabled) {
  color: var(--text-primary);
  background-color: rgba(148, 163, 184, 0.14);
  border-color: var(--border-default);
}

.status-bar__mcp:disabled {
  opacity: 0.6;
  cursor: progress;
}

.status-bar__mcp-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: var(--text-muted, #64748b);
  box-shadow: 0 0 0 0 transparent;
  transition: background-color 0.2s, box-shadow 0.2s;
}

.status-bar__mcp--on {
  color: var(--neon-cyan);
  background-color: var(--neon-cyan-08);
  border-color: var(--neon-cyan-30);
  text-shadow: 0 0 6px var(--neon-cyan-30);
  box-shadow: 0 0 10px var(--neon-cyan-08);
}

.status-bar__mcp--on:hover:not(:disabled) {
  background-color: var(--neon-cyan-15, rgba(34, 211, 238, 0.18));
}

.status-bar__mcp--on .status-bar__mcp-dot {
  background-color: #22d3ee;
  box-shadow: 0 0 8px rgba(34, 211, 238, 0.7), 0 0 2px rgba(34, 211, 238, 1);
  animation: mcpPulse 2.4s ease-in-out infinite;
}

.status-bar__mcp--busy {
  opacity: 0.75;
}

.status-bar__mcp-count {
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  padding: 0 0.35rem;
  border-radius: 9999px;
  background-color: rgba(34, 211, 238, 0.18);
  color: var(--neon-cyan);
  font-size: 0.66rem;
}

@keyframes mcpPulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.55; }
}

/* ── Backdrop (mobile only) ── */
.status-bar__backdrop {
  display: none;
}

/* ═══════════════════════════════════════════════
   Responsive — collapse nav below 640px
   ═══════════════════════════════════════════════ */
@media (max-width: 640px) {
  .status-bar__hamburger {
    display: flex;
  }

  .status-bar__nav {
    /* Off-canvas dropdown */
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    flex-direction: column;
    gap: 0;
    background-color: var(--bg-base);
    border-bottom: 1px solid var(--border-default);
    padding: 0;
    max-height: 0;
    overflow: hidden;
    opacity: 0;
    transition: max-height 0.35s cubic-bezier(0.4, 0, 0.2, 1),
                opacity 0.25s ease,
                padding 0.3s ease;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
    z-index: 200;
  }

  .status-bar__nav--open {
    max-height: 320px;
    opacity: 1;
    padding: 0.5rem 0;
  }

  .status-bar__link {
    padding: 0.65rem 1.25rem;
    border-radius: 0;
    font-size: 0.9rem;
    border: none;
    border-left: 2px solid transparent;
    transition: all 0.15s ease;
  }

  .status-bar__link:hover {
    border-radius: 0;
    border-color: transparent;
    border-left-color: var(--neon-cyan-30);
    background-color: var(--neon-cyan-08);
  }

  .status-bar__link.router-link-active {
    border-radius: 0;
    border-color: transparent;
    border-left-color: var(--neon-cyan);
    background-color: var(--neon-cyan-08);
    box-shadow: none;
  }

  .status-bar__backdrop {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 99;
    background: rgba(0, 0, 0, 0.4);
  }
}
</style>
