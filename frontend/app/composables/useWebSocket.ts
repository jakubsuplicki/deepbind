import type { WsEvent } from '~/types'

// Treat the connection as stale if no pong has landed in this many ms.
// Heartbeat fires every 25 s, so 30 s gives one full interval plus a
// small grace period for round-trip latency. Anything older than this on
// a send() attempt triggers a force-reconnect rather than trusting that
// `readyState === OPEN` reflects real TCP liveness — see send() below.
const STALENESS_MS = 30_000
const HEARTBEAT_MS = 25_000
// Cap the queue depth so a permanently-dead network can't accumulate
// frames forever. Eight is much higher than the realistic in-flight count
// (the user can only type so fast) but low enough that a runaway will
// drop instead of leak.
const OUTBOUND_QUEUE_MAX = 8

// True when running inside the bundled Tauri shell (production / packaged
// .app). `__TAURI_INTERNALS__` is set by Tauri 2's runtime in `init_script`
// before any user code runs. Used to route chat-message sends through the
// Rust shell's `send_chat_message` command (ADR 016) — bypasses macOS
// WKWebView's outbound WebSocket throttling, which has been measured at
// up to 27 s wire_time on the second send of a fresh app session. In
// browser/dev mode this is false and we keep the WS-direct path.
function _inTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}

