import type { ChatMessage, TraceItem, WsEvent } from '~/types'
import { useChatHealth } from '~/composables/useChatHealth'
import { useChatModel } from '~/composables/useChatModel'
import { useLocalModels } from '~/composables/useLocalModels'
import { useWebSocket } from '~/composables/useWebSocket'

export function useChat() {
  const messages = ref<ChatMessage[]>([])
  const currentResponse = ref('')
  const isLoading = ref(false)
  const toolActivity = ref('')
  const error = ref('')
  const sessionId = ref('')
  const canRetry = ref(false)
  let _lastContent = ''
  // Step 28a — trace event arrives between text_delta and done; held here
  // until `done` flushes the assistant message into the history.
  let _pendingTrace: TraceItem[] | null = null
  let _errorClearTimer: ReturnType<typeof setTimeout> | null = null
  let _slowTimer10: ReturnType<typeof setTimeout> | null = null
  let _slowTimer30: ReturnType<typeof setTimeout> | null = null
  const slowResponse = ref('')
  let _removeMessageListener: (() => void) | null = null
  let _removeReconnectListener: (() => void) | null = null

  const { isConnected, connect, send, onMessage, close, onReconnect, setSessionId } = useWebSocket()
  const chatHealth = useChatHealth()

  function _setError(msg: string, retryable = false): void {
    error.value = msg
    canRetry.value = retryable
    if (_errorClearTimer) clearTimeout(_errorClearTimer)
    _errorClearTimer = setTimeout(() => { error.value = ''; canRetry.value = false }, 8000)
  }

  function _startSlowTimer(): void {
    _clearSlowTimer()
    // ADR 015 — single dispatch target (Ollama), so the slow-load timers
    // always engage. The previous activeProvider check is dead code.
    _slowTimer10 = setTimeout(() => {
      if (isLoading.value && !currentResponse.value) {
        slowResponse.value = 'Local model is loading... This may take a moment.'
      }
    }, 10_000)
    _slowTimer30 = setTimeout(() => {
      if (isLoading.value && !currentResponse.value) {
        slowResponse.value = 'Still generating... Local models can be slow on CPU. Consider a smaller model if this is too slow.'
      }
    }, 30_000)
  }

  function _clearSlowTimer(): void {
    if (_slowTimer10) { clearTimeout(_slowTimer10); _slowTimer10 = null }
    if (_slowTimer30) { clearTimeout(_slowTimer30); _slowTimer30 = null }
    slowResponse.value = ''
  }

  function _handleEvent(event: WsEvent): void {
    if (event.type === 'session_start') {
      sessionId.value = event.session_id
      setSessionId(event.session_id)
      try { sessionStorage.setItem('jarvis_session_id', event.session_id) } catch {}
      return
    }

    if (event.type === 'session_history') {
      // Restore chat history after reconnect/refresh (only if UI is empty)
      if (messages.value.length === 0 && Array.isArray(event.messages)) {
        messages.value = event.messages
      }
      return
    }

    if (event.type === 'text_delta') {
      currentResponse.value += event.content
      _clearSlowTimer()
      return
    }

    if (event.type === 'tool_use') {
      const label = _toolLabel(event.name)
      toolActivity.value = label
      return
    }

    if (event.type === 'tool_result') {
      toolActivity.value = ''
      return
    }

    if (event.type === 'memory_changed') {
      window.dispatchEvent(new CustomEvent('jarvis:memory-changed', { detail: event }))
      return
    }

    if (event.type === 'trace') {
      _pendingTrace = Array.isArray(event.items) ? event.items : null
      return
    }

    if (event.type === 'done') {
      if (currentResponse.value) {
        const msg: ChatMessage = {
          role: 'assistant',
          content: currentResponse.value,
          model: event.model,
          provider: event.provider,
          timestamp: new Date().toISOString(),
        }
        if (_pendingTrace && _pendingTrace.length > 0) {
          msg.trace = _pendingTrace
        }
        if (event.metrics) {
          msg.metrics = event.metrics
        }
        messages.value.push(msg)
        currentResponse.value = ''
        // ADR 005 §C trigger 2 — feed the just-completed turn's decode
        // throughput into the health watcher. The watcher rolls a per-
        // model window and emits a soft hint when sustained-slow vs
        // probe baseline (or sustained-fast with a heavier rung
        // installed). Cooldowns and policy live inside the watcher;
        // this is a pure forward.
        if (event.metrics?.decode_tps != null && event.model) {
          chatHealth.recordTurn(event.model, event.metrics.decode_tps)
        }
      }
      _pendingTrace = null
      isLoading.value = false
      toolActivity.value = ''
      _clearSlowTimer()
      return
    }

    if (event.type === 'error') {
      const content = event.content || 'Something went wrong.'
      const retryable = /try again|overloaded|rate limit|reconnect/i.test(content)
      _setError(content, retryable)
      _pendingTrace = null
      isLoading.value = false
      toolActivity.value = ''
      _clearSlowTimer()
      return
    }

    // WebSocket disconnected mid-response — reset loading state
    if (event.type === 'disconnected') {
      if (isLoading.value) {
        if (currentResponse.value) {
          messages.value.push({ role: 'assistant', content: currentResponse.value + ' *(connection lost)*' })
          currentResponse.value = ''
        }
        _pendingTrace = null
        isLoading.value = false
        toolActivity.value = ''
        _setError('Connection lost — reconnecting...', true)
      }
    }
  }

  function _toolLabel(name: string): string {
    const labels: Record<string, string> = {
      search_notes: 'Searching notes...',
      read_note: 'Reading note...',
      write_note: 'Writing note...',
      append_note: 'Updating note...',
    }
    return labels[name] ?? `Running ${name}...`
  }

  function init(): void {
    // Clean up previous listeners if re-initializing (e.g. new session)
    if (_removeMessageListener) {
      _removeMessageListener()
      _removeMessageListener = null
    }
    if (_removeReconnectListener) {
      _removeReconnectListener()
      _removeReconnectListener = null
    }
    // Restore session ID from sessionStorage so page refreshes resume the same session
    const stored = (() => { try { return sessionStorage.getItem('jarvis_session_id') } catch { return null } })()
    if (stored && !sessionId.value) {
      sessionId.value = stored
    }
    // Always sync _lastSessionId — clears stale ID when starting a new session
    setSessionId(sessionId.value || '')
    connect(sessionId.value || undefined)
    _removeMessageListener = onMessage(_handleEvent)
    // When WS reconnects, clear transient error state.
    // The session_id is already passed via _lastSessionId in useWebSocket
    // so the backend will resume the same session.
    _removeReconnectListener = onReconnect(() => {
      error.value = ''
      canRetry.value = false
    })
  }

  function sendMessage(content: string, options?: { graphScope?: string }): void {
    if (!content.trim() || isLoading.value) return

    _lastContent = content.trim()
    messages.value.push({ role: 'user', content: _lastContent, timestamp: new Date().toISOString() })
    currentResponse.value = ''
    error.value = ''
    canRetry.value = false
    isLoading.value = true
    _startSlowTimer()

    const payload: Record<string, string> = { type: 'message', content: _lastContent, session_id: sessionId.value }
    if (options?.graphScope) payload.graph_scope = options.graphScope

    // ADR 015 — single dispatcher (local Ollama). Attach the chosen model;
    // base_url is forwarded only when the user has set an explicit override
    // in Settings → Advanced. In the bundled product the backend's
    // JARVIS_OLLAMA_BASE_URL env var (set by the Tauri shell to the private
    // bundled-Ollama port) wins by default. No API key, no provider abstraction.
    const { activeModel } = useChatModel()
    const { baseUrl } = useLocalModels()
    payload.provider = 'ollama'
    payload.model = activeModel.value
    if (baseUrl.value) payload.base_url = baseUrl.value

    const sent = send(payload)
    if (!sent) {
      // WS not ready — reset and show error
      isLoading.value = false
      _setError('Not connected — reconnecting, try again in a moment.', true)
    }
  }

  function retry(): void {
    if (!_lastContent || isLoading.value) return
    // Remove the last user message to re-send cleanly
    const last = messages.value[messages.value.length - 1]
    if (last?.role === 'user' && last.content === _lastContent) {
      messages.value.pop()
    }
    error.value = ''
    canRetry.value = false
    sendMessage(_lastContent)
  }

  function disconnect(): void {
    if (_errorClearTimer) {
      clearTimeout(_errorClearTimer)
      _errorClearTimer = null
    }
    _clearSlowTimer()
    if (_removeMessageListener) {
      _removeMessageListener()
      _removeMessageListener = null
    }
    if (_removeReconnectListener) {
      _removeReconnectListener()
      _removeReconnectListener = null
    }
    close()
  }

  onUnmounted(() => {
    disconnect()
  })

  return {
    messages,
    currentResponse,
    isLoading,
    toolActivity,
    error,
    canRetry,
    sessionId,
    isConnected,
    slowResponse,
    init,
    sendMessage,
    retry,
    disconnect,
  }
}
