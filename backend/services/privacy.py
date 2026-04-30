"""Central privacy / network kill-switches.

Single source of truth for "is this outbound network call allowed?".

Three layers, in priority order:
1. Environment variable ``JARVIS_OFFLINE_MODE=1`` — hard lock, UI cannot override.
2. Per-workspace preference ``privacy_offline_mode`` — user toggle in Settings.
3. Per-feature preferences (``privacy_web_search_enabled``,
   ``privacy_url_ingest_enabled``).

When offline mode is active, all per-feature toggles are forced off regardless
of their stored value. This guarantees that flipping the master switch
immediately blocks every outbound integration without having to re-toggle each
sub-setting.

Local Ollama, fastembed, the local reranker — none of those touch the public
internet, so this module's gates do not apply to them. Per ADR 015 the v1
stack has no cloud LLM providers at all, so the previous multi-provider gate
(``assert_provider_allowed`` etc.) was deleted.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from config import get_settings
from services import preference_service

_ENV_FLAG = "JARVIS_OFFLINE_MODE"


def _env_force_offline() -> bool:
    return os.environ.get(_ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def _ws(workspace_path: Optional[Path] = None) -> Path:
    return workspace_path or get_settings().workspace_path


def _bool_pref(prefs: dict, key: str, default: bool) -> bool:
    raw = prefs.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def get_privacy_settings(workspace_path: Optional[Path] = None) -> dict:
    """Return current effective privacy settings.

    ``offline_mode_locked`` is True when the env var forces offline mode — the
    UI should disable the toggle in that case.
    """
    prefs = preference_service.load_preferences(workspace_path=_ws(workspace_path))
    env_locked = _env_force_offline()
    user_offline = _bool_pref(prefs, "privacy_offline_mode", False)
    offline = env_locked or user_offline

    # Defaults: web search + url ingest both ON. The kill switches are opt-in
    # protections — flipping `offline_mode` forces both off without having to
    # re-toggle each sub-setting.
    web_search = _bool_pref(prefs, "privacy_web_search_enabled", True)
    url_ingest = _bool_pref(prefs, "privacy_url_ingest_enabled", True)

    if offline:
        web_search = False
        url_ingest = False

    return {
        "offline_mode": offline,
        "offline_mode_locked": env_locked,
        "web_search_enabled": web_search,
        "url_ingest_enabled": url_ingest,
    }


def is_offline_mode(workspace_path: Optional[Path] = None) -> bool:
    return get_privacy_settings(workspace_path)["offline_mode"]


def web_search_enabled(workspace_path: Optional[Path] = None) -> bool:
    return get_privacy_settings(workspace_path)["web_search_enabled"]


def url_ingest_enabled(workspace_path: Optional[Path] = None) -> bool:
    return get_privacy_settings(workspace_path)["url_ingest_enabled"]


_ALLOWED_PRIVACY_KEYS = {
    "offline_mode": "privacy_offline_mode",
    "web_search_enabled": "privacy_web_search_enabled",
    "url_ingest_enabled": "privacy_url_ingest_enabled",
}


def update_privacy_settings(
    updates: dict,
    workspace_path: Optional[Path] = None,
) -> dict:
    """Apply a partial update of privacy preferences.

    Unknown keys are rejected. Returns the new effective settings.
    Raises ``RuntimeError`` if the env lock is engaged and the caller tries
    to disable offline mode.
    """
    ws = _ws(workspace_path)
    env_locked = _env_force_offline()

    for key, value in updates.items():
        if key not in _ALLOWED_PRIVACY_KEYS:
            raise ValueError(f"Unknown privacy setting: {key}")
        if key == "offline_mode" and env_locked and not value:
            raise RuntimeError(
                f"Offline mode is locked by the {_ENV_FLAG} environment variable "
                "and cannot be disabled from the UI."
            )
        pref_key = _ALLOWED_PRIVACY_KEYS[key]
        preference_service.save_preference(
            pref_key,
            "true" if value else "false",
            workspace_path=ws,
        )

    return get_privacy_settings(ws)
