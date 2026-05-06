<script setup lang="ts">
import type { ChatMessage, ChatTurnMetrics, OrbState, UrlIngestResult } from '~/types'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { useChatHealth } from '~/composables/useChatHealth'
import { useSpecialists } from '~/composables/useSpecialists'
import ChatFirstTurnWarmup from '~/components/ChatFirstTurnWarmup.vue'

marked.setOptions({ breaks: true, gfm: true })

function renderMarkdown(text: string): string {
  const html = marked.parse(text) as string
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } })
}

function modelLabel(provider?: string, model?: string): string {
  // ADR 015 — single dispatcher; the on-message provider/model attribution
  // is purely for display. Strip the `ollama_chat/` prefix so the chat
  // history shows e.g. `qwen3:8b` instead of `ollama_chat/qwen3:8b`.
  if (!model) return ''
  return model.replace(/^ollama(?:_chat)?\//, '')
}

function formatTime(iso?: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleString('pl-PL', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

// Per-turn telemetry helpers — render a compact mono readout next to the
// model label (`12.4 t/s · 0.85s`). The full breakdown lives in the
// title attribute so the bubble stays uncluttered. Hidden when
// decode_tps is missing (empty turn / no timings reported).
function formatTtft(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

function metricsTooltip(m?: ChatTurnMetrics): string {
  if (!m) return ''
  const parts: string[] = []
  if (m.decode_tps != null) parts.push(`Decode: ${m.decode_tps.toFixed(2)} tok/s (${m.eval_count} tokens)`)
  if (m.prefill_tps != null) parts.push(`Prefill: ${m.prefill_tps.toFixed(0)} tok/s (${m.prompt_eval_count} tokens)`)
  parts.push(`TTFT: ${formatTtft(m.ttft_ms)}`)
  if (m.load_ms > 1) parts.push(`Load: ${formatTtft(m.load_ms)} (cold)`)
  parts.push(`Total: ${formatTtft(m.total_ms)}`)
  return parts.join('\n')
}

const { activeSpecialists, deactivate } = useSpecialists()
const chatHealth = useChatHealth()
chatHealth.ensureBaselinesLoaded()

function statusFor(model?: string): 'healthy' | 'slow' | 'fast' | 'unknown' {
  return model ? chatHealth.statusFor(model) : 'unknown'
}

// The most recent assistant turn that carried metrics drives the chat-
// header health banner — when the latest sample for that model is
// classified `slow`, show a discreet advisory linking to the probe.
// We don't show a banner for `fast` because the upgrade hint already
// fires once via toast; a persistent fast banner would nag.
const latestHealthStatus = computed<'healthy' | 'slow' | 'fast' | 'unknown'>(() => {
  for (let i = props.messages.length - 1; i >= 0; i--) {
    const m = props.messages[i]
    if (m.role === 'assistant' && m.model && m.metrics?.decode_tps != null) {
      return chatHealth.statusFor(m.model)
    }
  }
  return 'unknown'
})

const slowHealthMessage = computed(() => {
  if (latestHealthStatus.value !== 'slow') return null
  return 'Recent turns are running below this model\'s probe baseline.'
})

const props = defineProps<{
  messages: ChatMessage[]
  currentResponse: string
  isLoading: boolean
  toolActivity: string
  error?: string
  canRetry?: boolean
  voiceState?: OrbState
  voiceSupported?: boolean
  ollamaDown?: boolean
  slowResponse?: string
  /**
   * True once the sidecar has produced at least one successful response in
   * this WS connection (set on first `done` event in `useChat`). Drives
   * the cold-bootstrap surface gating: only show `ChatFirstTurnWarmup`
   * when the sidecar is genuinely cold (`sidecarWarm === false`). Once
   * warm, every turn — fresh session or follow-up — uses the typing-dots
   * indicator instead. Without this flag the bootstrap surface fired
   * for the first turn of every new session, even when the sidecar was
   * already warm and the actual wait was 1-2 s — which made the 4-stage
   * "BOOTSTRAPPING LOCAL MODEL" instrument-panel readout overpromise on
   * latency and feel patronizing.
   */
  sidecarWarm?: boolean
}>()

const emit = defineEmits<{
  send: [content: string]
  retry: []
  toggleVoice: []
  reconnectOllama: []
}>()

const { ingestUrl } = useApi()

const input = ref('')
const inputEl = ref<HTMLTextAreaElement | null>(null)
const messagesContainer = ref<HTMLElement | null>(null)
const ingestLoading = ref(false)
const ingestResult = ref<{ ok: boolean; message: string } | null>(null)

const URL_RE = /https?:\/\/[^\s.,;!?)>\]]+/
const YT_RE = /(?:youtube\.com\/watch\?.*v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/shorts\/)([\w-]{11})/

const detectedUrl = computed(() => {
  const match = input.value.match(URL_RE)
  return match ? match[0] : null
})

const urlType = computed(() => {
  if (!detectedUrl.value) return null
  return YT_RE.test(detectedUrl.value) ? 'youtube' : 'webpage'
})

async function handleSaveUrl() {
  if (!detectedUrl.value || ingestLoading.value) return
  ingestLoading.value = true
  ingestResult.value = null
  try {
    const res = await ingestUrl(detectedUrl.value)
    ingestResult.value = { ok: true, message: `Saved: ${res.path} (${res.word_count} words)` }
    setTimeout(() => { ingestResult.value = null }, 4000)
  } catch {
    ingestResult.value = { ok: false, message: 'Import failed' }
    setTimeout(() => { ingestResult.value = null }, 4000)
  } finally {
    ingestLoading.value = false
  }
}

function handleSend(): void {
  const text = input.value.trim()
  if (!text || props.isLoading) return
  emit('send', text)
  input.value = ''
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    handleSend()
  }
}

function autoResize(event: Event): void {
  const el = event.target as HTMLTextAreaElement
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 150) + 'px'
}

