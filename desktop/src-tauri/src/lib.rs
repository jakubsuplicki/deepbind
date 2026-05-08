//! DeepFilesAI desktop shell — ADR 003 graduation (G4a: bundled Ollama).
//!
//! Boot sequence (production / bundled):
//!   1. Resolve the bundled Ollama runtime payload under
//!      `<resource_dir>/ollama-runtime/` (shipped via Tauri's
//!      `bundle.resources` — see desktop/scripts/fetch-ollama.sh and
//!      tauri.conf.json).
//!   2. Spawn `ollama serve` on a private loopback port (11435 by default —
//!      one off from upstream's 11434 to avoid clashing with a user's
//!      separately-installed Ollama, per ADR 003 §"Coexistence").
//!      Models are stored under `<app_data>/ollama-models/` so uninstall
//!      cleanly removes them (driver #2).
//!   3. Wait for ollama to bind the port (TCP probe, 10 s deadline).
//!   4. Spawn the bundled Python sidecar (`jarvis-sidecar-<triple>`) with
//!      `JARVIS_OLLAMA_BASE_URL=http://127.0.0.1:11435` so the FastAPI
//!      backend talks to *our* Ollama, not whatever else is on the box.
//!   5. Read the sidecar's stdout until we see the READY handshake:
//!        JARVIS_BACKEND_READY host=<host> port=<port>
//!   6. Inject `BackendConfig` as `window.__JARVIS_CONFIG__` via
//!      initialization_script and create the window.
//!   7. On shell exit, kill both children. Tauri binds child lifetime to
//!      shell lifetime; the explicit kill prevents the brief orphan window
//!      during teardown and matches ADR 003's process-supervision discipline.
//!
//! Dev mode (`JARVIS_DEV_BACKEND_URL` set) bypasses *both* spawns and trusts
//! the developer's existing Ollama + manually-launched backend. See
//! desktop/scripts/dev.sh.

mod license;

use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use tauri::{
    async_runtime, AppHandle, Manager, RunEvent, State, WebviewUrl, WebviewWindowBuilder,
};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tokio::sync::mpsc;
use tokio::time::timeout;

#[derive(Clone, Debug, Serialize)]
struct BackendConfig {
    backend_url: String,
    ws_url: String,
}

const OLLAMA_HOST: &str = "127.0.0.1";
const OLLAMA_PORT: u16 = 11435;
const OLLAMA_READY_DEADLINE: Duration = Duration::from_secs(10);
const SIDECAR_READY_DEADLINE: Duration = Duration::from_secs(30);

/// Drains the sidecar's stdout until the READY line is observed (or timeout
/// elapses). Line shape is fixed by `backend/scripts/run_frozen.py`.
async fn await_sidecar_ready(
    mut rx: mpsc::Receiver<CommandEvent>,
    deadline: Duration,
) -> Result<BackendConfig, String> {
    let parser = |line: &str| -> Option<BackendConfig> {
        let line = line.trim();
        if !line.starts_with("JARVIS_BACKEND_READY") {
            return None;
        }
        let mut host: Option<String> = None;
        let mut port: Option<u16> = None;
        for tok in line.split_whitespace().skip(1) {
            if let Some(v) = tok.strip_prefix("host=") {
                host = Some(v.to_string());
            } else if let Some(v) = tok.strip_prefix("port=") {
                port = v.parse::<u16>().ok();
            }
        }
        let (host, port) = (host?, port?);
        Some(BackendConfig {
            backend_url: format!("http://{host}:{port}"),
            ws_url: format!("ws://{host}:{port}"),
        })
    };

    let work = async move {
        while let Some(evt) = rx.recv().await {
            match evt {
                CommandEvent::Stdout(bytes) | CommandEvent::Stderr(bytes) => {
                    let text = String::from_utf8_lossy(&bytes);
                    for raw_line in text.split_inclusive('\n') {
                        let trimmed = raw_line.trim_end();
                        if trimmed.is_empty() {
                            continue;
                        }
                        log::info!("sidecar > {trimmed}");
                        if let Some(cfg) = parser(trimmed) {
                            return Ok(cfg);
                        }
                    }
                }
                CommandEvent::Terminated(payload) => {
                    return Err(format!(
                        "sidecar exited before READY (code={:?}, signal={:?})",
                        payload.code, payload.signal
                    ));
                }
                CommandEvent::Error(err) => {
                    return Err(format!("sidecar IO error: {err}"));
                }
                _ => {}
            }
        }
        Err("sidecar stdout closed before READY".to_string())
    };

    match timeout(deadline, work).await {
        Ok(res) => res,
        Err(_) => Err(format!(
            "timed out waiting for sidecar READY after {:?}",
            deadline
        )),
    }
}

