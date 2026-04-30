/**
 * useFirstRun — drives the ADR 005 §B first-run pull pipeline UI.
 *
 * Polls `GET /api/local/first-run/status` while the orchestrator is
 * mid-pipeline (probing / pulling_primary / pulling_fallback / running_probe)
 * and exposes the snapshot to OnboardingLocalFlow.vue. Mirrors the shape of
 * `useReindexStatus` (the G5 cold-start reindex pill that ADR §B step 4
 * explicitly points at as the progress-UI pattern): single shared timer
 * across mounts, lazy on-mount fetch, ref-counted teardown.
 *
 * Two divergences from useReindexStatus:
 *
 *   1. The first-run pipeline is *blocking* until the marker lands. Where
 *      the reindex pill is a non-blocking notification, this composable
 *      surfaces the pipeline as the wizard's primary content. The
 *      `chatReady` getter flips true the moment the marker is written —
 *      that's the signal the wizard uses to release the user into chat
 *      (background fallback + probe continue silently).
 *
 *   2. There are two pull progresses (primary + fallback), not one. The
 *      backend's PullProgress dataclass shape is mirrored 1:1.
 *
 * The kickoff is driver-side: the wizard calls `start({ skip })` to begin
 * the pipeline. `start({ skip: true })` is the §B "Skip / opt-out" path —
 * writes no marker, lands the wizard in the manual model picker.
 */

import { apiUrl } from '~/utils/apiUrl'

export type FirstRunState =
  | 'idle'
  | 'probing'
  | 'pulling_primary'
  | 'pulling_fallback'
  | 'running_probe'
  | 'complete'
  | 'skipped'
  | 'failed'

export interface FirstRunPullProgress {
  model: string | null
  status: string
  completed: number
  total: number
  progress_pct: number
  error: string | null
}

export interface FirstRunStatus {
  state: FirstRunState
  tier: 'A' | 'B' | 'C' | null
  primary_model_id: string | null
  primary_ollama_model: string | null
  fallback_model_id: string | null
  fallback_ollama_model: string | null
  primary: FirstRunPullProgress
  fallback: FirstRunPullProgress
  probe_failed: boolean
  fallback_failed: boolean
  started_at: number | null
  primary_completed_at: number | null
  finished_at: number | null
  last_error: string | null
  marker_written: boolean
  marker_present: boolean
}

export interface FirstRunStartResult {
  result: 'started' | 'already_running' | 'already_complete' | 'skipped'
  tier?: 'A' | 'B' | 'C' | null
  primary_model_id?: string | null
}

const POLL_INTERVAL_MS = 1000

const DEFAULT_PULL: FirstRunPullProgress = {
  model: null,
  status: 'idle',
  completed: 0,
  total: 0,
  progress_pct: 0,
  error: null,
}

const DEFAULT_STATUS: FirstRunStatus = {
  state: 'idle',
  tier: null,
  primary_model_id: null,
  primary_ollama_model: null,
  fallback_model_id: null,
  fallback_ollama_model: null,
  primary: { ...DEFAULT_PULL },
  fallback: { ...DEFAULT_PULL },
  probe_failed: false,
  fallback_failed: false,
  started_at: null,
  primary_completed_at: null,
  finished_at: null,
  last_error: null,
  marker_written: false,
  marker_present: false,
}

let _timer: ReturnType<typeof setInterval> | null = null
let _refCount = 0

const TERMINAL_STATES: ReadonlySet<FirstRunState> = new Set(['complete', 'skipped', 'failed'])
const ACTIVE_STATES: ReadonlySet<FirstRunState> = new Set([
  'probing',
  'pulling_primary',
  'pulling_fallback',
  'running_probe',
])