export function useWebSocket() {
  const isConnected = ref(false)
  const _ws = ref<WebSocket | null>(null)
  const _listeners = new Set<(event: WsEvent) => void>()
  let _heartbeatTimer: ReturnType<typeof setInterval> | null = null
  let _reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let _reconnectAttempts = 0
  let _intentionalClose = false
  let _hasConnected = false
  let _lastSessionId: string | undefined
  const _reconnectCallbacks = new Set<() => void>()

  // ── Liveness + queueing ─────────────────────────────────────────────
  // Updated every time a pong (or any inbound frame) lands. Used by send()
  // to decide whether the socket is actually alive at the TCP level —
  // `readyState === OPEN` is necessary but not sufficient on macOS where
  // App Nap, OS sleep, and NAT timeouts can silently kill the underlying
  // socket without transitioning the JS WebSocket out of OPEN.
  let _lastPongAt = 0
  // Frames queued while a force-reconnect is in flight. Drained on the
  // next onopen so the user never sees a "lost message" — the message
  // that triggered the reconnect just rides the next connection.
  const _outboundQueue: string[] = []
  // Set when we deliberately close a stale socket. The onclose handler
  // checks this flag and skips broadcasting `disconnected` — consumers
  // (useChat) treat that as connection-loss and reset isLoading, which
  // would flash an error to the user even though the queued message is
  // about to flush onto a fresh connection. The flag is single-use:
  // cleared inside the same onclose call that consumes it.
  let _suppressNextDisconnect = false

  function _getWsUrl(sessionId?: string): string {
    // Tauri shell injects window.__JARVIS_CONFIG__ via init_script before page
    // load (ADR 003 §D). It carries the bound port the sidecar got from the
    // OS, so we always prefer it over the build-time runtimeConfig fallback.
    const tauriBase = (typeof window !== 'undefined' && window.__JARVIS_CONFIG__?.wsUrl) || ''
    let base: string
    if (tauriBase) {
      base = `${tauriBase}/api/chat/ws`
    } else {
      const configured = useRuntimeConfig().public.backendWsUrl as string | undefined
      if (configured) {
        base = configured
      } else {
        const loc = window.location
        const protocol = loc.protocol === 'https:' ? 'wss:' : 'ws:'
        base = `${protocol}//${loc.host}/api/chat/ws`
      }
    }
    if (sessionId) {
      const sep = base.includes('?') ? '&' : '?'
      return `${base}${sep}session_id=${sessionId}`
    }
    return base
  }

  function onReconnect(cb: () => void): () => void {
    _reconnectCallbacks.add(cb)
    return () => _reconnectCallbacks.delete(cb)
  }

  function _drainOutboundQueue(): void {
    while (
      _outboundQueue.length > 0
      && _ws.value
      && _ws.value.readyState === WebSocket.OPEN
    ) {
      const frame = _outboundQueue.shift()!
      _ws.value.send(frame)
    }
  }

  function connect(sessionId?: string): void {
    if (_ws.value && _ws.value.readyState === WebSocket.OPEN) return
    // Already in the middle of opening — don't double-start.
    if (_ws.value && _ws.value.readyState === WebSocket.CONNECTING) return

    if (sessionId) _lastSessionId = sessionId
    _intentionalClose = false
    const ws = new WebSocket(_getWsUrl(_lastSessionId))

    ws.onopen = () => {
      isConnected.value = true
      const isReconnect = _hasConnected
      _hasConnected = true
      _reconnectAttempts = 0
      // Assume liveness at open; the heartbeat below will refresh this
      // continuously, and any inbound frame counts as a liveness signal.
      _lastPongAt = Date.now()
      _heartbeatTimer = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }))
        }
      }, HEARTBEAT_MS)
      // Flush anything queued during a force-reconnect.
      _drainOutboundQueue()
      // Notify listeners that we reconnected (so they can re-init session etc)
      if (isReconnect) {
        for (const cb of _reconnectCallbacks) cb()
      }
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as WsEvent
        // Any inbound frame proves TCP is alive. Pong is the dedicated
        // heartbeat reply; track it but don't broadcast — chat consumers
        // don't care about transport-level events.
        _lastPongAt = Date.now()
        if (data.type === 'pong') return
        for (const listener of _listeners) {
          listener(data)
        }
      } catch {
        // ignore malformed messages
      }
    }

    ws.onclose = () => {
      isConnected.value = false
      _ws.value = null
      _clearHeartbeat()
      const suppress = _suppressNextDisconnect
      _suppressNextDisconnect = false
      if (!suppress) {
        // Notify listeners of disconnect so UI can reset loading state
        for (const listener of _listeners) {
          listener({ type: 'disconnected' } as WsEvent)
        }
      }
      if (!_intentionalClose) {
        _scheduleReconnect()
      }
    }

    ws.onerror = () => {
      isConnected.value = false
    }

    _ws.value = ws
  }

  function _scheduleReconnect(): void {
    // Don't double-schedule — onclose can fire repeatedly during a
    // failure storm and each call would multiply the backoff factor.
    if (_reconnectTimer) return
    _reconnectAttempts++
    // Exponential backoff: 1s, 2s, 4s, 8s, max 15s
    const delay = Math.min(1000 * Math.pow(2, _reconnectAttempts - 1), 15_000)
    _reconnectTimer = setTimeout(() => {
      _reconnectTimer = null
      connect()
    }, delay)
  }

  function _clearHeartbeat(): void {
    if (_heartbeatTimer) {
      clearInterval(_heartbeatTimer)
      _heartbeatTimer = null
    }
  }

  function _isStale(): boolean {
    // _lastPongAt = 0 means we haven't yet received any inbound frame on
    // this socket — caller should treat that as "fresh, not stale" so a
    // first send right after open isn't misclassified.
    if (_lastPongAt === 0) return false
    return Date.now() - _lastPongAt > STALENESS_MS
  }

  function _forceReconnect(): void {
    // Tell onclose to skip the disconnected event — we're closing on
    // purpose so the queued frame can flush onto a new connection.
    _suppressNextDisconnect = true
    _intentionalClose = false
    if (_reconnectTimer) {
      clearTimeout(_reconnectTimer)
      _reconnectTimer = null
    }
    if (_ws.value) {
      try { _ws.value.close() } catch {}
    }
    _ws.value = null
    _clearHeartbeat()
    // Reconnect immediately, bypassing the backoff timer — this isn't a
    // failure recovery, it's a deliberate refresh of a known-stale socket.
    connect(_lastSessionId)
  }

  function send(data: Record<string, unknown>): boolean {
    // Bundled Tauri path (ADR 016): chat-message sends go through the Rust
    // shell's `send_chat_message` command, which POSTs to the sidecar over
    // loopback HTTP. This bypasses WKWebView's outbound WebSocket
    // throttling — without it the second user message of a fresh app
    // session can stall up to 27 s waiting for the WebView's network
    // thread to flush a tiny ~100-byte frame. The streaming response still
    // comes back over the WebSocket; we just route the SEND off the WS.
    //
    // Heartbeat pings stay on the direct WS path so they continue to
    // exercise the same socket the server is streaming on (a pong on this
    // socket is what proves THIS socket is alive). Routing the ping
    // through HTTP would prove the HTTP path is alive but tell us nothing
    // about the WS, defeating the staleness detection below.
    if (_inTauri() && data.type === 'message') {
      // Fire-and-forget — the streaming response will arrive over the WS
      // independently. Errors here mean the HTTP POST itself failed
      // (sidecar down, no active session); surface them as a synthetic
      // disconnected event so consumers can show the "connection lost"
      // banner. We deliberately do NOT await — Vue's reactive updates
      // continue immediately, matching the current ws.send() semantics.
      ;(async () => {
        try {
          const { invoke } = await import('@tauri-apps/api/core')
          await invoke('send_chat_message', { payload: data })
        }
        catch (err) {
          // Treat as a transport-level disconnect so chat consumers
          // (useChat) reset isLoading and show an error.
          for (const listener of _listeners) {
            listener({ type: 'disconnected' } as WsEvent)
          }
          // Surface the underlying message so it isn't swallowed silently.
          // eslint-disable-next-line no-console
          console.error('send_chat_message failed:', err)
        }
      })()
      return true
    }

    const frame = JSON.stringify(data)

    // Fast path: socket is open AND recent pong proves TCP liveness.
    if (
      _ws.value
      && _ws.value.readyState === WebSocket.OPEN
      && !_isStale()
    ) {
      _ws.value.send(frame)
      return true
    }

    // Cap the queue so a permanently-broken connection can't leak frames.
    if (_outboundQueue.length >= OUTBOUND_QUEUE_MAX) {
      return false
    }
    _outboundQueue.push(frame)

    if (_ws.value && _ws.value.readyState === WebSocket.OPEN) {
      // Socket is OPEN at the JS layer but pongs have stopped arriving —
      // TCP is most likely dead (App Nap / OS sleep / NAT timeout silently
      // killed the underlying connection). Force-close and reconnect so
      // the queued frame can flush onto a fresh connection. Without this
      // path, ws.send() would appear to succeed and the user-facing wait
      // would stretch to ~23 s while the OS retransmit timer eventually
      // gave up.
      _forceReconnect()
    } else if (!_reconnectTimer && (!_ws.value || _ws.value.readyState !== WebSocket.CONNECTING)) {
      // Not open, not already connecting, no reconnect scheduled — kick one.
      _scheduleReconnect()
    }

    return true
  }

  function onMessage(listener: (event: WsEvent) => void): () => void {
    _listeners.add(listener)
    return () => _listeners.delete(listener)
  }

  function close(): void {
    _intentionalClose = true
    _clearHeartbeat()
    if (_reconnectTimer) {
      clearTimeout(_reconnectTimer)
      _reconnectTimer = null
    }
    _outboundQueue.length = 0
    _ws.value?.close()
    _ws.value = null
    isConnected.value = false
  }

  onUnmounted(() => {
    close()
  })

  function setSessionId(id: string): void {
    _lastSessionId = id
  }

  return { isConnected, connect, send, onMessage, close, onReconnect, setSessionId }
}