/// Block until the bundled ollama is accepting connections on `addr`, or the
/// deadline elapses. We use a TCP-connect probe rather than an HTTP GET
/// because (a) it's std-only — no http client dep — and (b) ollama doesn't
/// `Listen()` on the port until `serve` is fully wired.
fn await_ollama_ready(addr: SocketAddr, deadline: Duration) -> Result<(), String> {
    let start = Instant::now();
    let mut last_err: Option<std::io::Error> = None;
    while start.elapsed() < deadline {
        match TcpStream::connect_timeout(&addr, Duration::from_millis(200)) {
            Ok(_) => {
                log::info!(
                    "ollama bound {} after {:?}",
                    addr,
                    start.elapsed()
                );
                return Ok(());
            }
            Err(e) => last_err = Some(e),
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    Err(format!(
        "ollama did not bind {} within {:?} (last error: {:?})",
        addr, deadline, last_err
    ))
}

/// Spawn the bundled Ollama runtime as a child process. Returns the handle so
/// the shell can kill it on exit.
///
/// Working directory is set to the runtime dir so the binary's @loader_path
/// rpath resolves the sibling dylibs (libggml-base, mlx_metal_v*) without any
/// DYLD_LIBRARY_PATH gymnastics. OLLAMA_MODELS is rooted under app-data so
/// the directory is removed on uninstall.
fn spawn_ollama(app: &AppHandle) -> Result<Child, String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|e| format!("resource_dir lookup failed: {e}"))?;
    let runtime_dir: PathBuf = resource_dir.join("ollama-runtime");
    let ollama_bin = runtime_dir.join("ollama");
    if !ollama_bin.exists() {
        return Err(format!(
            "bundled ollama not found at {} \
             (run desktop/scripts/fetch-ollama.sh)",
            ollama_bin.display()
        ));
    }

    let app_data_dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("app_data_dir lookup failed: {e}"))?;
    let models_dir = app_data_dir.join("ollama-models");
    std::fs::create_dir_all(&models_dir)
        .map_err(|e| format!("failed to create {}: {}", models_dir.display(), e))?;

    let host_arg = format!("{OLLAMA_HOST}:{OLLAMA_PORT}");

    log::info!(
        "spawning ollama: bin={} models={} host={}",
        ollama_bin.display(),
        models_dir.display(),
        host_arg
    );

    let child = Command::new(&ollama_bin)
        .arg("serve")
        .current_dir(&runtime_dir)
        .env("OLLAMA_HOST", &host_arg)
        .env("OLLAMA_MODELS", &models_dir)
        // 5 m keep-alive — matches ADR 003 §B's "warm chat model in RAM
        // between turns without perpetually pinning VRAM/RAM."
        .env("OLLAMA_KEEP_ALIVE", "5m")
        // Drop stdout/stderr to avoid filling the pipe buffer (ollama is
        // chatty during model load). If we ever need the log, switch to
        // a tee that writes to the app log dir.
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .stdin(Stdio::null())
        .spawn()
        .map_err(|e| format!("ollama spawn failed: {e}"))?;

    Ok(child)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            send_chat_message,
            license::license_get_state,
            license::license_install_text,
            license::license_clear,
            license::license_open_data_folder,
        ])
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            // Dev-mode bypass: trust the developer's already-running stack
            // (their host Ollama + a hand-launched backend). See
            // desktop/scripts/dev.sh.
            let cfg = match std::env::var("JARVIS_DEV_BACKEND_URL").ok() {
                Some(ref url) if !url.is_empty() => {
                    log::info!("dev mode: JARVIS_DEV_BACKEND_URL={url}");
                    let ws_url = url
                        .replacen("https://", "wss://", 1)
                        .replacen("http://", "ws://", 1);
                    BackendConfig {
                        backend_url: url.clone(),
                        ws_url,
                    }
                }
                _ => {
                    // 1. Spawn bundled ollama and wait for it to bind :11435.
                    let ollama_child = spawn_ollama(app.handle())
                        .map_err(|e| format!("ollama spawn failed: {e}"))?;
                    let ollama_addr: SocketAddr =
                        format!("{OLLAMA_HOST}:{OLLAMA_PORT}").parse().unwrap();
                    if let Err(e) =
                        await_ollama_ready(ollama_addr, OLLAMA_READY_DEADLINE)
                    {
                        // We started a child but it never bound. Kill it
                        // before bubbling the error so we don't leak a
                        // process when Tauri tears the app down.
                        let mut c = ollama_child;
                        let _ = c.kill();
                        return Err(format!("ollama not ready: {e}").into());
                    }
                    app.manage(OllamaHandle(Mutex::new(Some(ollama_child))));
                    let ollama_base_url = format!("http://{ollama_addr}");
                    log::info!("ollama ready at {ollama_base_url}");

                    // 2. Spawn jarvis-sidecar pointed at our ollama. Pass our
                    // PID so the sidecar's watchdog can self-terminate on a
                    // hard shell exit (SIGKILL / force-quit). See
                    // backend/scripts/run_frozen.py and ADR 003 §"Negative"
                    // §zombies.
                    let shell_pid = std::process::id().to_string();
                    // CORS: the bundled webview origin is platform-specific
                    // (`tauri://localhost` on macOS, `https://tauri.localhost`
                    // on Windows). Pass both so the same shell binary is
                    // portable when we add Windows. Dev mode bypasses this
                    // path entirely (JARVIS_DEV_BACKEND_URL branch above).
                    // Without this env var the FastAPI default
                    // `["http://localhost:3000"]` rejects the webview's
                    // requests with no Access-Control-Allow-Origin header,
                    // surfacing in the UI as "Failed to fetch model catalog".
                    let sidecar = app
                        .shell()
                        .sidecar("jarvis-sidecar")
                        .map_err(|e| format!("sidecar lookup failed: {e}"))?
                        .env("JARVIS_SHELL_PID", shell_pid)
                        .env("JARVIS_OLLAMA_BASE_URL", &ollama_base_url)
                        .env(
                            "JARVIS_CORS_ORIGINS",
                            "tauri://localhost,https://tauri.localhost",
                        );
                    let (rx, child) = sidecar
                        .spawn()
                        .map_err(|e| format!("sidecar spawn failed: {e}"))?;

                    // 3. Wait for the READY line (synchronous block on the
                    // async runtime — Tauri's setup hook is sync).
                    let cfg = async_runtime::block_on(await_sidecar_ready(
                        rx,
                        SIDECAR_READY_DEADLINE,
                    ))
                    .map_err(|e| format!("backend handshake failed: {e}"))?;

                    log::info!(
                        "sidecar ready: backend_url={}, ws_url={}",
                        cfg.backend_url,
                        cfg.ws_url
                    );

                    app.manage(SidecarHandle(Mutex::new(Some(child))));
                    cfg
                }
            };

            // Make the backend URL + a shared reqwest client available to
            // `send_chat_message`. The client disables connection pooling
            // (`pool_max_idle_per_host(0)`) — every chat-message POST opens
            // a fresh TCP connection to the loopback sidecar. Reason: the
            // pool used to keep idle connections for 60 s, but uvicorn's
            // default HTTP keep-alive is 5 s. Between two user turns
            // (typically 10-30 s) the server side would close the
            // connection while the pool still cached it; the next POST
            // would pull a half-closed socket from the pool, write into
            // the kernel buffer, and stall ~18 s waiting for a response
            // that never came (build #11 chat_step received decomposition:
            // `rust_to_fastapi_ms=18211.6` while `js_to_rust_ms=10.0` and
            // `fastapi_to_lock_ms=0.1`). Cost of disabling pooling on
            // loopback is ~100 µs per POST — orders of magnitude smaller
            // than the stale-connection stall it eliminates.
            app.manage(BackendUrlHandle(cfg.backend_url.clone()));
            app.manage(HttpClient(
                reqwest::Client::builder()
                    .pool_max_idle_per_host(0)
                    .build()
                    .map_err(|e| format!("reqwest client build failed: {e}"))?,
            ));

            // 4. Probe the license state once at boot so the first paint
            // can decide between trial-banner / wall / settings without a
            // round-trip. ADR 019 — the wall must be present from the
            // very first paint when the trial has expired (or a license
            // is invalid), otherwise gated content flickers visible for
            // the duration of the network hop.
            let initial_license_state = {
                let url_handle = app.state::<BackendUrlHandle>();
                let http_handle = app.state::<HttpClient>();
                license::boot_state_blocking(app.handle(), &url_handle.0, &http_handle.0)
            };

            // 5. Build the window with config + license state baked in via
            // init_script, so the page loads with both globals already set.
            let cfg_json = serde_json::to_string(&serde_json::json!({
                "backendUrl": cfg.backend_url,
                "wsUrl": cfg.ws_url,
            }))
            .map_err(|e| format!("config serialize failed: {e}"))?;
            let license_json = serde_json::to_string(&initial_license_state)
                .map_err(|e| format!("license-state serialize failed: {e}"))?;
            let init_script = format!(
                "window.__JARVIS_CONFIG__ = {cfg_json}; \
                 window.__JARVIS_LICENSE_STATE__ = {license_json};"
            );

            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("DeepFilesAI")
                .inner_size(960.0, 720.0)
                .initialization_script(&init_script)
                .resizable(true)
                .build()
                .map_err(|e| format!("window build failed: {e}"))?;

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            // ADR 019 chunk 5 — `.deepfileslic` file association handler.
            // Fires when the OS hands us a file (double-click in Finder,
            // attachment from welcome email). We read the file content
            // and emit a `license:file_opened` event; the frontend layout
            // listens, calls the `license_install_text` command, and the
            // existing wall/banner reactions take it from there.
            //
            // We do the file read synchronously here (in the run handler)
            // because Tauri's RunEvent::Opened is delivered synchronously
            // and we want to surface read failures right away. The HTTP
            // round-trip to the sidecar happens in the frontend command
            // path so it can show a spinner / handle errors uniformly
            // with the paste-a-key flow.
            if let RunEvent::Opened { ref urls } = event {
                use tauri::Emitter;
                for url in urls {
                    let path = match url.to_file_path() {
                        Ok(p) => p,
                        Err(_) => {
                            log::warn!(
                                "license: ignoring non-file URL {url}"
                            );
                            continue;
                        }
                    };
                    match std::fs::read_to_string(&path) {
                        Ok(text) => {
                            log::info!(
                                "license: file_opened — {} ({} bytes)",
                                path.display(),
                                text.len()
                            );
                            if let Err(e) =
                                app.emit("license:file_opened", text)
                            {
                                log::warn!(
                                    "license: emit license:file_opened failed: {e}"
                                );
                            }
                        }
                        Err(e) => {
                            log::warn!(
                                "license: read {} failed: {}",
                                path.display(),
                                e
                            );
                        }
                    }
                }
            }

            // On exit, kill both children. Tauri binds child lifetime to the
            // shell, but explicit kill prevents the brief orphan window
            // during teardown and matches ADR 003's process-supervision
            // discipline. Order: jarvis-sidecar first (so it stops issuing
            // requests), then ollama.
            if matches!(
                event,
                RunEvent::ExitRequested { .. } | RunEvent::Exit
            ) {
                if let Some(handle) = app.try_state::<SidecarHandle>() {
                    if let Ok(mut guard) = handle.0.lock() {
                        if let Some(child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
                if let Some(handle) = app.try_state::<OllamaHandle>() {
                    if let Ok(mut guard) = handle.0.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                            // Reap so we don't leave a zombie.
                            let _ = child.wait();
                        }
                    }
                }
            }
        });
}