export function useFirstRun() {
  const status = useState<FirstRunStatus>('first-run-status', () => ({ ...DEFAULT_STATUS }))
  const error = useState<string | null>('first-run-error', () => null)

  async function fetchOnce(): Promise<FirstRunStatus | null> {
    try {
      const data = await $fetch<FirstRunStatus>(apiUrl('/api/local/first-run/status'))
      status.value = data
      error.value = null
      return data
    } catch (e: unknown) {
      // Backend not yet up (cold start race) — try again on the next tick.
      // Don't clobber a previously-good snapshot with the error string.
      error.value = e instanceof Error ? e.message : 'first-run status fetch failed'
      return null
    }
  }

  function startPolling(): void {
    if (_timer !== null) return
    _timer = setInterval(() => {
      void fetchOnce().then((data) => {
        if (data && TERMINAL_STATES.has(data.state)) stopPolling()
      })
    }, POLL_INTERVAL_MS)
  }

  function stopPolling(): void {
    if (_timer !== null) {
      clearInterval(_timer)
      _timer = null
    }
  }

  /**
   * Kick the orchestrator. `skip: true` is the §B opt-out path: the
   * backend records `state="skipped"` without writing the marker, and the
   * wizard transitions to the manual model picker.
   *
   * Returns the orchestrator's response so the caller can branch on
   * `already_running` / `already_complete` (e.g. don't restart the modal
   * UI in those cases — just react to the snapshot).
   */
  async function start(opts: { skip?: boolean } = {}): Promise<FirstRunStartResult | null> {
    try {
      const result = await $fetch<FirstRunStartResult>(apiUrl('/api/local/first-run/start'), {
        method: 'POST',
        body: { skip: opts.skip ?? false },
      })
      // Snapshot immediately so the UI reflects the new state without
      // waiting a full poll interval.
      await fetchOnce()
      if (result.result === 'started' && !_timer) startPolling()
      return result
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : 'first-run start failed'
      return null
    }
  }

  /** True once the foreground primary pull lands (chat is unlocked). */
  const chatReady = computed<boolean>(() => {
    const s = status.value
    if (s.marker_written || s.marker_present) return true
    return s.state === 'pulling_fallback' || s.state === 'running_probe' || s.state === 'complete'
  })

  /** True while the orchestrator is mid-pipeline. */
  const active = computed<boolean>(() => ACTIVE_STATES.has(status.value.state))

  /** True when the orchestrator has settled and the wizard can finish. */
  const finished = computed<boolean>(() =>
    status.value.state === 'complete' || status.value.state === 'skipped',
  )

  /** Single-line label for the pipeline UI. */
  const stageLabel = computed<string>(() => {
    const s = status.value
    switch (s.state) {
      case 'probing':
        return 'Detecting your hardware…'
      case 'pulling_primary':
        return s.primary.model ? `Downloading ${s.primary.model}` : 'Preparing download…'
      case 'pulling_fallback':
        return s.fallback.model ? `Topping up ${s.fallback.model} in the background` : 'Finishing up'
      case 'running_probe':
        return 'Validating your setup…'
      case 'complete':
        return s.probe_failed ? 'Setup complete (probe deferred)' : 'Setup complete'
      case 'skipped':
        return 'Skipped — pick a model when you are ready'
      case 'failed':
        return s.last_error ?? 'First-run setup failed'
      default:
        return ''
    }
  })

  onMounted(() => {
    _refCount++
    if (_refCount === 1) {
      void fetchOnce().then((data) => {
        if (data && ACTIVE_STATES.has(data.state)) startPolling()
      })
    }
  })

  onBeforeUnmount(() => {
    _refCount--
    if (_refCount <= 0) {
      _refCount = 0
      stopPolling()
    }
  })

  // If status flips into an active state from anywhere (e.g. another tab
  // started the pipeline), pick up polling. If it flips into terminal,
  // wind down.
  watch(
    () => status.value.state,
    (state) => {
      if (ACTIVE_STATES.has(state)) startPolling()
      else if (TERMINAL_STATES.has(state)) stopPolling()
    },
  )

  return {
    status: readonly(status),
    error: readonly(error),
    fetchOnce,
    start,
    chatReady,
    active,
    finished,
    stageLabel,
  }
}
