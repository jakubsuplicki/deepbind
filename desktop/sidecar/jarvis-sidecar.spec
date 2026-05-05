# PyInstaller spec — real Jarvis backend bundle (ADR 003 §F + §J).
#
# Build with:
#     pyinstaller --noconfirm desktop/sidecar/jarvis-sidecar.spec
#
# Output: desktop/sidecar/dist/jarvis-sidecar (single-file binary, native arch).
#
# This replaces the spike's hello.spec when graduation chunk G2 lands. Where
# hello.spec was a minimal FastAPI hello-world, this one bundles:
#
#     1. backend/scripts/run_frozen.py as the entrypoint (which imports
#        backend/main.py and serves the full FastAPI app).
#     2. The whole `backend/` package — routers + services + models + utils +
#        mcp_server. We use collect_submodules so PyInstaller doesn't miss
#        modules that are imported lazily (inside route handlers, e.g.
#        `from services.embedding_service import reindex_all` inside
#        routers/memory.py).
#     3. Hidden imports for libraries with dynamic plugin loaders that
#        PyInstaller's static analysis doesn't see — fastembed (ONNX runtime
#        model loader), spacy (lang-pack registries), keyring (per-OS
#        backends per ADR 003 §J), uvicorn (loop/protocol registries).
#
# What this spec does NOT do (yet):
#     - Bundle the Ollama CLI. That's G4.
#
# G2b additions (2026-04-29) — fulfilling ADR 003 §A "self-contained from
# minute zero":
#
#     1. fastembed ONNX weights (~240 MB) bundled from
#        backend/_bundled_models/fastembed/ (populated by
#        desktop/scripts/fetch-bundled-models.sh — call it from CI/build
#        before invoking PyInstaller).
#
#     2. spaCy NER model package (xx_ent_wiki_sm, per ADR 018) — installs as
#        a regular Python package via the wheel URL pinned in
#        backend/requirements.txt. Its entrypoint __init__.py registers with
#        spaCy's pkg-resources scanner, so spacy.load("xx_ent_wiki_sm")
#        resolves inside the bundle without any path gymnastics. Picked up
#        below via collect_data_files plus an explicit hidden import
#        alongside spacy.lang.xx (which the wheel does not pull in via
#        static analysis).

# ruff: noqa
# type: ignore

import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

HERE = Path(SPECPATH).resolve()
REPO_ROOT = HERE.parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"

# ADR 015 — single-target local-only build. The v1 product has no cloud
# SDK code in the source repo, so the previous JARVIS_DESKTOP_BUNDLE flag
# (which gated whether to exclude `anthropic`/`openai`/`litellm` from the
# bundle) is no longer needed: there is nothing to exclude. The previous
# `excludes` list survives below for `tkinter`/`matplotlib`/`PIL` (CPU
# bloat, not capability gating).

# --- Hidden imports ---------------------------------------------------------
#
# PyInstaller follows static `import` statements but misses three patterns we
# use heavily:
#   (a) Lazy imports inside function bodies (FastAPI route handlers do this
#       for cold-start friendliness).
#   (b) Plugin-registry imports — fastembed picks an ONNX model class by
#       name string at runtime; spacy loads language packs the same way.
#   (c) keyring's per-OS backend resolution (per ADR 003 §J — keychain on
#       mac, Credential Manager on win, encrypted-file fallback elsewhere).

backend_pkgs = [
    "config",
    "main",
    "models",
    "routers",
    "services",
    "services.chat",
    "services.enrichment",
    "services.graph_service",
    "services.retrieval",
    "utils",
    "mcp_server",
    # Production code imports from tests.eval.latency (chat_model_probe.py:52).
    # Until that's untangled, the eval harness ships in the bundle. Including
    # only the latency subtree — not the whole tests/ — keeps weight down.
    "tests.eval.latency",
]
hidden = []
for pkg in backend_pkgs:
    try:
        hidden.extend(collect_submodules(pkg))
    except Exception:
        # Some submodules (e.g. test-only) may not be importable in a clean
        # build env — collect_submodules raises rather than skipping.
        pass

hidden.extend([
    # spaCy NER model package — installed via requirements.txt direct wheel
    # URL. The package registers itself with spacy's entry-point scanner so
    # spacy.load("xx_ent_wiki_sm") finds it at runtime inside the bundle.
    # Per ADR 018 (v1 English-only), we ship a single multilingual NER model
    # selected for license cleanliness, not for its incidental 9-language
    # coverage. Replaces the previous pl_core_news_sm (GPL-3.0) +
    # en_core_web_sm (MIT-but-OntoNotes-trained) pair — see the
    # commercial-licensing audit for the full reasoning.
    "xx_ent_wiki_sm",
    # uvicorn loop / protocol / lifespan registries
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.uvloop",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # FastAPI / Pydantic v2 transitives
    "fastapi",
    "pydantic",
    "pydantic_settings",
    "pydantic.deprecated.decorator",
    # fastembed (ONNX model loader)
    "fastembed",
    "fastembed.text",
    "onnxruntime",
    "tokenizers",
    # spaCy + the multilingual base language module that xx_ent_wiki_sm uses.
    # The model's wheel registers itself via spaCy's entry-point scanner so
    # spacy.load("xx_ent_wiki_sm") resolves at runtime; PyInstaller doesn't
    # find spacy.lang.xx via static analysis of the model wheel.
    "spacy",
    "spacy.lang.xx",
    # keyring per-OS backends — ADR 003 §J
    "keyring",
    "keyring.backends",
    "keyring.backends.macOS",
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
    "keyring.backends.fail",
    "keyrings.alt",
    "keyrings.alt.file",
    # Misc transitives PyInstaller has historically missed for our deps.
    # Note: `litellm`, `tiktoken`, and `tiktoken_ext` were removed per ADR 015
    # (single-target local-only stack) — those are cloud-SDK transitives.
    # Keeping them here would silently re-bundle anthropic/openai SDKs from a
    # dev's polluted venv even though they're not in requirements.txt.
    "aiosqlite",
])

