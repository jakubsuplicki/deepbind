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

use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{
    async_runtime, AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder,
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
                    let sidecar = app
                        .shell()
                        .sidecar("jarvis-sidecar")
                        .map_err(|e| format!("sidecar lookup failed: {e}"))?
                        .env("JARVIS_SHELL_PID", shell_pid)
                        .env("JARVIS_OLLAMA_BASE_URL", &ollama_base_url);
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

            // 4. Build the window with the config baked in via init_script,
            // so the page loads with `window.__JARVIS_CONFIG__` already set.
            let cfg_json = serde_json::to_string(&serde_json::json!({
                "backendUrl": cfg.backend_url,
                "wsUrl": cfg.ws_url,
            }))
            .map_err(|e| format!("config serialize failed: {e}"))?;
            let init_script = format!("window.__JARVIS_CONFIG__ = {cfg_json};");

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