watch(
  () => props.isLoading,
  (loading, wasLoading) => {
    if (wasLoading && !loading) {
      nextTick(() => inputEl.value?.focus())
    }
  },
)

// ── First-turn warmup surface ────────────────────────────────────────
//
// Show the bootstrap surface only when the sidecar is genuinely cold —
// no successful response has come back through this WS connection yet
// (`sidecarWarm` flips true on the first `done` event in useChat). This
// supersedes the prior session-local "no assistant messages yet"
// heuristic, which over-fired on every "+ New" session even when the
// model was already resident and the actual wait was 1-2 s. The cold
// cost is process-scoped, not session-scoped: once the sidecar has
// produced any output, all subsequent turns — across any session, on
// any "+ New" — should use the lighter typing-dots indicator. Only
// when the user genuinely has a cold sidecar (fresh launch, sidecar
// never produced output yet) should the 4-stage instrument readout
// make a promise about a 15 s wait.
const isFirstTurnWaiting = computed(() =>
  props.isLoading
  && !props.currentResponse
  && !props.toolActivity
  && !props.sidecarWarm,
)

const firstTurnStartedAt = ref<number>(0)
watch(
  () => isFirstTurnWaiting.value,
  (active, wasActive) => {
    if (active && !wasActive) {
      firstTurnStartedAt.value = Date.now()
    }
  },
  { immediate: true },
)

// ── Re-warming microstate (warm-sidecar turns with > 1 s before stream) ──
//
// On warm-sidecar follow-ups the typing dots used to sit silently for up
// to 24 s on the consistently-reproducing turn-2 back-pressure (per the
// chat_step instrumentation at backend/routers/chat.py:861). After 1 s
// without any text or tool activity, escalate the dots to an explicit
// "re-warming model…" hint so the user has a stable mental model.
// Cold-sidecar first turn keeps using the 4-stage ChatFirstTurnWarmup
// instrument readout — this microstate covers everything else.
const currentTurnStartedAt = ref<number>(0)
const reWarmTickNow = ref<number>(Date.now())
let reWarmTick: ReturnType<typeof setInterval> | null = null

function stopReWarmTimer(): void {
  if (reWarmTick) {
    clearInterval(reWarmTick)
    reWarmTick = null
  }
}

watch(
  () => props.isLoading,
  (loading, wasLoading) => {
    if (loading && !wasLoading) {
      currentTurnStartedAt.value = Date.now()
      reWarmTickNow.value = Date.now()
      stopReWarmTimer()
      reWarmTick = setInterval(() => {
        reWarmTickNow.value = Date.now()
      }, 250)
    } else if (!loading && wasLoading) {
      currentTurnStartedAt.value = 0
      stopReWarmTimer()
    }
  },
)

onUnmounted(stopReWarmTimer)

const isReWarming = computed(() =>
  props.isLoading
  && !props.currentResponse
  && !props.toolActivity
  && !isFirstTurnWaiting.value
  && currentTurnStartedAt.value > 0
  && (reWarmTickNow.value - currentTurnStartedAt.value) > 1000,
)

