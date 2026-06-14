//! License + trial-state shell-side adapter (ADR 019, chunk 2).
//!
//! Owns the *storage* surfaces the Python sidecar deliberately doesn't:
//!
//! 1. **License file** at `app_data_dir()/license.json` — read on launch
//!    + on every refresh, written when the user pastes a key.
//! 2. **Trial-start timestamp** in the OS keychain (macOS Keychain /
//!    Windows Credential Manager / Linux Secret Service) under service
//!    `com.deepbind.desktop`, key `trial_started_at`. Survives app
//!    reinstall — that's the whole reason for using the keychain over a
//!    plain file (ADR 019 §"Trial state must persist across reinstalls").
//!
//! The state-machine logic itself lives in the Python sidecar
//! (`backend/services/entitlements.py`). This module reads its two
//! storage surfaces, posts them to `POST /api/license/state`, and
//! returns the response to the frontend.
//!
//! ## Synthesized trial-start contract
//!
//! When the keychain has no `trial_started_at` entry (very first launch),
//! we POST `{license_text: ..., trial_started_at: null}` to the backend.
//! The backend treats null as "trial just started" and echoes back the
//! synthesized ISO timestamp it used. We then write that exact string to
//! the keychain — the backend is the single source of truth for ISO
//! formatting (matches the same contract `LicenseClaims.expires_at`
//! enforces). No `chrono` dep needed on the Rust side.

use std::path::PathBuf;

use keyring::Entry;
use serde::Serialize;
use serde_json::Value;
use tauri::{AppHandle, Manager, State};

use crate::{BackendUrlHandle, HttpClient};

/// Service name under which the trial-start timestamp is stored. Stable
/// across reinstalls — that's the contract. Don't change without a
/// migration plan or every existing trial resets to "just started" on
/// the next launch (which is the opposite of the protection we want).
const KEYRING_SERVICE: &str = "com.deepbind.desktop";
const KEYRING_TRIAL_START_KEY: &str = "trial_started_at";

/// Keychain key for the clock-rollback monotonic-state record (ADR 019
/// chunk 6 / ADR 006 §"Clock-tampering defense"). Holds the highest UTC
/// ISO timestamp this install has ever observed. Updated on every
/// refresh to ``max(prev, response.effective_now)``. The OS keychain
/// protection is what makes this tamper-resistant against clock
/// rollback — same mechanism, different key.
const KEYRING_MONOTONIC_KEY: &str = "monotonic_floor";

/// Resolve the platform license file path.
///
/// Tauri's `app_data_dir()` returns the right per-OS location: macOS
/// `~/Library/Application Support/<bundle-id>/`, Windows
/// `%APPDATA%\<bundle-id>\`, Linux `~/.local/share/<bundle-id>/`.
fn license_file_path(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("app_data_dir lookup failed: {e}"))?;
    std::fs::create_dir_all(&dir)
        .map_err(|e| format!("create app_data_dir {}: {}", dir.display(), e))?;
    Ok(dir.join("license.json"))
}

fn read_license_file(app: &AppHandle) -> Result<Option<String>, String> {
    let path = license_file_path(app)?;
    match std::fs::read_to_string(&path) {
        Ok(text) => Ok(Some(text)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(format!("read license file {}: {}", path.display(), e)),
    }
}

fn write_license_file(app: &AppHandle, text: &str) -> Result<(), String> {
    let path = license_file_path(app)?;
    // Atomic-ish write: write to a tmp file in the same dir, then rename.
    // Avoids a partial-license file if the disk fills mid-write or the
    // process is killed between the open and the close.
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, text)
        .map_err(|e| format!("write tmp license {}: {}", tmp.display(), e))?;
    std::fs::rename(&tmp, &path)
        .map_err(|e| format!("rename {} -> {}: {}", tmp.display(), path.display(), e))?;
    Ok(())
}

fn delete_license_file(app: &AppHandle) -> Result<(), String> {
    let path = license_file_path(app)?;
    match std::fs::remove_file(&path) {
        Ok(_) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("delete license file {}: {}", path.display(), e)),
    }
}

fn keyring_entry(key: &str) -> Result<Entry, String> {
    Entry::new(KEYRING_SERVICE, key).map_err(|e| format!("keyring entry init: {e}"))
}

