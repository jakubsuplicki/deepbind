"""MCP server info endpoint — read-only.

The MCP server itself is a standalone CLI launched by the client (Cursor,
Claude Desktop, VS Code, etc.) via stdio. There are two physical entry
points depending on how the backend is running:

- Dev mode (editable install): the `jarvis-mcp` console script registered
  in pyproject.toml — typically symlinked to ~/.local/bin by
  scripts/install-backend.mjs.
- Bundled mode (PyInstaller .app): the same `jarvis-sidecar` binary that
  serves the FastAPI app, invoked with `--mcp`. See run_frozen.py.

This router reports the right command + args for whichever mode is live so
the Settings UI can render an honest, stable client-config snippet.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from config import get_settings
from mcp_server.app import build_app
from mcp_server.middleware.audit import get_stats

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class McpInfoResponse(BaseModel):
    cli_on_path: bool
    cli_path: str | None
    cli_command: str
    cli_args: list[str]
    workspace_path: str
    backend_dir: str
    tool_count: int
    write_tool_count: int
    audit_log_path: str
    calls_today: int
    last_call: str | None
    top_tool: str | None


def _resolve_cli(backend_dir: Path) -> tuple[str, list[str]]:
    """Pick the right (command, args) pair for the current runtime.

    Order:
    1. If `jarvis-mcp` is on PATH, use it. Stable name, dev-friendly.
    2. If running under PyInstaller, return the bundle binary + `--mcp` so
       the snippet is rooted at sys.executable (stable path inside the .app)
       rather than at sys._MEIPASS (an ephemeral extraction dir that
       changes every launch).
    3. Otherwise — dev install without the symlink — fall back to the venv's
       jarvis-mcp script.
    """
    on_path = shutil.which("jarvis-mcp")
    if on_path:
        return "jarvis-mcp", ["--transport", "stdio"]

    if getattr(sys, "frozen", False):
        return sys.executable, ["--mcp", "--transport", "stdio"]

    return f"{backend_dir}/.venv/bin/jarvis-mcp", ["--transport", "stdio"]


@router.get("/info", response_model=McpInfoResponse)
async def mcp_info() -> McpInfoResponse:
    ws = get_settings().workspace_path
    backend_dir = Path(__file__).resolve().parent.parent
    cli_path = shutil.which("jarvis-mcp")
    cli_command, cli_args = _resolve_cli(backend_dir)

    read_app = build_app(ws, allow_writes=False)
    write_app = build_app(ws, allow_writes=True)
    read_count = len(await read_app.list_tools())
    write_count = len(await write_app.list_tools()) - read_count

    stats = get_stats(ws)

    return McpInfoResponse(
        cli_on_path=cli_path is not None,
        cli_path=cli_path,
        cli_command=cli_command,
        cli_args=cli_args,
        workspace_path=str(ws),
        backend_dir=str(backend_dir),
        tool_count=read_count,
        write_tool_count=write_count,
        audit_log_path=str(ws / "app" / "logs" / "mcp.jsonl"),
        calls_today=stats.get("calls_today", 0),
        last_call=stats.get("last_call"),
        top_tool=stats.get("top_tool"),
    )
