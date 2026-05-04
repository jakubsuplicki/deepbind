import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import get_settings

logger = logging.getLogger(__name__)

WORKSPACE_DIRS = [
    "app",
    "app/sessions",
    "app/cache",
    "app/logs",
    "app/audio",
    "memory",
    "memory/inbox",
    "memory/daily",
    "memory/projects",
    "memory/people",
    "memory/areas",
    "memory/plans",
    "memory/summaries",
    "memory/knowledge",
    "memory/preferences",
    "memory/examples",
    "memory/attachments",
    "graph",
    "agents",
]


class WorkspaceExistsError(Exception):
    pass


def workspace_exists(workspace_path: Optional[Path] = None) -> bool:
    path = workspace_path or get_settings().workspace_path
    config_file = path / "app" / "config.json"
    return config_file.exists()


def get_workspace_status(workspace_path: Optional[Path] = None) -> dict:
    path = workspace_path or get_settings().workspace_path
    if not workspace_exists(path):
        return {"initialized": False}

    config_file = path / "app" / "config.json"
    try:
        json.loads(config_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read config.json: %s", exc)
        return {"initialized": False}

    return {
        "initialized": True,
        "workspace_path": str(path),
    }


def create_workspace(workspace_path: Optional[Path] = None) -> dict:
    path = workspace_path or get_settings().workspace_path

    if workspace_exists(path):
        raise WorkspaceExistsError(f"Workspace already exists at {path}")

    # Create directory tree
    for d in WORKSPACE_DIRS:
        (path / d).mkdir(parents=True, exist_ok=True)

    try:
        config = {
            "version": "0.1.0",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "workspace_path": str(path),
        }
        config_file = path / "app" / "config.json"
        config_file.write_text(json.dumps(config, indent=2))

        # Seed built-in specialists (e.g. Jira Strategist)
        try:
            from services.specialist_service import seed_builtin_specialists
            seeded = seed_builtin_specialists(path)
            if seeded:
                logger.info("Seeded built-in specialists: %s", seeded)
        except Exception as exc:
            logger.warning("Failed to seed specialists: %s", exc)
    except Exception:
        import shutil
        shutil.rmtree(path, ignore_errors=True)
        raise

    return {"status": "ok", "workspace_path": str(path)}


def get_api_key(workspace_path: Optional[Path] = None) -> Optional[str]:
    """ADR 015 — local-only stack. There is no API key in v1; the bundle
    contains no cloud-provider SDK. The function is preserved as an inert
    `None` so existing chat-router signatures (`api_key: str = ""`) keep
    working without a router-wide refactor; remove when those callsites
    drop the parameter."""
    return None