// ── Throttled streaming markdown render ────────────────────────────────
//
// Why throttle: build #6 chat_step instrumentation found that a 27-token
// reply spent 13.5s in the streaming loop while Ollama itself reported
// only 3.4s of work — a 10s gap. Each text_delta event triggered a full
// re-render of the streaming bubble; `renderMarkdown(currentResponse)`
// re-parses the *entire accumulated string* through marked + DOMPurify
// every time a new token arrives. As the response grows, that work
// grows with it. The Tauri webview gets stuck doing markdown parses and
// can't drain the WebSocket fast enough; the backend's `await
// ws.send_json(...)` calls back-pressure on each text_delta, stretching
// the streaming loop wall-clock far past Ollama's actual emit time.
//
// Fix: only re-parse markdown at most every STREAM_RENDER_MS during
// streaming. The user perceives a smooth update because the human eye
// can't distinguish 50 ms from 80 ms anyway, but the WS now drains at
// full speed and end-to-end latency tracks Ollama's real performance.
// The trailing-edge timer guarantees the final state always renders
// even if the last token lands inside a throttle window. When the
// message moves into `messages[]` (stream complete), the per-message
// `v-html="renderMarkdown(msg.content)"` in the messages loop renders
// the final markdown — that path stays untouched.
const STREAM_RENDER_MS = 80
const renderedStream = ref('')
let _streamRenderTimer: ReturnType<typeof setTimeout> | null = null
let _streamRenderLast = 0

watch(
  () => props.currentResponse,
  (text) => {
    if (!text) {
      renderedStream.value = ''
      _streamRenderLast = 0
      if (_streamRenderTimer) {
        clearTimeout(_streamRenderTimer)
        _streamRenderTimer = null
      }
      return
    }
    const now = Date.now()
    const sinceLast = now - _streamRenderLast
    if (sinceLast >= STREAM_RENDER_MS) {
      renderedStream.value = renderMarkdown(text)
      _streamRenderLast = now
      if (_streamRenderTimer) {
        clearTimeout(_streamRenderTimer)
        _streamRenderTimer = null
      }
    }
    else if (!_streamRenderTimer) {
      _streamRenderTimer = setTimeout(() => {
        renderedStream.value = renderMarkdown(props.currentResponse)
        _streamRenderLast = Date.now()
        _streamRenderTimer = null
      }, STREAM_RENDER_MS - sinceLast)
    }
  },
)

onBeforeUnmount(() => {
  if (_streamRenderTimer) {
    clearTimeout(_streamRenderTimer)
    _streamRenderTimer = null
  }
})

watch(
  () => [props.messages.length, props.currentResponse],
  () => {
    nextTick(() => {
      if (messagesContainer.value) {
        messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
      }
    })
  },
)
</script>

