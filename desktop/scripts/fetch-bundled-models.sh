#!/usr/bin/env bash
#
# Pre-populate the fastembed model cache that the desktop installer ships
# inside the PyInstaller bundle (ADR 003 §A — "self-contained, offline-capable
# from minute zero"). Without this step, fastembed downloads ~1 GB on first
# run from HuggingFace, which regresses the offline-first guarantee.
#
# spaCy NER model is NOT fetched here — it installs as a regular Python
# package from `requirements.txt` (xx_ent_wiki_sm-3.8.0) and PyInstaller picks
# it up automatically via collect_submodules. This script only handles
# fastembed's runtime-cached ONNX weights.
#
# Output (per ADR 018, v1 ships English-only):
#   backend/_bundled_models/fastembed/
#     └── models--snowflake--snowflake-arctic-embed-l/
#         ├── files_metadata.json
#         └── snapshots/<hash>/
#             ├── config.json
#             ├── onnx/
#             │   └── model.onnx              (~1 GB FP32 — truncated by HF
#             │                                 dedup pass below)
#             ├── tokenizer.json
#             └── tokenizer_config.json
#
# We dereference HF's symlinks (snapshots/* → blobs/*) and delete blobs/, refs/,
# and .locks/ so PyInstaller bundles the model once instead of doubled.
#
# Idempotent: if the cache already exists with the model dir, we skip the fetch.

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DESKTOP_DIR="$( cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd )"
REPO_ROOT="$( cd -- "$DESKTOP_DIR/.." &> /dev/null && pwd )"

VENV_PY="$REPO_ROOT/backend/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
    echo "error: backend venv not found at $VENV_PY" >&2
    echo "run 'npm run wake-up-jarvis' from the repo root first." >&2
    exit 1
fi

# Source of truth: backend/services/embedding_service.py:_MODEL_NAME.
# Keep these in sync — the spec's runtime _MEIPASS resolution will only find
# weights cached for this exact model_name.
#
# Per ADR 018 (v1 ships English-only), the bundled embedder is
# Snowflake/snowflake-arctic-embed-l — Apache-2.0, 1024-dim, ~1 GB ONNX.
# Top of the MTEB English Retrieval leaderboard among permissively-licensed
# candidates that ship in fastembed's built-in registry. See
# docs/research/models/embedding-english-first.md.
MODEL_NAME="snowflake/snowflake-arctic-embed-l"
CACHE_DIR="$REPO_ROOT/backend/_bundled_models/fastembed"

# fastembed's HF cache stores under "models--<repo>--<name>" with the org/name
# slashes replaced by "--". The arctic-embed-l fastembed registry entry pulls
# directly from the upstream HF org (no qdrant ONNX-Q mirror), so the cache
# dir is:
EXPECTED_DIR="$CACHE_DIR/models--snowflake--snowflake-arctic-embed-l"

if [[ -d "$EXPECTED_DIR/snapshots" ]] && find "$EXPECTED_DIR/snapshots" -name 'model.onnx' -size +500M | grep -q .; then
    echo "==> fastembed weights already cached at $EXPECTED_DIR — skipping fetch"
    exit 0
fi

echo "==> populating fastembed cache for $MODEL_NAME"
echo "    -> $CACHE_DIR"
mkdir -p "$CACHE_DIR"

# Trigger fastembed's own download into our cache_dir. Running embed() forces
# it to actually fetch (instantiation alone is lazy in 0.8.x).
"$VENV_PY" - <<EOF
from fastembed import TextEmbedding
m = TextEmbedding(model_name="$MODEL_NAME", cache_dir="$CACHE_DIR")
list(m.embed(["warmup"]))  # force fetch
print("fastembed: model cached")
EOF

# Dereference snapshots/ symlinks → real files, then drop blobs/, refs/, .locks/.
# This keeps the bundle close to the model's true size (~1.0 GB for arctic-l)
# instead of ~2× that after PyInstaller materializes the symlinked blobs.
echo "==> deduplicating HF cache layout (resolve symlinks, drop blobs/)"
"$VENV_PY" - <<EOF
import os, shutil
from pathlib import Path

cache = Path("$CACHE_DIR")
for model_dir in cache.iterdir():
    if not model_dir.is_dir() or not model_dir.name.startswith("models--"):
        continue
    snapshots = model_dir / "snapshots"
    if snapshots.is_dir():
        for snap in snapshots.iterdir():
            if not snap.is_dir():
                continue
            for entry in snap.iterdir():
                if entry.is_symlink():
                    target = entry.resolve()
                    entry.unlink()
                    shutil.copy2(target, entry)
    # Keep refs/main — HF resolves "main" → snapshot hash via this 40-byte file.
    # Drop blobs/ (snapshots/ now hold real files, no symlinks left to follow)
    # and .locks/ (runtime mutexes; HF recreates as needed).
    for sub in ("blobs", ".locks"):
        p = model_dir / sub
        if p.exists():
            shutil.rmtree(p)

# Drop the top-level .locks/ dir that HF creates next to model dirs.
top_locks = cache / ".locks"
if top_locks.exists():
    shutil.rmtree(top_locks)
EOF

# Final size report.
TOTAL_BYTES=$(find "$CACHE_DIR" -type f -exec stat -f '%z' {} + 2>/dev/null | awk '{s+=$1} END {print s}')
TOTAL_MB=$(( TOTAL_BYTES / 1024 / 1024 ))
echo "==> bundled fastembed cache: ${TOTAL_MB} MB at $CACHE_DIR"
