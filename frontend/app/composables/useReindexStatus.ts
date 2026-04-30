/**
 * useReindexStatus — polls /api/memory/reindex/status while the embedding
 * reindex supervisor (ADR 003 §I) is running, so the StatusBar can surface
 * a non-blocking "indexing your vault…" pill on cold start.
 *
 * Lifecycle:
 *   - On mount, fetches once immediately.
 *   - If state === 'running', polls every POLL_INTERVAL_MS until idle/failed.
 *   - When state goes idle, keeps the snapshot for IDLE_DISPLAY_MS so the
 *     user sees "Indexed N notes" briefly, then hides.
 *   - When state goes failed, keeps showing until manually dismissed; the
 *     user can re-trigger via POST /api/memory/reindex-embeddings.
 *
 * Cross-instance safety: reuses Nuxt's useState so all StatusBar instances
 * share one polling timer.
 */

export type ReindexState = 'idle' | 'running' | 'failed'

export interface ReindexStatus {
  state: ReindexState
  started_at: number | null
  finished_at: number | null
  scanned: number
  total: number
  progress_pct: number
  last_error: string | null
  last_run_count: number
}

const POLL_INTERVAL_MS = 2000
const IDLE_DISPLAY_MS = 5000

const DEFAULT: ReindexStatus = {
  state: 'idle',
  started_at: null,
  finished_at: null,
  scanned: 0,
  total: 0,
  progress_pct: 0,
  last_error: null,
  last_run_count: 0,
}

let _timer: ReturnType<typeof setInterval> | null = null
let _idleHideTimer: ReturnType<typeof setTimeout> | null = null
let _refCount = 0

export function useReindexStatus() {
  const status = useState<ReindexStatus>('reindex-status', () => ({ ...DEFAULT }))
  const visible = useState<boolean>('reindex-status-visible', () => false)
  const dismissed = useState<boolean>('reindex-status-dismissed', () => false)

  async function fetchOnce(): Promise<void> {
    try {
      const data = await $fetch<ReindexStatus>(apiUrl('/api/memory/reindex/status'))
      const prev = status.value.state
      status.value = data
      if (data.state === 'running') {
        visible.value = true
        dismissed.value = false
        if (_idleHideTimer) {
          clearTimeout(_idleHideTimer)
          _idleHideTimer = null
        }
      } else if (data.state === 'idle') {
        if (prev === 'running' && !dismissed.value) {
          // Keep the pill briefly so the user sees "Done — N indexed".
          if (_idleHideTimer) clearTimeout(_idleHideTimer)
          _idleHideTimer = setTimeout(() => {
            visible.value = false
            _idleHideTimer = null
          }, IDLE_DISPLAY_MS)
        } else {
          visible.value = false
        }
      } else if (data.state === 'failed') {
        visible.value = !dismissed.value
      }
    } catch {
      // Backend not yet up (cold start race) — try again on the next tick.
    }
  }

  function startPolling(): void {
    if (_timer !== null) return
    _timer = setInterval(() => {
      void fetchOnce()
    }, POLL_INTERVAL_MS)
  }

  function stopPolling(): void {
    if (_timer !== null) {
      clearInterval(_timer)
      _timer = null
    }
  }

  async function retry(): Promise<void> {
    dismissed.value = false
    try {
      await $fetch(apiUrl('/api/memory/reindex-embeddings'), { method: 'POST' })
    } catch {
      // /status will surface the failure on next poll.
    }
    await fetchOnce()
    startPolling()
  }

  function dismiss(): void {
    dismissed.value = true
    visible.value = false
  }

  onMounted(() => {
    _refCount++
    if (_refCount === 1) {
      void fetchOnce().then(() => {
        if (status.value.state === 'running') startPolling()
      })
    }
  })

  onBeforeUnmount(() => {
    _refCount--
    if (_refCount <= 0) {
      _refCount = 0
      stopPolling()
      if (_idleHideTimer) {
        clearTimeout(_idleHideTimer)
        _idleHideTimer = null
      }
    }
  })

  // Whenever the running flag flips on (e.g. after a manual reindex POST),
  // make sure the timer is on.
  watch(
    () => status.value.state,
    (state) => {
      if (state === 'running') startPolling()
      else if (state === 'idle' && !_idleHideTimer) stopPolling()
      else if (state === 'failed') stopPolling()
    },
  )

  return {
    status: readonly(status),
    visible: readonly(visible),
    fetchOnce,
    retry,
    dismiss,
  }
}
