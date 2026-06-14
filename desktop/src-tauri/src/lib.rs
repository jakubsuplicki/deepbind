//! DeepBind desktop shell — ADR 003 graduation (G4a: bundled Ollama).
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

use serde::{Deserialize, Serialize};
use tauri::{
    async_runtime, AppHandle, Emitter, Manager, RunEvent, State, WebviewUrl,
    WebviewWindowBuilder,
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

#[derive(Clone, Debug, Deserialize, Serialize)]
struct SourceImportGrantResponse {
    source_token: String,
    source_kind: String,
    display_name: String,
    root_path: String,
    expires_at: String,
}

const SOURCE_IMPORT_SAMPLE_DATASET: &str = "sample-data/deepfiles-demo-folder";

// ---------------------------------------------------------------------------
// Boot-stage tracking — drives the splash screen.
// ---------------------------------------------------------------------------
//
// The Tauri shell builds the window IMMEDIATELY, before any of the slow boot
// work (Ollama spawn → bind, sidecar spawn → READY, license probe). The
// splash component subscribes to `boot:stage` events and mirrors the real
// boot progress; on `boot:complete` it injects `__JARVIS_CONFIG__` +
// `__JARVIS_LICENSE_STATE__` globals and crossfades to the real layout.
//
// The shell maintains a snapshot of the most-recent stage in `BootStateHandle`
// so a splash that mounts AFTER an early stage already fired (Vue's hydration
// vs the Rust task is a race) can call `get_boot_state` and resume from the
// right point. Without that, an unlucky splash mount could miss the
// `ollama_starting` event and sit on the default-pending state for several
// seconds.

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
enum BootPhase {
    OllamaStarting,
    OllamaReady,
    SidecarStarting,
    SidecarReady,
    LicenseProbing,
    Ready,
    Error,
}

#[derive(Clone, Debug, Serialize)]
struct BootStage {
    phase: BootPhase,
    /// Human-readable detail line. Plain English, not a code path.
    detail: String,
    /// 0.0..1.0 monotone — splash uses this to paint the trace.
    progress: f32,
    /// Populated only on `Phase::Ready` and `Phase::Error`. The frontend
    /// reads `config` + `license` here on the boot:complete event.
    #[serde(skip_serializing_if = "Option::is_none")]
    config: Option<BackendConfig>,
    #[serde(skip_serializing_if = "Option::is_none")]
    license: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

impl BootStage {
    fn new(phase: BootPhase, detail: &str, progress: f32) -> Self {
        Self {
            phase,
            detail: detail.to_string(),
            progress,
            config: None,
            license: None,
            error: None,
        }
    }

    fn ready(config: BackendConfig, license: serde_json::Value) -> Self {
        Self {
            phase: BootPhase::Ready,
            detail: "ready".to_string(),
            progress: 1.0,
            config: Some(config),
            license: Some(license),
            error: None,
        }
    }

    fn error(detail: &str, message: String) -> Self {
        Self {
            phase: BootPhase::Error,
            detail: detail.to_string(),
            progress: 0.0,
            config: None,
            license: None,
            error: Some(message),
        }
    }
}

struct BootStateHandle(Mutex<BootStage>);

fn emit_boot(app: &AppHandle, stage: BootStage) {
    if let Some(handle) = app.try_state::<BootStateHandle>() {
        if let Ok(mut guard) = handle.0.lock() {
            *guard = stage.clone();
        }
    }
    if let Err(e) = app.emit("boot:stage", &stage) {
        log::warn!("boot:stage emit failed: {e}");
    }
}

#[tauri::command]
fn get_boot_state(state: State<'_, BootStateHandle>) -> BootStage {
    state.0.lock().map(|g| g.clone()).unwrap_or_else(|_| {
        BootStage::error("internal lock poisoned", "boot state unreadable".into())
    })
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

fn generate_source_import_token() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes)
        .map_err(|e| format!("source-import token generation failed: {e}"))?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

#[cfg(target_os = "macos")]
fn pick_folder_blocking() -> Result<String, String> {
    let output = Command::new("osascript")
        .arg("-e")
        .arg("POSIX path of (choose folder with prompt \"Choose a folder to scan\")")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| format!("folder picker failed to start: {e}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr.contains("User canceled") || stderr.contains("-128") {
            return Err("Folder selection cancelled".to_string());
        }
        return Err(format!("folder picker failed: {}", stderr.trim()));
    }
    let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if path.is_empty() {
        return Err("Folder selection returned no path".to_string());
    }
    Ok(path)
}

#[cfg(not(target_os = "macos"))]
fn pick_folder_blocking() -> Result<String, String> {
    Err("Native folder picker is not available on this platform yet".to_string())
}

#[cfg(target_os = "macos")]
fn pick_archive_blocking() -> Result<String, String> {
    let output = Command::new("osascript")
        .arg("-e")
        .arg(concat!(
            "POSIX path of (choose file with prompt ",
            "\"Choose a ZIP archive to scan\" of type {\"zip\"})",
        ))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| format!("archive picker failed to start: {e}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr.contains("User canceled") || stderr.contains("-128") {
            return Err("Archive selection cancelled".to_string());
        }
        return Err(format!("archive picker failed: {}", stderr.trim()));
    }
    let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if path.is_empty() {
        return Err("Archive selection returned no path".to_string());
    }
    Ok(path)
}