# --- Datas ------------------------------------------------------------------
#
# fastembed / onnxruntime ship runtime data files (vocab, JSON configs, version
# banners) that PyInstaller's bytecode-only analysis won't pick up.
# `collect_data_files` walks the package on disk and includes them.
#
# spaCy model package (xx_ent_wiki_sm) carries its actual weights as data
# files inside the package directory (e.g. ner/model, vocab/strings.json).
# collect_data_files picks them up.
#
# Removed per ADR 015: `litellm`, `tiktoken`, `tiktoken_ext`. Those are cloud-
# SDK transitives — listing them here would silently re-bundle anthropic /
# openai SDKs from a dev's polluted venv even though they're not in
# requirements.txt. Keep this list narrowly scoped to packages we actually
# import in production code; if a build env happens to have litellm installed
# it must NOT leak into the .app.

datas = []
for pkg in [
    "fastembed",
    "xx_ent_wiki_sm",
]:
    try:
        datas.extend(collect_data_files(pkg))
    except Exception:
        pass

# Bundled fastembed ONNX cache (populated by
# desktop/scripts/fetch-bundled-models.sh per ADR 003 §A). At runtime the
# bundle is unpacked under sys._MEIPASS, and embedding_service.py resolves
# the cache_dir to <_MEIPASS>/_bundled_models/fastembed (see G2b.4).
BUNDLED_MODELS_DIR = REPO_ROOT / "backend" / "_bundled_models"
if (BUNDLED_MODELS_DIR / "fastembed").exists():
    datas.append((str(BUNDLED_MODELS_DIR / "fastembed"), "_bundled_models/fastembed"))
else:
    raise SystemExit(
        "error: backend/_bundled_models/fastembed/ is missing. Run "
        "`bash desktop/scripts/fetch-bundled-models.sh` before building the "
        "sidecar so the installer is offline-capable on first run (ADR 003 §A)."
    )

# --- Analysis ---------------------------------------------------------------

a = Analysis(
    [str(BACKEND_ROOT / "scripts" / "run_frozen.py")],
    # pathex so `from main import app` and `from routers... import ...` resolve
    # both during PyInstaller analysis and at runtime (the bundle re-injects
    # the bundle root onto sys.path).
    pathex=[str(BACKEND_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=sorted(set(hidden)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Things we DON'T need in the sidecar binary. Keeps the bundle honest.
        # NOTE: cannot exclude 'tests' here — production code imports
        # tests.eval.latency from chat_model_probe.py:52. The collect_submodules
        # call above only picks the latency subtree, not the whole tree.
        # NOTE: PIL is excluded by design. fastembed.common.types does
        # `from PIL import Image` at module top even on the text-only path,
        # but we never embed images. backend/utils/pil_stub.py installs a
        # 5-line PIL.Image stub at frozen-entrypoint startup so the import
        # succeeds. Including real Pillow drags in ~30 MB of libjpeg/libtiff/
        # libwebp dylibs which causes amfid to hang `_dyld_start` for 24+ min
        # on first launch of ad-hoc-signed bundles on macOS Tahoe (verified
        # 2026-04-29 during G2b). Notarized builds (G2c) skip that scan path
        # but local dev iteration becomes impossible — keep PIL out.
        "tkinter",
        "matplotlib",
        "PIL",
        "PIL.Image",
        "pandas",
        "scipy.tests",
        "numpy.tests",
        "test",
        "IPython",
        "jupyter",
        # ADR 015 audit gate: cloud LLM SDKs must NEVER ship inside the bundle.
        # No production code imports these (verified by the build-sidecar.sh
        # post-build assertion), so the explicit exclude is belt-and-suspenders:
        # if a future PR accidentally adds `import openai` somewhere, PyInstaller
        # errors at build time instead of silently bundling the SDK from a
        # polluted dev venv. tiktoken / tiktoken_ext are listed because they
        # are litellm/openai transitives, not because we use them — see the
        # comment in services/token_counting.py for why we don't.
        "litellm",
        "anthropic",
        "openai",
        "tiktoken",
        "tiktoken_ext",
        "google.generativeai",
        "google.generativelanguage",
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
    console=True,  # headless — keep the console so stdout (READY line) flows.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,  # signed by Tauri's bundler later.
    entitlements_file=None,
)
