# PyInstaller spec — hello-world sidecar for ADR 003 §K notarization spike.
#
# Build with:
#     pyinstaller --noconfirm desktop/sidecar/hello.spec
#
# Output: desktop/sidecar/dist/jarvis-sidecar (single-file binary, macOS arm64).
#
# This spec is intentionally minimal. The real-backend spec will land after
# the notarization gate passes; it will inherit hidden-imports + datas
# discipline from this one (see ADR 003 §J).

# ruff: noqa
# type: ignore

import sys
from pathlib import Path

block_cipher = None

HERE = Path(SPECPATH).resolve()

a = Analysis(
    [str(HERE / "hello.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # FastAPI/Pydantic v2 transitives that PyInstaller's static analysis misses.
        "fastapi",
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Things we DON'T need in the sidecar binary. Keeps size honest.
        "tkinter",
        "matplotlib",
        "PIL",
        "numpy",
        "pandas",
        "test",
        "tests",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="jarvis-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX confuses macOS hardened-runtime + notarization. Never UPX.
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # sidecar is headless — keep the console so stdout flows.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # native build; CI matrix sets per-arch.
    codesign_identity=None,  # signed by Tauri's bundler later.
    entitlements_file=None,
)