<template>
  <div class="chat-panel">
    <Transition name="badge-slide">
      <div v-if="activeSpecialists.length" class="chat-panel__specialist-bar">
        <SpecialistBadge
          v-for="spec in activeSpecialists"
          :key="spec.id"
          :specialist="spec"
          @deactivate="deactivate(spec.id)"
        />
      </div>
    </Transition>

    <!-- Ollama offline banner -->
    <div v-if="ollamaDown" class="chat-panel__ollama-banner">
      <Icon name="ph:warning-fill" class="icon--md icon--warning chat-panel__ollama-banner-icon" />
      <span class="chat-panel__ollama-banner-text">Ollama is not responding. Chat is unavailable until the local runtime comes back online.</span>
      <button class="chat-panel__ollama-banner-btn" @click="emit('reconnectOllama')">Reconnect</button>
    </div>

    <!-- (Legacy `slowResponse` banner removed 2026-05-01: superseded by
         ChatFirstTurnWarmup for the cold-bootstrap case + the streaming
         response itself for warm turns. Two indicators on the same
         screen for the same wait read as broken UI. The slowResponse
         ref is still computed in useChat.ts but no longer rendered;
         keeping the ref for now as a passive signal in case a future
         surface wants it.) -->

    <!-- Sustained-slow advisory (ADR 005 §C trigger 2). Only renders
         when the latest turn's decode_tps for the active model is below
         the slow threshold against its probe baseline. Click-through to
         the probe panel re-tests in place. -->
    <div v-if="slowHealthMessage" class="chat-panel__health-banner">
      <Icon name="ph:gauge" class="icon--sm chat-panel__health-banner-glyph" />
      <span class="chat-panel__health-banner-text">{{ slowHealthMessage }}</span>
      <NuxtLink to="/settings#local-models" class="chat-panel__health-banner-link">Re-test models</NuxtLink>
    </div>

    <div ref="messagesContainer" class="chat-panel__messages">
      <div
        v-for="(msg, i) in messages"
        :key="`${msg.role}-${i}-${msg.content.slice(0, 20)}`"
        class="chat-panel__message"
        :class="msg.role"
      >
        <div
          class="chat-panel__bubble"
          :class="{ 'chat-panel__bubble--md': msg.role === 'assistant' }"
        >
          <div v-if="msg.role === 'assistant' && (msg.model || msg.timestamp || msg.metrics)" class="chat-panel__meta">
            <span v-if="msg.model" class="chat-panel__meta-model">
              <Icon name="ph:hard-drives" class="chat-panel__meta-icon" aria-label="Local model" />
              {{ modelLabel(msg.provider, msg.model) }}
            </span>
            <span
              v-if="msg.metrics?.decode_tps != null"
              class="chat-panel__meta-tps"
              :class="`chat-panel__meta-tps--${statusFor(msg.model)}`"
              :title="metricsTooltip(msg.metrics)"
            >
              <span class="chat-panel__meta-tps-num">{{ msg.metrics.decode_tps.toFixed(1) }}</span>
              <span class="chat-panel__meta-tps-unit">t/s</span>
              <span class="chat-panel__meta-tps-sep">·</span>
              <span class="chat-panel__meta-tps-ttft">{{ formatTtft(msg.metrics.ttft_ms) }}</span>
            </span>
            <span v-if="msg.timestamp" class="chat-panel__meta-time">{{ formatTime(msg.timestamp) }}</span>
          </div>
          <div v-else-if="msg.role === 'user' && msg.timestamp" class="chat-panel__meta">
            <span class="chat-panel__meta-time">{{ formatTime(msg.timestamp) }}</span>
          </div>
          <div v-if="msg.role === 'assistant'" v-html="renderMarkdown(msg.content)" />
          <template v-else>{{ msg.content }}</template>
          <TraceList v-if="msg.role === 'assistant' && msg.trace" :items="msg.trace" />
        </div>
      </div>

      <div v-if="currentResponse" class="chat-panel__message assistant">
        <div class="chat-panel__bubble chat-panel__bubble--md">
          <!-- Throttled markdown — see `renderedStream` watcher above for
               why this isn't `renderMarkdown(currentResponse)` directly.
               Short version: that synchronous re-parse on every WS
               text_delta backed up the WebSocket and stretched the
               streaming loop wall-clock far past Ollama's emit time. -->
          <span v-html="renderedStream" />
          <span class="chat-panel__cursor">▊</span>
        </div>
      </div>

      <!-- First-turn-of-session bootstrap surface — replaces the generic
           typing dots for the one turn where the wait is actually long
           (~16s on M5 24 GB). Communicates the one-time nature of the
           cost so the user has a stable mental model after turn 1
           lands. See `ChatFirstTurnWarmup.vue` for design intent. -->
      <ChatFirstTurnWarmup
        v-if="isFirstTurnWaiting && firstTurnStartedAt > 0"
        :started-at="firstTurnStartedAt"
        :estimated-total-sec="15"
      />

      <!-- Re-warming microstate — warm-sidecar turns where >1 s elapses
           without text or tool activity. Escalates the silent typing
           dots to an explicit hint so the user has a mental model for
           the consistently-reproducing turn-2 back-pressure (chat.py:861
           instrumentation). -->
      <div
        v-else-if="isReWarming"
        class="chat-panel__message assistant"
      >
        <div class="chat-panel__bubble chat-panel__rewarming">
          <span class="chat-panel__rewarming-dot" />
          <span class="chat-panel__rewarming-text">re-warming model…</span>
        </div>
      </div>

      <!-- Typing indicator (dots) — for follow-up turns where the wait
           is short (1-2s warm) or merely longer-than-cached (eviction,
           tool-loop second dispatch). Always reachable as the lower-
           drama loading state once the session has had at least one
           assistant turn. -->
      <div
        v-else-if="isLoading && !currentResponse && !toolActivity"
        class="chat-panel__message assistant"
      >
        <div class="chat-panel__bubble chat-panel__typing">
          <span class="chat-panel__dot" />
          <span class="chat-panel__dot" />
          <span class="chat-panel__dot" />
        </div>
      </div>

      <div v-if="toolActivity" class="chat-panel__activity">
        <span class="chat-panel__activity-spinner" />
        {{ toolActivity }}
      </div>

      <div v-if="error" class="chat-panel__error">
        <Icon name="ph:warning-fill" class="icon--md icon--warning chat-panel__error-icon" />
        <span class="chat-panel__error-text">{{ error }}</span>
        <button v-if="canRetry" class="chat-panel__error-retry" @click="emit('retry')">Retry</button>
      </div>
    </div>

    <div v-if="detectedUrl" class="chat-panel__url-bar">
      <Icon
        :name="urlType === 'youtube' ? 'ph:youtube-logo-fill' : 'ph:link'"
        class="icon--md icon--accent chat-panel__url-icon"
      />
      <span class="chat-panel__url-text">{{ detectedUrl }}</span>
      <button
        class="chat-panel__url-action"
        :disabled="ingestLoading"
        @click="handleSaveUrl"
      >
        {{ ingestLoading ? 'Saving...' : 'Save to memory' }}
      </button>
    </div>

    <div v-if="ingestResult" class="chat-panel__url-result" :class="{ 'chat-panel__url-result--error': !ingestResult.ok }">
      <Icon
        :name="ingestResult.ok ? 'ph:check-circle-fill' : 'ph:x-circle-fill'"
        :class="['icon--sm', ingestResult.ok ? 'icon--success' : 'icon--danger']"
      />
      {{ ingestResult.message }}
    </div>

    <div class="chat-panel__input-bar">
      <textarea
        ref="inputEl"
        v-model="input"
        class="chat-panel__input"
        placeholder="Talk to Jarvis..."
        rows="1"
        :disabled="isLoading"
        @keydown="handleKeydown"
        @input="autoResize"
      />
      <ModelSelector />
      <button
        class="chat-panel__icon-btn chat-panel__icon-btn--send"
        :disabled="isLoading || !input.trim()"
        aria-label="Send message"
        @click="handleSend"
      >
        <Icon name="ph:paper-plane-tilt-fill" class="icon--lg" />
      </button>
    </div>
  </div>
