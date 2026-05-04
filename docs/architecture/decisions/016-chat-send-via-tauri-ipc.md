# ADR 016 — Chat sends route through Tauri IPC; WebSocket carries stream only

**Status:** Accepted
**Date:** 2026-05-04
**Related:** [ADR 003](003-desktop-distribution-tauri-and-sidecars.md), [ADR 015](015-single-target-local-only-stack.md)

## Context

The bundled chat path uses a single persistent WebSocket between the Tauri webview and the FastAPI sidecar. The frontend opens `ws://127.0.0.1:<port>/api/chat/ws` on page load, sends user messages as `{type: "message", …}` frames, and receives streaming `text_delta` / `tool_use` / `done` frames back on the same socket. This was clean and symmetric — until consistent reproduction of a turn-2 latency bug forced a rethink.

### The bug, by the diagnostic numbers

Build #11 added two timestamps to every chat send: `t_enter_ms` (sendMessage entry) and `t_pre_send_ms` (just before `ws.send()`). The backend logs `js_block_ms = t_pre_send − t_enter` and `wire_time_ms = backend_now − t_pre_send`. Across multiple fresh-app sessions on Apple M5 24 GB:

- Turn 1 of every fresh session: `js_block_ms ≈ 0`, `wire_time_ms ≈ 1 ms`.
- Turn 2 of every fresh session: `js_block_ms ≈ 0`, `wire_time_ms = 26 871 ms`.
- Turn 3+ of every fresh session: `js_block_ms ≈ 0`, `wire_time_ms ≈ 1 ms`.

JS execution between `sendMessage` entry and `ws.send()` is effectively instant. The frame sat in transit for **27 seconds** between the `WebSocket.send()` call returning and the backend's `await websocket.receive_text()` resolving. The backend was completely silent during that window — no `chat_step`, no `httpx`, no log lines at all.

### Root cause

macOS WKWebView (which Tauri 2 uses on macOS) applies the same power-management throttling to in-page WebSocket I/O that it applies to other deferrable network activity. After turn 1's response renders and the UI goes quiet, WKWebView decides the view is idle and lowers the WebSocket's I/O priority. When the user clicks send, JS executes immediately, but the frame queues at WKWebView's network layer waiting for an external wakeup — typically the next heartbeat ping at 25 s, which closely matches the observed 27 s tail.

This is consistent with WKWebView's documented behaviour around `NSURLSession` task scheduling under power and thermal pressure, and it is *not* something the page can opt out of from JS. Tauri-only mitigations (forcing the view to stay perceived-active, e.g. via a `requestAnimationFrame` loop or short-interval `setTimeout`) work but are battery-hostile and rely on undocumented heuristics. The throttling does not affect:

- HTTP `fetch` / `XMLHttpRequest` requests with the same lifetime profile.
- Network I/O initiated from the Tauri Rust shell — those go through the host process's network stack, not WKWebView's.
- Streaming **inbound** WebSocket frames (the backend → frontend direction) — once the WKWebView's network thread is awake to receive a frame, subsequent frames flow normally on that socket.

So the throttling specifically targets the **outbound** path of an idle in-page WebSocket. Splitting sends off that path is the structural fix.

## Decision

User chat-message sends route through a Tauri Rust command, **not** through the in-page WebSocket. The streaming response continues to flow back over the existing WebSocket — it is the bidirectional symmetry of the WS that breaks, not the WS itself.

Concretely:

- A new `#[tauri::command] async fn send_chat_message(payload: serde_json::Value) -> Result<(), String>` on the Tauri shell. It POSTs the payload to `<backend_url>/api/chat/message` via a long-lived `reqwest::Client` (loopback HTTP, no TLS).
- The frontend's `useWebSocket.send()` checks `'__TAURI_INTERNALS__' in window` and, when true and the frame is `{type: "message"}`, awaits `invoke('send_chat_message', { payload })` instead of `ws.send(JSON.stringify(payload))`. Heartbeat pings still go over the WS — they need to exercise the same socket the server is streaming on, otherwise the staleness check in `useWebSocket` (introduced for a separate idle-disconnection bug) loses signal.
- On the backend, a new `POST /api/chat/message` endpoint validates the payload, looks up the session's WebSocket in a new `_active_sessions` registry, and dispatches `_handle_message` against it as a background task. The HTTP request returns 200 immediately; streaming flows back over the WS as before. Errors during processing surface as `error` events on the WS, matching the legacy contract.
- The `_active_sessions` registry maps `session_id → {ws, get_llm, lock}`. Populated when a WS handler accepts a session, cleared in the handler's `finally` block (and also any other entries pointing at that WS, defending against stale references after a session-id switch). The per-session `asyncio.Lock` serialises concurrent dispatches so two messages for the same session never run `_handle_message` concurrently — applies whether they arrive over WS or HTTP.

In browser dev mode (`__TAURI_INTERNALS__` absent) the frontend keeps the WS-direct path. The backend supports both paths; either side can land first. The `chat_step received` log line records `transport=ws` or `transport=http` so we can keep an eye on the WKWebView throttling behaviour over time.

## Trade-offs

| Choice | Benefit | Cost |
|---|---|---|
| Split sends off WS into Rust IPC | Eliminates the 27 s WKWebView outbound-throttle stall. Architecturally honest — sends and streams have different lifetime profiles. | Two transport paths to maintain. Backend gains a new HTTP endpoint with its own validation surface. The "everything over WS" property is gone. |
| Long-lived `reqwest::Client` in Tauri state | Connection pool keeps loopback overhead microseconds. | Adds reqwest as a Cargo dep (~no TLS features → ~lean). |
| HTTP returns 200 immediately, dispatch is a background task | UI gets prompt acknowledgement; user can keep typing. | A dispatch failure during streaming surfaces over WS, not via the HTTP response. The browser's `invoke()` can't observe stream-side errors directly. |
| Keep heartbeat pings on WS | The pong on this socket proves *this* socket is alive (used by the staleness detector). | Pings still get throttled the same way as outbound message frames did — but they're harmless when delayed (server-side timeout is generous). |

## Alternatives considered

- **A. Keep everything on WebSocket; force the view active with an RAF loop.** Rejected — relies on macOS's undocumented "active" heuristic, fights the OS power manager, and burns battery.
- **B. Replace WebSocket entirely with HTTP + Server-Sent Events.** Larger refactor than necessary. Inbound stream over WS is not affected by the bug; only outbound sends are. SSE on the inbound side would also lose the bidirectional control channel (`memory_changed`, `compaction`, etc.) which other features depend on.
- **C. Use `tauri-plugin-http` to route fetch through Rust without a custom command.** Would work, but adds a plugin dependency for a single endpoint. The custom command is ~30 lines of Rust and zero plugins.
- **D. Send a wake-up ping immediately before each message.** Possibly works, possibly the ping itself sits in the same throttled queue. Doesn't address the underlying cause.

## Migration

- `_active_sessions` registry is forward-compatible: an older frontend that still uses `ws.send()` works unchanged, because the WS path also goes through `_process_chat_payload` / `_active_sessions`.
- Browser dev mode (`npm run dev` against a hand-launched backend) keeps the WS-direct path because `__TAURI_INTERNALS__` is absent. No dev-flow change.
- The `chat_step received transport=…` field lets us measure the wire-time distribution under each transport empirically, and confirms the fix in production logs without re-running the bug under instrumentation.

## Verification

The bug is reliably reproduced by: launch the bundled .app, send a first message, wait for the response to render, send a second message immediately. Pre-fix behaviour: turn-2 `wire_time_ms` is 25–27 s. Post-fix expected behaviour: turn-2 `wire_time_ms` ≤ a few ms with `transport=http`.

Because the wire-time diagnostic is part of the same change as the fix, a single bundled build verifies both.