fn keyring_get(key: &str) -> Result<Option<String>, String> {
    let entry = keyring_entry(key)?;
    match entry.get_password() {
        Ok(text) => Ok(Some(text)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(format!("keyring get {key}: {e}")),
    }
}

fn keyring_set(key: &str, value: &str) -> Result<(), String> {
    let entry = keyring_entry(key)?;
    entry
        .set_password(value)
        .map_err(|e| format!("keyring set {key}: {e}"))
}

fn read_trial_start_from_keychain() -> Result<Option<String>, String> {
    keyring_get(KEYRING_TRIAL_START_KEY)
}

fn write_trial_start_to_keychain(value: &str) -> Result<(), String> {
    keyring_set(KEYRING_TRIAL_START_KEY, value)
}

fn read_monotonic_floor_from_keychain() -> Result<Option<String>, String> {
    keyring_get(KEYRING_MONOTONIC_KEY)
}

fn write_monotonic_floor_to_keychain(value: &str) -> Result<(), String> {
    keyring_set(KEYRING_MONOTONIC_KEY, value)
}

/// Body shape for `POST /api/license/state` — all fields nullable.
#[derive(Serialize)]
struct StateRequest<'a> {
    #[serde(skip_serializing_if = "Option::is_none")]
    license_text: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    trial_started_at: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    monotonic_floor: Option<&'a str>,
}

/// Round-trip: read both storage surfaces, push to the sidecar, return
/// the computed state.
///
/// On first-ever launch (keychain empty, no license file) the sidecar
/// echoes back a synthesized `trial_started_at`; we persist it to the
/// keychain so subsequent launches read a real timestamp. This is the
/// only place keychain *writes* happen on the trial path — once written,
/// the value is read-only for the rest of the trial.
async fn refresh_state(
    app: &AppHandle,
    backend_url: &str,
    client: &reqwest::Client,
) -> Result<Value, String> {
    let license_text = read_license_file(app)?;
    let trial_start = read_trial_start_from_keychain()?;
    let monotonic_floor = read_monotonic_floor_from_keychain()?;

    let body = StateRequest {
        license_text: license_text.as_deref(),
        trial_started_at: trial_start.as_deref(),
        monotonic_floor: monotonic_floor.as_deref(),
    };

    let url = format!("{backend_url}/api/license/state");
    let resp = client
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("license/state POST failed: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let detail = resp.text().await.unwrap_or_default();
        return Err(format!("license/state non-2xx ({status}): {detail}"));
    }

    let state: Value = resp
        .json()
        .await
        .map_err(|e| format!("license/state JSON parse: {e}"))?;

    // First-launch persistence: keychain was empty AND the response
    // carries a synthesized trial_started_at. Persist it.
    if trial_start.is_none() {
        if let Some(synthesized) = state.get("trial_started_at").and_then(|v| v.as_str()) {
            if let Err(e) = write_trial_start_to_keychain(synthesized) {
                // Non-fatal — the user gets a fresh trial next launch
                // (keychain might be locked or unavailable). Log and
                // continue rather than failing the boot probe.
                log::warn!("trial-start persist to keychain failed: {e}");
            } else {
                log::info!("trial-start initialised in keychain: {synthesized}");
            }
        }
    }

    // Monotonic-state advance (ADR 019 chunk 6). The backend echoes
    // back its `effective_now` — the clock value it actually used,
    // which is `max(system_now, build_epoch, monotonic_floor)`. We
    // persist this as the new floor (string-comparison is fine for
    // ISO 8601 with `Z` suffix because the format is lexicographically
    // monotonic). Skipped silently if the response shape is missing
    // the field (older sidecar / manual debug).
    if let Some(eff_now) = state.get("effective_now").and_then(|v| v.as_str()) {
        let advance = match monotonic_floor.as_deref() {
            Some(prev) => eff_now > prev,
            None => true,
        };
        if advance {
            if let Err(e) = write_monotonic_floor_to_keychain(eff_now) {
                log::warn!("monotonic-floor persist to keychain failed: {e}");
            }
        }
    }

    Ok(state)
}

