/**
 * useWarmup — track sidecar ML-component warmup status.
 *
 * The backend pre-loads heavy ML artifacts (fastembed embedder + reranker,
 * spaCy NER, HF tokenizers) at sidecar boot in a background thread. Without
 * this, those costs land on the *first* user-facing chat turn and produce
 * the cold-start "turn 2 takes 25-30 s" stall users see. See
 * `backend/services/warmup_service.py` for the full rationale.
 *
 * This composable polls `/api/health/warm` until the sidecar reports
 * `ready: true`, then stops. The poll cadence backs off slowly (250 ms →
 * 2 s) so an already-warm sidecar doesn't pay an extra round-trip on
 * subsequent app launches.
 *
 * The state is a module-level singleton: every consumer gets the same
 * reactive refs, so we never end up with multiple independent poll loops
 * racing against the same endpoint.
 */
import { ref, readonly } from 'vue'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
export type WarmupComponentState = 'pending' | 'running' | 'ready' | 'failed' | 'skipped'

export interface WarmupComponent {
  state: WarmupComponentState
  duration_ms: number | null
  error: string | null
}

export interface WarmupSnapshot {
  ready: boolean
  started: boolean
  completed: boolean
  started_at: number | null
  completed_at: number | null
  components: Record<string, WarmupComponent>
}

const _ready = ref(false)
const _components = ref<Record<string, WarmupComponent>>({})
const _started = ref(false)
const _failed = ref(false) // network/HTTP failures, not per-component failure
let _pollTimer: ReturnType<typeof setTimeout> | null = null
let _pollStarted = false

// Backoff schedule. Once warmup is reported ready or every component reaches
// a terminal state we stop. The schedule is short up front (the user is
// looking at a "preparing" affordance) and stretches out so a slow-loading
// sidecar doesn't get hammered.
const _SCHEDULE_MS = [250, 250, 500, 500, 1000, 1000, 2000]
let _scheduleIdx = 0

function _nextDelay(): number {
  const i = Math.min(_scheduleIdx, _SCHEDULE_MS.length - 1)
  _scheduleIdx += 1
  return _SCHEDULE_MS[i] ?? 2000
}

async function _pollOnce(): Promise<void> {
  try {
    const snap = await $fetch<WarmupSnapshot>(apiUrl('/api/health/warm'))
    _components.value = snap.components ?? {}
    _started.value = !!snap.started
    _ready.value = !!snap.ready
    _failed.value = false
    if (snap.ready) {
      // Terminal state — stop polling; the result is sticky for this session.
      _stopPolling()
      return
    }
  }
  catch {
    // Endpoint not yet reachable (sidecar still booting) or transient network
    // hiccup. Don't surface this as a hard failure; just keep polling.
    _failed.value = true
  }
  _pollTimer = setTimeout(_pollOnce, _nextDelay())
}

function _stopPolling(): void {
  if (_pollTimer !== null) {
    clearTimeout(_pollTimer)
    _pollTimer = null
  }
}

/**
 * Begin polling (idempotent). Call once during app startup — after that,
 * any consumer can read the reactive refs without triggering a new loop.
 */
export function startWarmupPolling(): void {
  if (_pollStarted) return
  _pollStarted = true
  _pollOnce()
}

/**
 * Stop polling and reset state. Test-only — production callers don't
 * need to teardown the singleton.
 */
export function _resetWarmupForTests(): void {
  _stopPolling()
  _ready.value = false
  _components.value = {}
  _started.value = false
  _failed.value = false
  _pollStarted = false
  _scheduleIdx = 0
}

export function useWarmup() {
  return {
    /** True once every component has reached a terminal state. */
    ready: readonly(_ready),
    /** True once the backend has scheduled the warmup task. */
    started: readonly(_started),
    /** True if the most recent poll request errored. */
    pollFailed: readonly(_failed),
    /** Per-component status map keyed by component name. */
    components: readonly(_components),
    startWarmupPolling,
  }
}
