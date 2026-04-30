from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode


class Settings(BaseSettings):
    # User's Markdown vault. Source of truth for all knowledge state.
    workspace_path: Path = Path.home() / "Jarvis"

    # App-data dir (SQLite, license, app config). Distinct from the vault so
    # uninstall can wipe app-data without touching user content. The Tauri
    # shell sets this to platform-appropriate Application Support path
    # (ADR 003 §"Model storage is overridden, not defaulted").
    app_data_path: Path = Path.home() / "Jarvis" / ".app"

    api_host: str = "127.0.0.1"
    # 0 = OS-assigned ephemeral port. The Tauri shell sets JARVIS_API_PORT=0
    # at spawn time and reads the actual port back via the READY line on the
    # sidecar's stdout (ADR 003 §D). Dev/test invocations keep the legacy
    # 8000 default.
    api_port: int = 8000

    # Origins allowed to call the backend. Default covers Nuxt dev server.
    # The Tauri shell appends platform origins (`tauri://localhost` mac,
    # `https://tauri.localhost` win) via the JARVIS_CORS_ORIGINS env var
    # at spawn time (ADR 003 §E).
    #
    # NoDecode tells pydantic-settings to skip JSON-parsing this env var so
    # our validator below can accept comma-separated values:
    #     JARVIS_CORS_ORIGINS=tauri://localhost,https://tauri.localhost
    # JSON-list form (`["tauri://localhost"]`) still works because the
    # validator passes through anything that's already a list.
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    # Graph edge type kill switches
    similarity_edges_enabled: bool = True
    temporal_edges_enabled: bool = True

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_csv(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                # Tolerate JSON-list form too (legacy + tests).
                import json
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return [str(s).strip() for s in parsed if str(s).strip()]
                except json.JSONDecodeError:
                    pass
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    model_config = {"env_prefix": "JARVIS_", "env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