</template>

<style scoped>
.chat-panel {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  width: 100%;
  max-width: 900px;
}

.chat-panel__specialist-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.35rem;
  padding: 0.4rem 1.5rem;
  border-radius: 16px;
  margin-top: 12px;
  border-bottom: 1px solid var(--border-subtle);
  background:
    linear-gradient(90deg, var(--neon-cyan-08) 0%, transparent 60%),
    var(--bg-surface);
  flex-shrink: 0;
}

.badge-slide-enter-active {
  transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
}
.badge-slide-leave-active {
  transition: all 0.2s ease;
}
.badge-slide-enter-from {
  opacity: 0;
  transform: translateY(-100%);
}
.badge-slide-leave-to {
  opacity: 0;
  transform: translateY(-100%);
}

.chat-panel__messages {
  flex: 1;
  overflow-y: auto;
  padding: 1.25rem 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

/* Ollama offline banner */
.chat-panel__ollama-banner {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.6rem 1rem;
  margin: 0.5rem 1.5rem 0;
  border-radius: 8px;
  background: rgba(251, 191, 36, 0.06);
  border: 1px solid rgba(251, 191, 36, 0.2);
  font-size: 0.82rem;
  color: #fbbf24;
  flex-shrink: 0;
}

.chat-panel__ollama-banner-icon {
  font-size: 0.9rem;
}

.chat-panel__ollama-banner-text {
  flex: 1;
}

.chat-panel__ollama-banner-btn {
  padding: 0.25rem 0.6rem;
  border-radius: 5px;
  border: 1px solid rgba(251, 191, 36, 0.3);
  background: transparent;
  color: #fbbf24;
  font-size: 0.75rem;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s;
}

.chat-panel__ollama-banner-btn:hover {
  background: rgba(251, 191, 36, 0.1);
}

.chat-panel__ollama-banner-btn--alt {
  border-color: var(--border-default);
  color: var(--text-secondary);
}

.chat-panel__ollama-banner-btn--alt:hover {
  background: rgba(255, 255, 255, 0.04);
}

/* Slow response indicator */
.chat-panel__slow-indicator {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.4rem 1rem;
  margin: 0.3rem 1.5rem 0;
  font-size: 0.78rem;
  color: var(--text-muted);
  flex-shrink: 0;
}

.chat-panel__slow-icon {
  font-size: 0.85rem;
}

/* Sustained-slow chat-side advisory. Mono so it pairs with the per-turn
   tps pill; amber-tinted but quieter than the OOM banner. The link is
   the chat-side counterpart to the snackbar's "Re-test models" action. */
.chat-panel__health-banner {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  padding: 0.42rem 0.95rem;
  margin: 0.3rem 1.5rem 0;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.72rem;
  color: var(--neon-orange);
  background: rgba(251, 146, 60, 0.04);
  border: 1px solid rgba(251, 146, 60, 0.18);
  border-radius: 6px;
  flex-shrink: 0;
}

.chat-panel__health-banner-glyph {
  color: rgba(251, 146, 60, 0.7);
  font-size: 0.65rem;
}

.chat-panel__health-banner-text {
  flex: 1;
  color: var(--text-secondary);
}

.chat-panel__health-banner-link {
  color: var(--neon-orange);
  text-decoration: none;
  border-bottom: 1px dashed rgba(251, 146, 60, 0.4);
  padding-bottom: 1px;
  transition: color 0.15s, border-color 0.15s;
  white-space: nowrap;
}

.chat-panel__health-banner-link:hover {
  color: rgba(251, 146, 60, 1);
  border-bottom-color: rgba(251, 146, 60, 0.9);
}

.chat-panel__message {
  display: flex;
  animation: slideIn 0.25s ease-out;
}

.chat-panel__message.user {
  justify-content: flex-end;
}

.chat-panel__message.assistant {
  justify-content: flex-start;
}

.chat-panel__bubble {
  max-width: 78%;
  padding: 0.7rem 1rem;
  border-radius: 0.85rem;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.55;
  font-size: 0.95rem;
}

/* Markdown content styles */
.chat-panel__bubble--md {
  white-space: normal;
}
.chat-panel__bubble--md :deep(p) {
  margin: 0 0 0.5em;
}
.chat-panel__bubble--md :deep(p:last-child) {
  margin-bottom: 0;
}
.chat-panel__bubble--md :deep(strong) {
  color: var(--neon-cyan);
  font-weight: 600;
}
.chat-panel__bubble--md :deep(em) {
  color: var(--text-secondary);
  font-style: italic;
}
.chat-panel__bubble--md :deep(code) {
  background: rgba(2, 254, 255, 0.08);
  border: 1px solid var(--neon-cyan-15);
  border-radius: 4px;
  padding: 0.1em 0.4em;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.88em;
  color: var(--neon-cyan);
}
.chat-panel__bubble--md :deep(pre) {
  background: var(--bg-deep);
  border: 1px solid var(--border-default);
  border-radius: 8px;
  padding: 0.85rem 1rem;
  overflow-x: auto;
  margin: 0.5em 0;
}
.chat-panel__bubble--md :deep(pre code) {
  background: none;
  border: none;
  padding: 0;
  font-size: 0.85em;
  color: var(--text-primary);
}
.chat-panel__bubble--md :deep(ul),
.chat-panel__bubble--md :deep(ol) {
  margin: 0.4em 0;
  padding-left: 1.4em;
}
.chat-panel__bubble--md :deep(li) {
  margin: 0.2em 0;
}
.chat-panel__bubble--md :deep(h1),
.chat-panel__bubble--md :deep(h2),
.chat-panel__bubble--md :deep(h3) {
  color: var(--neon-cyan);
  margin: 0.6em 0 0.3em;
  font-size: 1em;
  font-weight: 700;
}
.chat-panel__bubble--md :deep(blockquote) {
  border-left: 3px solid var(--neon-cyan-30);
  margin: 0.5em 0;
  padding-left: 0.75em;
  opacity: 0.8;
}
.chat-panel__bubble--md :deep(a) {
  color: var(--neon-cyan);
  text-decoration: underline;
  text-underline-offset: 2px;
}

/* --- Messages --- */
.chat-panel__message.user .chat-panel__bubble {
  background: rgba(2, 254, 255, 0.1);
  border: 1px solid rgba(2, 254, 255, 0.2);
  color: var(--text-primary);
}

.chat-panel__message.assistant .chat-panel__bubble {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  color: var(--text-primary);
}

.chat-panel__meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  font-size: 0.65rem;
  color: var(--text-muted);
  opacity: 0.6;
  margin-bottom: 0.35rem;
  user-select: none;
  line-height: 1;
}