struct SidecarHandle(Mutex<Option<CommandChild>>);
struct OllamaHandle(Mutex<Option<Child>>);

/// Holds the resolved sidecar HTTP base URL so the `send_chat_message`
/// command knows where to POST. Populated in the setup hook after the
/// READY handshake and never mutated after.
struct BackendUrlHandle(String);

/// Long-lived reqwest client reused across `send_chat_message` invocations.
/// Loopback-only, plain HTTP, so no TLS feature flags. Shared across calls
/// so we don't pay the per-call connection-pool setup cost.
struct HttpClient(reqwest::Client);

/// Forward a chat message payload from JS to the sidecar via plain HTTP
/// POST instead of the WebView's WebSocket. Background: macOS WKWebView
/// can throttle a view's outbound WebSocket frames after the view goes
/// idle (measured at ~27 s wire_time on M5 turn-2 sends — see ADR 016).
/// Routing the send through the Rust shell over loopback HTTP bypasses
/// that throttling entirely. The streaming response still flows back
/// over the existing WebSocket — the backend looks up the session's WS
/// in `_active_sessions` and dispatches `_handle_message` against it,
/// matching the legacy WS-direct contract.
///
/// `payload` is the same dict the WS path expects (type/content/session_id/
/// provider/model/base_url/t_enter_ms/t_pre_send_ms). The endpoint returns
/// 200 immediately; errors during processing surface as `error` events on
/// the WS, matching the legacy error shape.
#[tauri::command]
async fn send_chat_message(
    payload: serde_json::Value,
    backend: State<'_, BackendUrlHandle>,
    client: State<'_, HttpClient>,
) -> Result<(), String> {
    // Wire-time decomposition diagnostic — capture the moment Rust enters
    // the command (i.e. after the JS `await invoke(...)` has crossed the
    // Tauri IPC boundary). Combined with `t_pre_send_ms` from JS and
    // `t_fastapi_entered_ms` on the FastAPI side, this lets the chat
    // router's `chat_step received` log decompose `wire_time_ms` into:
    //   js_to_rust_ms      = t_rust_received_ms - t_pre_send_ms      (Tauri invoke + IPC)
    //   rust_to_fastapi_ms = t_fastapi_entered_ms - t_rust_received_ms (Rust HTTP + macOS net)
    //   fastapi_to_lock_ms = now - t_fastapi_entered_ms              (FastAPI + lock acquire)
    // Without this, "wire_time" is a single number that hides whether
    // a residual delay is in Tauri's JS bridge, Rust's HTTP client, or
    // backend-side queuing.
    let t_rust_received_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as f64)
        .unwrap_or(0.0);
    let mut payload = payload;
    if let Some(obj) = payload.as_object_mut() {
        obj.insert(
            "t_rust_received_ms".to_string(),
            serde_json::json!(t_rust_received_ms),
        );
    }
    let url = format!("{}/api/chat/message", backend.0);
    let resp = client
        .0
        .post(&url)
        .json(&payload)
        .send()
        .await
        .map_err(|e| format!("send_chat_message HTTP error: {e}"))?;
    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("send_chat_message non-2xx ({status}): {body}"));
    }
    Ok(())
}
