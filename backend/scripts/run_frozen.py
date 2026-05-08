"""Frozen entrypoint for the PyInstaller-bundled backend (ADR 003 §F).

This is the entry script PyInstaller targets when packaging the real
backend into the Tauri sidecar binary. It differs from `python main.py`
in three concrete ways:

    1. Reads JARVIS_API_HOST / JARVIS_API_PORT from env. JARVIS_API_PORT=0
       (the Tauri shell's default) means OS-assigned ephemeral. We pre-bind
       the listening socket so the actual port is known BEFORE uvicorn
       starts logging.
    2. Prints exactly one machine-readable READY line on stdout for the
       Tauri shell to parse:
           JARVIS_BACKEND_READY host=<host> port=<port>
       This is the ONLY line the shell uses for the port handshake; do
       not change its shape (see desktop/src-tauri/src/lib.rs:await_ready).
    3. Disables uvicorn auto-reload (incompatible with PyInstaller).

It also installs the same shell-PID watchdog as the spike's hello.py — but
only when JARVIS_SHELL_PID is set, so direct invocations from a venv
(`python -m backend.scripts.run_frozen`) don't self-terminate when there's
no parent shell to watch.

Per ADR 003 §F (frozen entrypoint) + §"Negative" §"Child-process zombies on
macOS force-quit" (the watchdog) + §D (env-driven config + READY-line port
handshake).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# When PyInstaller bundles a script as the entry point, the package layout
# changes — `from main import app` works because PyInstaller adds the bundle
# root to sys.path. For local dev (`python backend/scripts/run_frozen.py`),
# the parent directory needs to be on sys.path so `import main` resolves.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

# Install the PIL.Image stub before anything else imports fastembed. Real
# Pillow is intentionally excluded from the frozen bundle — see
# backend/utils/pil_stub.py for why (amfid hang on macOS Tahoe).
from utils import pil_stub  # noqa: E402
pil_stub.install()

import uvicorn  # noqa: E402

from main import app  # noqa: E402  (FastAPI app from backend/main.py)


def _watch_shell_and_exit() -> None:
    """Background watchdog: terminate when the Tauri shell dies.

    See desktop/sidecar/hello.py for the rationale — same code, lifted into
    the real backend so it survives graduation.
    """
    shell_pid_raw = os.environ.get("JARVIS_SHELL_PID", "").strip()
    if not shell_pid_raw.isdigit():
        return
    shell_pid = int(shell_pid_raw)
    while True:
        time.sleep(1.0)
        try:
            os.kill(shell_pid, 0)  # signal 0 — liveness probe, no signal sent
        except (ProcessLookupError, PermissionError, OSError):
            os._exit(0)


def _setup_logging() -> Path:
    """Wire a rotating FileHandler so the bundled sidecar's logs survive.

    The Tauri shell drains the sidecar's stdout/stderr only until the READY
    handshake, then drops the receiver — anything `logger.info(...)` would
    print after READY goes into a closed channel. We set up our own
    persistent log file under the standard macOS log location so an
    operator (or this codebase's `_prefill_log` diagnostic) can surface
    its output without rebuilding the bundle.

    Returns the resolved log file path. Idempotent: callers can re-invoke
    safely on hot reload.
    """
    if sys.platform == "darwin":
        log_dir = Path.home() / "Library" / "Logs" / "DeepFilesAI"
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        log_dir = (Path(local) if local else Path.home() / "AppData" / "Local") / "DeepFilesAI" / "Logs"
    else:
        xdg = os.environ.get("XDG_STATE_HOME")
        log_dir = (Path(xdg) if xdg else Path.home() / ".local" / "state") / "DeepFilesAI" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "sidecar.log"

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    # Drop any existing FileHandlers for this exact path so re-init doesn't
    # accumulate duplicates on dev hot reload.
    for h in list(root.handlers):
        if isinstance(h, RotatingFileHandler) and Path(getattr(h, "baseFilename", "")) == log_path:
            root.removeHandler(h)
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    return log_path


def _resolve_host() -> str:
    return os.environ.get("JARVIS_API_HOST", "127.0.0.1").strip() or "127.0.0.1"


def _resolve_port() -> int:
    raw = os.environ.get("JARVIS_API_PORT", "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _bind_socket(host: str, port: int) -> socket.socket:
    """Pre-bind so the OS-assigned port is knowable before uvicorn logs.

    SO_REUSEADDR per ADR 003 §"Negative" §"Port-binding races on rapid
    restart" — TIME_WAIT can otherwise block re-bind for ~60s after a
    crash + immediate restart.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def _dispatch_mcp_mode_if_requested() -> None:
    """When invoked as `<bundle-binary> --mcp …`, run as the stdio MCP server.

    The same frozen binary serves two roles: by default it boots the FastAPI
    sidecar; with `--mcp` in argv it dispatches to mcp_server.__main__ instead.
    This lets external MCP clients (Claude Desktop, Cursor, etc.) spawn the
    bundled binary directly — there is no separate `jarvis-mcp` executable in
    the .app, and the dependency tree (services, fastembed, spaCy, …) is
    already paid for, so a second mode costs nothing.

    Returns only if `--mcp` is absent. Otherwise calls SystemExit via the MCP
    entry point's normal completion path.
    """
    if "--mcp" not in sys.argv[1:]:
        return
    sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--mcp"]
    from mcp_server.__main__ import main as mcp_main
    mcp_main()
    raise SystemExit(0)


def main() -> int:
    _dispatch_mcp_mode_if_requested()

    log_path = _setup_logging()
    logging.getLogger("jarvis-sidecar").info(
        "sidecar starting; logs at %s", log_path
    )

    host = _resolve_host()
    requested_port = _resolve_port()
    sock = _bind_socket(host, requested_port)
    bound_host, bound_port = sock.getsockname()

    # The handshake. EXACT format — see lib.rs:await_ready parser.
    sys.stdout.write(
        f"JARVIS_BACKEND_READY host={bound_host} port={bound_port}\n"
    )
    sys.stdout.flush()

    if os.environ.get("JARVIS_SHELL_PID", "").strip().isdigit():
        threading.Thread(target=_watch_shell_and_exit, daemon=True).start()

    config = uvicorn.Config(
        app,
        log_config=None,
        access_log=False,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    try:
        asyncio.run(server.serve(sockets=[sock]))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