.chat-panel__meta-model {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
}

.chat-panel__meta-icon {
  font-size: 11px;
  color: var(--neon-cyan-60);
  opacity: 0.8;
}

.chat-panel__meta-time {
  margin-left: auto;
  white-space: nowrap;
}

/* Per-turn telemetry readout (ADR 005 §C trigger 2) — sits between the
   model label and the timestamp. Mono so it reads as a technical
   readout (matches the chat-model-probe evidence list); tinted by
   health status. The full breakdown lives in the native title tooltip
   so this row stays a single uncluttered glyph-stripe. */
.chat-panel__meta-tps {
  display: inline-flex;
  align-items: baseline;
  gap: 0.25rem;
  padding: 1px 0.4rem;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  background: rgba(2, 254, 255, 0.04);
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.62rem;
  letter-spacing: 0.01em;
  color: var(--text-secondary);
  cursor: help;
  white-space: nowrap;
  transition: border-color 0.15s, background 0.15s;
}

.chat-panel__meta-tps:hover {
  border-color: var(--neon-cyan-30);
  background: var(--neon-cyan-08);
}

.chat-panel__meta-tps-num {
  color: var(--neon-cyan-60);
  font-weight: 600;
}

.chat-panel__meta-tps-unit {
  color: var(--text-muted);
  font-size: 0.92em;
}