#[cfg(not(target_os = "macos"))]
fn pick_archive_blocking() -> Result<String, String> {
    Err("Native archive picker is not available on this platform yet".to_string())
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

/// Boot orchestration — runs in an async task spawned from `setup`. The
/// window is already painting the splash by the time this starts; every
/// stage transition is mirrored to the splash via `boot:stage` events.
///
/// Errors are *non-fatal*: instead of bubbling up (which would kill the
/// already-built window), we emit a `Phase::Error` stage and let the splash
/// surface a "boot failed" state with a retry / quit affordance. This is the
/// honest UX — silently exiting on a bundle integrity issue would leave the
/// user staring at a vanished window with nothing to act on.
async fn run_boot_sequence(app: AppHandle) {
    // Dev-mode short-circuit. JARVIS_DEV_BACKEND_URL trusts the developer's
    // already-running Ollama + manually-launched backend; no ollama spawn,
    // no sidecar spawn, just license probe and emit Ready. See
    // desktop/scripts/dev.sh.
    if let Ok(url) = std::env::var("JARVIS_DEV_BACKEND_URL") {
        if !url.is_empty() {
            log::info!("dev mode: JARVIS_DEV_BACKEND_URL={url}");
            let ws_url = url
                .replacen("https://", "wss://", 1)
                .replacen("http://", "ws://", 1);
            let cfg = BackendConfig { backend_url: url, ws_url };
            // Manage the shared HTTP client so license probe + chat sends work.
            app.manage(BackendUrlHandle(cfg.backend_url.clone()));
            match reqwest::Client::builder().pool_max_idle_per_host(0).build() {
                Ok(client) => {
                    app.manage(HttpClient(client));
                }
                Err(e) => {
                    emit_boot(&app, BootStage::error("http client build failed", e.to_string()));
                    return;
                }
            }
            emit_boot(&app, BootStage::new(BootPhase::LicenseProbing, "verifying entitlement", 0.85));
            let url_handle = app.state::<BackendUrlHandle>();
            let http_handle = app.state::<HttpClient>();
            let license = license::boot_state(&app, &url_handle.0, &http_handle.0).await;
            emit_boot(&app, BootStage::ready(cfg, license));
            return;
        }
    }

    // -----------------------------------------------------------------
    // Stage 1: Ollama
    // -----------------------------------------------------------------
    emit_boot(&app, BootStage::new(BootPhase::OllamaStarting, "spinning up local inference runtime", 0.10));
    let ollama_child = match spawn_ollama(&app) {
        Ok(c) => c,
        Err(e) => {
            emit_boot(&app, BootStage::error("ollama spawn failed", e));
            return;
        }
    };
    // Manage immediately so the quit handler can find + kill it even if
    // we abort the boot sequence below.
    app.manage(OllamaHandle(Mutex::new(Some(ollama_child))));

    let ollama_addr: SocketAddr = format!("{OLLAMA_HOST}:{OLLAMA_PORT}").parse().unwrap();
    if let Err(e) = async_runtime::spawn_blocking(move || {
        await_ollama_ready(ollama_addr, OLLAMA_READY_DEADLINE)
    })
    .await
    .unwrap_or_else(|e| Err(format!("ollama-ready task panicked: {e}")))
    {
        emit_boot(&app, BootStage::error("ollama did not bind", e));
        return;
    }
    let ollama_base_url = format!("http://{ollama_addr}");
    log::info!("ollama ready at {ollama_base_url}");
    emit_boot(&app, BootStage::new(BootPhase::OllamaReady, "inference runtime online", 0.30));

    // -----------------------------------------------------------------
    // Stage 2: Sidecar (PyInstaller unpack + Python startup is the bulk
    // of cold-launch wall-clock — typically 8-25 s on a fresh install)
    // -----------------------------------------------------------------
    emit_boot(&app, BootStage::new(BootPhase::SidecarStarting, "extracting inference services", 0.40));
    let shell_pid = std::process::id().to_string();
    let source_import_token = app
        .state::<SourceImportGrantToken>()
        .0
        .clone();
    let sidecar_cmd = match app.shell().sidecar("jarvis-sidecar") {
        Ok(c) => c,
        Err(e) => {
            emit_boot(&app, BootStage::error("sidecar lookup failed", e.to_string()));
            return;
        }
    };
    let sidecar_cmd = sidecar_cmd
        .env("JARVIS_SHELL_PID", shell_pid)
        .env("JARVIS_OLLAMA_BASE_URL", &ollama_base_url)
        .env("JARVIS_SOURCE_IMPORT_GRANT_TOKEN", &source_import_token)
        // CORS: bundled webview origin is platform-specific
        // (`tauri://localhost` on macOS, `https://tauri.localhost`
        // on Windows). Pass both so the same binary is portable.
        .env("JARVIS_CORS_ORIGINS", "tauri://localhost,https://tauri.localhost");

    let (rx, child) = match sidecar_cmd.spawn() {
        Ok(p) => p,
        Err(e) => {
            emit_boot(&app, BootStage::error("sidecar spawn failed", e.to_string()));
            return;
        }
    };
    app.manage(SidecarHandle(Mutex::new(Some(child))));

    let cfg = match await_sidecar_ready(rx, SIDECAR_READY_DEADLINE).await {
        Ok(c) => c,
        Err(e) => {
            emit_boot(&app, BootStage::error("backend handshake failed", e));
            return;
        }
    };
    log::info!("sidecar ready: backend_url={}, ws_url={}", cfg.backend_url, cfg.ws_url);
    emit_boot(&app, BootStage::new(BootPhase::SidecarReady, "services online", 0.75));

    // -----------------------------------------------------------------
    // Stage 3: shared HTTP client + license probe
    // -----------------------------------------------------------------
    // Long-lived reqwest client — see comments in send_chat_message for the
    // pool_max_idle_per_host(0) reasoning.
    app.manage(BackendUrlHandle(cfg.backend_url.clone()));
    match reqwest::Client::builder().pool_max_idle_per_host(0).build() {
        Ok(client) => {
            app.manage(HttpClient(client));
        }
        Err(e) => {
            emit_boot(&app, BootStage::error("http client build failed", e.to_string()));
            return;
        }
    }

    emit_boot(&app, BootStage::new(BootPhase::LicenseProbing, "verifying entitlement", 0.90));
    let license = {
        let url_handle = app.state::<BackendUrlHandle>();
        let http_handle = app.state::<HttpClient>();
        license::boot_state(&app, &url_handle.0, &http_handle.0).await
    };

    // -----------------------------------------------------------------
    // Stage 4: ready → splash transitions out, real layout mounts
    // -----------------------------------------------------------------
    emit_boot(&app, BootStage::ready(cfg, license));
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            get_boot_state,
            source_import_pick_folder,
            source_import_pick_archive,
            source_import_pick_sample_dataset,
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

            // Seed the boot-state handle with the pending stage so a splash
            // that calls `get_boot_state` before the first event fires sees
            // a coherent value (not "ready" by default).
            app.manage(BootStateHandle(Mutex::new(BootStage::new(
                BootPhase::OllamaStarting,
                "initializing",
                0.0,
            ))));
            let source_import_token = std::env::var("JARVIS_SOURCE_IMPORT_GRANT_TOKEN")
                .ok()
                .filter(|value| !value.is_empty())
                .map(Ok)
                .unwrap_or_else(generate_source_import_token)?;
            app.manage(SourceImportGrantToken(source_import_token));

            // Build the window IMMEDIATELY. The page loads the splash route;
            // `useBoot` subscribes to `boot:stage` events and crossfades into
            // the real layout once `Phase::Ready` arrives. Total time-to-paint
            // is ~200 ms vs. the 10-30 s blank dock-bounce of the prior shape.
            //
            // No `initialization_script` for `__JARVIS_CONFIG__` /
            // `__JARVIS_LICENSE_STATE__` — those values aren't known yet. The
            // splash component writes them onto `window` when `boot:complete`
            // fires, before triggering the layout mount, so consumers
            // (`useLicenseState`, `useApi`, …) see them populated on their
            // first read. ADR 019's first-paint contract holds: the splash
            // is non-content, no gated surface paints until license state is
            // known.
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("DeepBind")
                .inner_size(960.0, 720.0)
                .resizable(true)
                .build()
                .map_err(|e| format!("window build failed: {e}"))?;

            // Spawn the boot sequence. Window is already up; this drives the
            // splash through its real stages. Errors land as `Phase::Error`
            // events rather than aborting, so the user sees a tangible failure
            // state instead of a silently-vanished window.
            let app_handle = app.handle().clone();
            async_runtime::spawn(async move {
                run_boot_sequence(app_handle).await;
            });

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

/// Shared secret used only between the Tauri shell and the local sidecar to
/// turn a native picker result into a short-lived backend source grant. The
/// frontend receives the resulting grant token, not a raw path-scanning API.
struct SourceImportGrantToken(String);

#[tauri::command]
async fn source_import_pick_folder(
    backend: State<'_, BackendUrlHandle>,
    client: State<'_, HttpClient>,
    grant_token: State<'_, SourceImportGrantToken>,
) -> Result<SourceImportGrantResponse, String> {
    let path = async_runtime::spawn_blocking(pick_folder_blocking)
        .await
        .map_err(|e| format!("folder picker task failed: {e}"))??;

    create_source_import_grant_for_path(
        path,
        "local_folder",
        backend.inner(),
        client.inner(),
        grant_token.inner(),
    )
    .await
}

#[tauri::command]
async fn source_import_pick_archive(
    backend: State<'_, BackendUrlHandle>,
    client: State<'_, HttpClient>,
    grant_token: State<'_, SourceImportGrantToken>,
) -> Result<SourceImportGrantResponse, String> {
    let path = async_runtime::spawn_blocking(pick_archive_blocking)
        .await
        .map_err(|e| format!("archive picker task failed: {e}"))??;

    create_source_import_grant_for_path(
        path,
        "local_archive",
        backend.inner(),
        client.inner(),
        grant_token.inner(),
    )
    .await
}

fn sample_dataset_path(app: &AppHandle) -> Result<PathBuf, String> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(resource_dir) = app.path().resource_dir() {
        candidates.push(resource_dir.join(SOURCE_IMPORT_SAMPLE_DATASET));
    }
    candidates.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(SOURCE_IMPORT_SAMPLE_DATASET));

    for candidate in candidates {
        if candidate.is_dir() {
            return Ok(candidate);
        }
    }

    Err(format!(
        "sample dataset not found at resource or dev path: {SOURCE_IMPORT_SAMPLE_DATASET}"
    ))
}

