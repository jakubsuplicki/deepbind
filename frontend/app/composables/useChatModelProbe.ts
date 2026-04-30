import type {
  ChatModelProbeEvent,
  ChatModelProbeEvidence,
  ChatModelProbeRecord,
  ChatModelProbeRerunReason,
  ChatModelProbeStatus,
} from '~/types'

/**
 * Composable for the install-time chat-model self-test (ADR 012).
 *
 * Owns three pieces of UI state:
 * - ``status`` — the GET response (persisted record + needs_rerun flag).
 * - ``events`` — live SSE events from a running probe; lets onboarding
 *   render per-candidate progress (``probing 1/3: qwen3:30b…``).
 * - ``running`` — true while a probe is in flight.
 *
 * Used by OnboardingLocalFlow (auto-runs the probe after a model is
 * downloaded), LocalModelsSection (settings re-run + override), and
 * pages/main.vue (boot detection — banner when needs_rerun is true).
 */
export function useChatModelProbe() {
  const status = useState<ChatModelProbeStatus | null>('chat-model-probe-status', () => null)
  const events = useState<ChatModelProbeEvent[]>('chat-model-probe-events', () => [])
  const running = useState<boolean>('chat-model-probe-running', () => false)
  const error = useState<string | null>('chat-model-probe-error', () => null)

  let _abort: AbortController | null = null

  async function fetchStatus(baseUrl?: string): Promise<ChatModelProbeStatus | null> {
    try {
      const params = baseUrl ? { base_url: baseUrl } : {}
      const data = await $fetch<ChatModelProbeStatus>(apiUrl('/api/local/chat-model-probe'), { params })
      status.value = data
      error.value = null
      return data
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : 'Failed to read probe status'
      return null
    }
  }

  /**
   * Open an SSE stream against /run; push events into ``events`` so the
   * caller can render per-candidate progress. Resolves with the final
   * persisted record (or null if the run errored).
   *
   * Refuses to start if a probe is already running for this composable
   * instance — guards against the user double-clicking "Re-run probe".
   */
  async function runProbe(baseUrl?: string): Promise<ChatModelProbeRecord | null> {
    if (running.value) return null
    running.value = true
    events.value = []
    error.value = null
    _abort = new AbortController()

    try {
      const url = apiUrl(`/api/local/chat-model-probe/run${baseUrl ? `?base_url=${encodeURIComponent(baseUrl)}` : ''}`)
      const response = await fetch(url, {
        method: 'POST',
        signal: _abort.signal,
      })

      if (response.status === 503) {
        error.value = 'Ollama runtime is not reachable. Start Ollama and try again.'
        return null
      }
      if (!response.ok || !response.body) {
        error.value = `Probe failed: ${response.status}`
        return null
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let final: ChatModelProbeRecord | null = null
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const parsed = JSON.parse(line.slice(6)) as ChatModelProbeEvent
            events.value = [...events.value, parsed]
            if (parsed.event === 'complete') {
              final = parsed.result
            } else if (parsed.event === 'error') {
              error.value = parsed.message
            }
          } catch {
            // Ignore malformed SSE lines
          }
        }
      }

      // Refresh ``status`` so consumers see the new persisted record.
      if (final) await fetchStatus(baseUrl)
      return final
    } catch (e: unknown) {
      if ((e as DOMException)?.name === 'AbortError') return null
      error.value = e instanceof Error ? e.message : 'Probe failed'
      return null
    } finally {
      running.value = false
      _abort = null
    }
  }

  function cancelProbe(): void {
    if (_abort) {
      _abort.abort()
      _abort = null
    }
    running.value = false
  }

  /** Set or clear the user override. ``model: null`` reverts to recommendation. */
  async function setOverride(model: string | null): Promise<void> {
    try {
      await $fetch(apiUrl('/api/local/chat-model-probe/override'), {
        method: 'POST',
        body: { model },
      })
      await fetchStatus()
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : 'Failed to set override'
    }
  }

  /**
   * The model the chat router will dispatch to: user_override wins
   * unconditionally; otherwise recommended_model; otherwise null.
   */
  const effectiveModel = computed<string | null>(() => {
    const r = status.value?.persisted
    if (!r) return null
    return r.user_override ?? r.recommended_model
  })

  /** True when a re-run is needed (boot-time banner trigger). */
  const needsRerun = computed<boolean>(() => Boolean(status.value?.needs_rerun))

  const rerunReason = computed<ChatModelProbeRerunReason | null>(() =>
    status.value?.rerun_reason ?? null,
  )

  const lastEvidence = computed<ChatModelProbeEvidence[]>(() =>
    status.value?.persisted?.candidates_evaluated ?? [],
  )

  /** Live progress text for the streaming UI ("Probing 2/3: qwen3:14b"). */
  const progressLabel = computed<string | null>(() => {
    if (!running.value) return null
    const last = events.value[events.value.length - 1]
    if (!last) return 'Starting probe…'
    if (last.event === 'started') {
      return `Probing ${last.candidate_count} candidate${last.candidate_count === 1 ? '' : 's'}…`
    }
    if (last.event === 'candidate_start') {
      return `Probing ${last.index + 1}/${last.candidate_count}: ${last.model}`
    }
    if (last.event === 'candidate_evidence') {
      return `Evaluated ${last.evidence.model} → ${last.evidence.verdict}`
    }
    if (last.event === 'complete') {
      return last.result.recommended_model
        ? `Recommended: ${last.result.recommended_model}`
        : 'No candidate passed all probes'
    }
    if (last.event === 'error') {
      return `Error: ${last.message}`
    }
    return null
  })

  return {
    status,
    events,
    running,
    error,
    fetchStatus,
    runProbe,
    cancelProbe,
    setOverride,
    effectiveModel,
    needsRerun,
    rerunReason,
    lastEvidence,
    progressLabel,
  }
}