.chat-panel__meta-tps-sep {
  color: var(--text-muted);
  opacity: 0.6;
}

.chat-panel__meta-tps-ttft {
  color: var(--text-secondary);
}

/* Health-status tints. The watcher's classification of *current* perf
   for the active model leaks into the historical bubble: a slow turn
   keeps reading slow even after the model recovers, which is the
   honest reading — that turn was slow when it happened. */
.chat-panel__meta-tps--slow {
  border-color: rgba(251, 146, 60, 0.25);
  background: rgba(251, 146, 60, 0.04);
}
.chat-panel__meta-tps--slow .chat-panel__meta-tps-num {
  color: var(--neon-orange);
}
.chat-panel__meta-tps--slow:hover {
  border-color: rgba(251, 146, 60, 0.5);
  background: rgba(251, 146, 60, 0.08);
}

.chat-panel__meta-tps--fast {
  border-color: rgba(52, 211, 153, 0.25);
  background: rgba(52, 211, 153, 0.04);
}
.chat-panel__meta-tps--fast .chat-panel__meta-tps-num {
  color: #34d399;
}
.chat-panel__meta-tps--fast:hover {
  border-color: rgba(52, 211, 153, 0.5);
  background: rgba(52, 211, 153, 0.08);
}

.chat-panel__cursor {
  color: var(--neon-cyan);
  animation: blink 0.8s step-end infinite;
}

@keyframes blink {
  50% { opacity: 0; }
}

.chat-panel__activity {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8rem;
  color: var(--neon-cyan-60);
  padding: 0.25rem 0;
  font-style: italic;
}

.chat-panel__activity-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--neon-cyan-30);
  border-top-color: var(--neon-cyan);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  flex-shrink: 0;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* --- Error bar --- */
.chat-panel__error {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin: 0.5rem 0;
  padding: 0.6rem 0.85rem;
  font-size: 0.85rem;
  color: rgba(251, 146, 60, 0.95);
  background: rgba(251, 146, 60, 0.06);
  border: 1px solid rgba(251, 146, 60, 0.2);
  border-radius: 8px;
  animation: slideIn 0.25s ease-out;
}

.chat-panel__error-icon {
  font-size: 1rem;
  flex-shrink: 0;
}

.chat-panel__error-text {
  flex: 1;
}

.chat-panel__error-retry {
  flex-shrink: 0;
  padding: 0.25rem 0.75rem;
  font-size: 0.8rem;
  border: 1px solid rgba(251, 146, 60, 0.3);
  border-radius: 6px;
  background: rgba(251, 146, 60, 0.1);
  color: rgba(251, 146, 60, 0.95);
  cursor: pointer;
  transition: all 0.2s;
}

.chat-panel__error-retry:hover {
  background: rgba(251, 146, 60, 0.2);
  border-color: rgba(251, 146, 60, 0.5);
}

/* --- Empty state --- */
.chat-panel__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex: 1;
  gap: 0.75rem;
  opacity: 0.5;
  padding: 3rem 1rem;
}

.chat-panel__empty-icon {
  font-size: 2.5rem;
}

.chat-panel__empty-text {
  font-size: 0.9rem;
  color: var(--text-muted);
  text-align: center;
  max-width: 300px;
  line-height: 1.5;
}

/* --- Typing dots --- */
.chat-panel__typing {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0.85rem 1.1rem !important;
  min-height: auto;
}

.chat-panel__dot {
  width: 7px;
  height: 7px;
  background: var(--neon-cyan-60);
  border-radius: 50%;
  animation: typingBounce 1.2s ease-in-out infinite;
}

.chat-panel__dot:nth-child(2) { animation-delay: 0.15s; }
.chat-panel__dot:nth-child(3) { animation-delay: 0.3s; }

@keyframes typingBounce {
  0%, 60%, 100% {
    transform: translateY(0);
    opacity: 0.4;
  }
  30% {
    transform: translateY(-5px);
    opacity: 1;
  }
}

