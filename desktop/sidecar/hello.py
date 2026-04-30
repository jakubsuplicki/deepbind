"""
Hello-world FastAPI sidecar for the ADR 003 §K notarization spike.

This is intentionally minimal — it is NOT the real Jarvis backend. Its only
job is to validate the Tauri-shell ↔ Python-sidecar architecture end-to-end:

    1. Bind to an ephemeral port (OS-assigned via port=0).
    2. Print exactly one machine-readable READY line on stdout so the shell
       knows where to point the webview:
           JARVIS_BACKEND_READY host=127.0.0.1 port=<n>
    3. Serve GET /api/health → {"status": "ok", "version": "..."}.

When this binary, packaged with PyInstaller into a Tauri sidecar bundle,
notarizes successfully on macOS arm64, the architecture is proven and the
real backend can move into the same shape.

Per ADR 003 §D — env-driven config + stdout port handshake.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

import uvicorn
from fastapi import FastAPI

APP_VERSION = "spike-0.1.0"


def _watch_shell_and_exit() -> None:
    """Background watchdog: terminate when the Tauri shell dies.

    Mitigates ADR 003 §"Negative" §"Child-process zombies on macOS force-quit".
    PyInstaller's onefile bootloader is the immediate child of the Tauri shell;
    the actual Python interpreter (this process) is the bootloader's *child*.
    The naive `getppid()` watchdog catches clean shutdowns, but when the shell
    is SIGKILLed the bootloader is orphaned-but-alive (init becomes its parent,
    PyInstaller's bootloader doesn't self-terminate when reparented), so PPID
    polling never trips.

    Tauri passes the original shell PID via JARVIS_SHELL_PID; we poll its
    liveness with kill(pid, 0). When the shell goes away — Cmd+Q, force-quit,
    debugger detach, OS shutdown — we self-terminate within ~1s.
    """
    shell_pid_raw = os.environ.get("JARVIS_SHELL_PID", "").strip()
    if not shell_pid_raw.isdigit():
        # Direct CLI invocation (no shell parent) — fall back to ppid==1 trip.
        initial_ppid = os.getppid()
        while True:
            time.sleep(1.0)
            if os.getppid() != initial_ppid or os.getppid() == 1:
                os._exit(0)
        return

    shell_pid = int(shell_pid_raw)
    while True:
        time.sleep(1.0)
        try:
            os.kill(shell_pid, 0)  # signal 0: liveness probe, no actual signal sent
        except (ProcessLookupError, PermissionError, OSError):
            os._exit(0)


def _build_app() -> FastAPI:
    app = FastAPI(title="DeepFilesAI Sidecar (spike)", version=APP_VERSION)

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": APP_VERSION,
            "kind": "spike",
            "pid": os.getpid(),
        }

    return app


app = _build_app()


def _resolve_port() -> int:
    """Honour JARVIS_API_PORT; fall back to 0 (OS-assigned)."""
    raw = os.environ.get("JARVIS_API_PORT", "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _resolve_host() -> str:
    return os.environ.get("JARVIS_API_HOST", "127.0.0.1").strip() or "127.0.0.1"


def _bind_socket(host: str, port: int) -> socket.socket:
    """Pre-bind so we know the actual port BEFORE uvicorn starts logging.

    Tauri shell parses our READY line synchronously; if uvicorn's own
    startup banner races ahead, the shell's stdout reader can be
    confused by ANSI noise. We bind with SO_REUSEADDR (per ADR 003
    §"Negative" mitigation for port-binding races on rapid restart),
    print our line, then hand the bound socket to uvicorn.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def main() -> int:
    host = _resolve_host()
    requested = _resolve_port()
    sock = _bind_socket(host, requested)
    bound_host, bound_port = sock.getsockname()

    # The handshake. EXACT format — the Tauri shell parses it with a regex.
    sys.stdout.write(
        f"JARVIS_BACKEND_READY host={bound_host} port={bound_port}\n"
    )
    sys.stdout.flush()

    # Skip the watchdog when running directly from a venv (dev mode):
    # there's no Tauri shell to watch, and the user controls the lifetime
    # via Ctrl+C / dev.sh trap. Detected by the absence of JARVIS_SHELL_PID.
    if os.environ.get("JARVIS_SHELL_PID", "").strip().isdigit():
        threading.Thread(target=_watch_shell_and_exit, daemon=True).start()

    config = uvicorn.Config(
        app,
        log_config=None,  # quiet; the shell only needs the READY line
        access_log=False,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    # uvicorn 0.39+ accepts a list of pre-bound sockets via .serve(sockets=...)
    import asyncio

    try:
        asyncio.run(server.serve(sockets=[sock]))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
