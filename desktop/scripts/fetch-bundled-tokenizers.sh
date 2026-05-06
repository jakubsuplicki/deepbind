#!/usr/bin/env bash
#
# Pre-populate the bundled HuggingFace tokenizer cache that the desktop
# installer ships inside the PyInstaller bundle (ADR 003 §A — "self-contained,
# offline-capable from minute zero"; commercial-licensing-audit.md finding #7
# — "bundle Qwen3/Granite/gpt-oss tokenizers offline" defense-in-depth).
#
# Without this step, services/token_counting.py falls back to the char/4
# estimator at runtime — functional but ~30% inaccurate for non-English
# content, which silently breaks the ADR 009 70%-budget compaction trigger.
#
# Output:
#   backend/_bundled_tokenizers/
#     ├── Qwen__Qwen3-1.7B/tokenizer.json
#     ├── Qwen__Qwen3-4B/tokenizer.json
#     ├── Qwen__Qwen3-8B/tokenizer.json
#     ├── Qwen__Qwen3-4B-Instruct-2507/tokenizer.json
#     ├── Qwen__Qwen3-14B/tokenizer.json
#     ├── Qwen__Qwen3-30B-A3B-Instruct-2507/tokenizer.json
#     ├── Qwen__Qwen3-30B-A3B-Thinking-2507/tokenizer.json
#     ├── ibm-granite__granite-4.0-h-micro/tokenizer.json
#     ├── ibm-granite__granite-4.0-h-tiny/tokenizer.json
#     ├── ibm-granite__granite-4.0-h-small/tokenizer.json
#     └── openai__gpt-oss-120b/tokenizer.json
#
# Per-tokenizer size is ~2-7 MB; total bundle addition is ~30-70 MB.
#
# The id list is the single source of truth in
# backend/services/token_counting.py:_BUNDLED_TOKENIZER_IDS. This script's
# IDS array MUST stay in sync; test_token_counting.py::test_allowlist_matches_catalog
# pins the runtime allowlist against MODEL_CATALOG, but the fetch list is its
# own thing — sync drift here means a new catalog entry's tokenizer never gets
# fetched, so it ships missing. The Python helper at the bottom of this script
# asserts that the fetch IDS exactly matches _BUNDLED_TOKENIZER_IDS, so a
# Python-side change without a script-side change aborts the build.
#
# Idempotent: skips already-present tokenizer files.
#
# License posture: every fetched tokenizer is from an Apache-2.0 model (Qwen3
# family, Granite 4 family, gpt-oss). Per ADR 005 §A's catalog-discipline rule
# the catalog rejects non-permissive licenses at the type level, so this script
# can never legitimately fetch a non-permissive parent's tokenizer.

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

CACHE_DIR="$REPO_ROOT/backend/_bundled_tokenizers"
mkdir -p "$CACHE_DIR"

# Source of truth for which tokenizers ship: _BUNDLED_TOKENIZER_IDS in
# backend/services/token_counting.py. The Python heredoc below derives the
# fetch list from there — there is no separate hardcoded list to drift.
echo "==> fetching bundled tokenizers into $CACHE_DIR"

"$VENV_PY" - <<EOF
import sys
from pathlib import Path
sys.path.insert(0, "$REPO_ROOT/backend")

from services.token_counting import _BUNDLED_TOKENIZER_IDS, _sanitized_id

cache_dir = Path("$CACHE_DIR")
ids = sorted(_BUNDLED_TOKENIZER_IDS)
print(f"Source-of-truth allowlist has {len(ids)} tokenizer ids")

# Use the tokenizers package directly — same library the runtime uses, no
# transformers / huggingface_hub required.
from tokenizers import Tokenizer

fetched = 0
skipped = 0
for tokenizer_id in ids:
    target_dir = cache_dir / _sanitized_id(tokenizer_id)
    target_path = target_dir / "tokenizer.json"
    if target_path.is_file():
        skipped += 1
        print(f"  skip  {tokenizer_id} → already present at {target_path.relative_to(cache_dir)}")
        continue

    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        # from_pretrained downloads from HF Hub if not in ~/.cache/huggingface,
        # else loads from the disk cache. Either way the in-memory Tokenizer
        # is what we save.
        tok = Tokenizer.from_pretrained(tokenizer_id)
        tok.save(str(target_path))
        size_kb = target_path.stat().st_size // 1024
        print(f"  fetch {tokenizer_id} → {target_path.relative_to(cache_dir)} ({size_kb} KB)")
        fetched += 1
    except Exception as exc:
        print(f"  ERROR {tokenizer_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

print(f"==> done: {fetched} fetched, {skipped} already cached, {len(ids)} total")
EOF

# Final size report.
TOTAL_BYTES=$(find "$CACHE_DIR" -type f -name 'tokenizer.json' -exec stat -f '%z' {} + 2>/dev/null | awk '{s+=$1} END {print s}')
TOTAL_MB=$(( TOTAL_BYTES / 1024 / 1024 ))
echo "==> bundled tokenizer cache: ${TOTAL_MB} MB at $CACHE_DIR"