/* --- Re-warming microstate (warm-sidecar > 1 s wait) --- */
.chat-panel__rewarming {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  padding: 0.85rem 1.1rem !important;
  min-height: auto;
  font-size: 0.82rem;
  color: var(--neon-cyan-60);
  font-style: italic;
  letter-spacing: 0.015em;
}
.chat-panel__rewarming-dot {
  width: 7px;
  height: 7px;
  background: var(--neon-cyan);
  border-radius: 50%;
  flex-shrink: 0;
  animation: rewarmingPulse 1.2s ease-in-out infinite;
}
@keyframes rewarmingPulse {
  0%, 100% { opacity: 0.45; transform: scale(1); }
  50%      { opacity: 1;    transform: scale(1.25); }
}

@keyframes slideIn {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.chat-panel__input-bar {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 1.2rem 1.5rem;
  border-top: 1px solid var(--border-default);
  background: var(--bg-base);
  flex-shrink: 0;
}

.chat-panel__input {
  flex: 1;
  resize: none;
  min-height: 46px;
  max-height: 150px;
  padding: 0.7rem 1rem;
  font-size: 0.95rem;
  line-height: 1.5;
  color: var(--text-primary);
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  border-radius: 12px;
  outline: none;
  transition: border-color 0.2s, box-shadow 0.2s;
  font-family: inherit;
}

.chat-panel__input:focus {
  border-color: var(--neon-cyan-30);
  box-shadow: 0 0 0 2px var(--neon-cyan-08), 0 0 15px var(--neon-cyan-08);
}

.chat-panel__input::placeholder {
  color: var(--text-muted);
}

.chat-panel__icon-btn {
  width: 42px;
  height: 42px;
  border-radius: 50%;
  border: 1px solid var(--border-default);
  background: var(--bg-surface);
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: all 0.2s;
}

.chat-panel__icon-btn:hover {
  color: var(--neon-cyan);
  border-color: var(--neon-cyan-30);
  background: var(--bg-elevated);
  box-shadow: 0 0 10px var(--neon-cyan-08);
}

.chat-panel__icon-btn--active {
  color: var(--neon-red);
  border-color: rgba(239, 68, 68, 0.5);
  box-shadow: 0 0 12px rgba(239, 68, 68, 0.15);
  animation: mic-pulse 1.2s ease-in-out infinite;
}

.chat-panel__icon-btn--send {
  background: rgba(2, 254, 255, 0.12);
  border-color: var(--neon-cyan-30);
  color: var(--neon-cyan);
}

.chat-panel__icon-btn--send:hover {
  background: rgba(2, 254, 255, 0.2);
  border-color: var(--neon-cyan-60);
  color: var(--neon-cyan);
  box-shadow: 0 0 15px var(--neon-cyan-15);
}

.chat-panel__icon-btn--send:disabled {
  opacity: 0.25;
  cursor: not-allowed;
  background: var(--bg-surface);
  border-color: var(--border-subtle);
  color: var(--text-muted);
  box-shadow: none;
}

@keyframes mic-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.3); }
  50% { box-shadow: 0 0 0 6px rgba(239, 68, 68, 0); }
}

.chat-panel__url-bar {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.6rem 1.5rem;
  background: var(--neon-cyan-08);
  border-top: 1px solid var(--border-default);
  font-size: 0.85rem;
}

.chat-panel__url-icon {
  flex-shrink: 0;
}

.chat-panel__url-text {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}

.chat-panel__url-action {
  flex-shrink: 0;
  padding: 0.3rem 0.85rem;
  border: 1px solid var(--neon-cyan-30);
  border-radius: 6px;
  background: transparent;
  color: var(--neon-cyan);
  cursor: pointer;
  font-size: 0.8rem;
  transition: all 0.2s;
}

.chat-panel__url-action:hover {
  background: var(--neon-cyan-08);
  box-shadow: 0 0 10px var(--neon-cyan-08);
}

.chat-panel__url-action:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.chat-panel__url-result {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.4rem 1.5rem;
  font-size: 0.8rem;
  border-top: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}

.chat-panel__url-result--error {
  color: var(--neon-red);
}

/* Setup slide transition */
.setup-slide-enter-active {
  transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
}
.setup-slide-leave-active {
  transition: all 0.2s ease;
}
.setup-slide-enter-from {
  opacity: 0;
  transform: translateY(12px);
}
.setup-slide-leave-to {
  opacity: 0;
  transform: translateY(12px);
}
</style>