/// Frontend-callable: read current entitlement state, refreshing from
/// disk + keychain. Used by the App.vue boot path to decide between
/// trial-banner / wall / settings-panel.
#[tauri::command]
pub async fn license_get_state(
    app: AppHandle,
    backend: State<'_, BackendUrlHandle>,
    client: State<'_, HttpClient>,
) -> Result<Value, String> {
    refresh_state(&app, &backend.0, &client.0).await
}

/// Frontend-callable: install a license-key text block (paste-a-key
/// flow). Validates by POSTing to the backend; if the resulting state
/// is `licensed_active` or `licensed_in_grace`, writes the file to disk
/// and returns the new state. If invalid, leaves disk untouched and
/// returns the invalid-state payload so the UI can show the diagnostic.
#[tauri::command]
pub async fn license_install_text(
    text: String,
    app: AppHandle,
    backend: State<'_, BackendUrlHandle>,
    client: State<'_, HttpClient>,
) -> Result<Value, String> {
    let trimmed = text.trim().to_string();
    if trimmed.is_empty() {
        return Err("License text is empty.".to_string());
    }

    // Validate first — don't write to disk if the backend rejects it.
    // Skip the trial / floor inputs on the validate-only call: the
    // sidecar is recomputing state from the new license_text, not
    // re-evaluating trial state. We refresh the full state below to
    // pick up the canonical post-install state.
    let body = StateRequest {
        license_text: Some(&trimmed),
        trial_started_at: None,
        monotonic_floor: None,
    };
    let url = format!("{}/api/license/state", backend.0);
    let resp = client
        .0
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("license validate POST failed: {e}"))?;
    if !resp.status().is_success() {
        let status = resp.status();
        let detail = resp.text().await.unwrap_or_default();
        return Err(format!("license validate non-2xx ({status}): {detail}"));
    }
    let state: Value = resp
        .json()
        .await
        .map_err(|e| format!("license validate JSON parse: {e}"))?;

    let state_name = state.get("state").and_then(|v| v.as_str()).unwrap_or("");
    let writable = matches!(state_name, "licensed_active" | "licensed_in_grace");

    if writable {
        write_license_file(&app, &trimmed)?;
        log::info!("license installed; state={state_name}");
    } else {
        log::warn!("license install rejected; state={state_name}");
    }

    // Re-read through the standard refresh path so what we return is
    // identical to what `license_get_state` would return next call.
    // (For invalid licenses, refresh sees disk unchanged → trial state.
    // We want the user to see the *invalid* diagnostic, not a misleading
    // "back to trial" message. So return the validation result directly.)
    if writable {
        refresh_state(&app, &backend.0, &client.0).await
    } else {
        Ok(state)
    }
}

/// Frontend-callable: clear any installed license (Settings → "Reset
/// license" button). Returns the recomputed state — typically drops
/// the user back into trial-active or trial-expired depending on
/// keychain history.
#[tauri::command]
pub async fn license_clear(
    app: AppHandle,
    backend: State<'_, BackendUrlHandle>,
    client: State<'_, HttpClient>,
) -> Result<Value, String> {
    delete_license_file(&app)?;
    log::info!("license file cleared");
    refresh_state(&app, &backend.0, &client.0).await
}

/// Frontend-callable: open the user's data folder in Finder/Explorer.
/// Surfaced on the past-grace wall so the customer can reach their
/// Markdown files even when the app is gated. The path is provided by
/// the caller — the Rust side stays dumb about workspace location
/// (which is configurable via JARVIS_WORKSPACE_PATH on the backend).
#[tauri::command]
pub async fn license_open_data_folder(
    path: String,
    app: AppHandle,
) -> Result<(), String> {
    use tauri_plugin_shell::ShellExt;
    app.shell()
        .open(&path, None)
        .map_err(|e| format!("open data folder {path}: {e}"))
}

/// One-shot: read license state during the splash-driven boot sequence.
/// Errors are logged and the function returns `Value::Null` so the frontend
/// boots into a "state unknown" fallback (`useLicenseState` has its own
/// background re-fetch) rather than blocking the splash transition.
pub async fn boot_state(
    app: &AppHandle,
    backend_url: &str,
    client: &reqwest::Client,
) -> Value {
    match refresh_state(app, backend_url, client).await {
        Ok(state) => state,
        Err(e) => {
            log::warn!("boot license state probe failed: {e}");
            Value::Null
        }
    }
}