#[tauri::command]
async fn source_import_pick_sample_dataset(
    app: AppHandle,
    backend: State<'_, BackendUrlHandle>,
    client: State<'_, HttpClient>,
    grant_token: State<'_, SourceImportGrantToken>,
) -> Result<SourceImportGrantResponse, String> {
    let path = sample_dataset_path(&app)?;
    create_source_import_grant_for_path(
        path.to_string_lossy().to_string(),
        "local_folder",
        backend.inner(),
        client.inner(),
        grant_token.inner(),
    )
    .await
}

async fn create_source_import_grant_for_path(
    path: String,
    source_kind: &str,
    backend: &BackendUrlHandle,
    client: &HttpClient,
    grant_token: &SourceImportGrantToken,
) -> Result<SourceImportGrantResponse, String> {
    let url = format!("{}/api/source-import/grants", backend.0);
    let resp = client
        .0
        .post(&url)
        .header("x-deepfiles-shell-token", grant_token.0.as_str())
        .json(&serde_json::json!({
            "path": path,
            "source_kind": source_kind,
        }))
        .send()
        .await
        .map_err(|e| format!("source grant HTTP error: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("source grant rejected ({status}): {body}"));
    }

    resp.json::<SourceImportGrantResponse>()
        .await
        .map_err(|e| format!("source grant response decode failed: {e}"))
}

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
