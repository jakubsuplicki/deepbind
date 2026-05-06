"""Token counting for ADR 009 production-side compaction.

Why this exists:

ADR 009 §"Token-aware budget tracking" makes the compaction budget a
**token** number, not a character number. Character approximation is off
by 20–40% on Polish/Chinese/Japanese/Arabic — and even on English the
drift across long conversations breaks the 70%-budget compaction trigger.
A real Qwen3 tokenizer for the active model gives accurate counts.

How tokenizers are loaded (offline-only, post-2026-05-06):

Tokenizers are loaded **strictly from the bundled offline cache** at
``backend/_bundled_tokenizers/<sanitized_id>/tokenizer.json``. The runtime
never calls ``tokenizers.Tokenizer.from_pretrained(id)`` — that path
would hit huggingface.co at first use, which contradicts ADR 002's
offline-first stance, leaks per-tokenizer license terms onto our build
host, and would silently fail in air-gapped customer environments. The
bundle is populated at build time by
``desktop/scripts/fetch-bundled-tokenizers.sh``.

A positive allowlist (``_BUNDLED_TOKENIZER_IDS``) gates which ids are
ever attempted — even if a stray tokenizer.json file landed inside the
bundle, an off-allowlist id resolves to None and falls through to the
char/4 estimator. The allowlist is the source of truth for "what
tokenizers does v1 ship?" and must match the catalog's set of
``tokenizer_id`` values; ``test_token_counting.py`` enforces that
invariant. See commercial-licensing-audit.md finding #7 for the
defense-in-depth motivation.

What this module does:

- Lazily loads HuggingFace tokenizers (the `tokenizers` package, not the
  full `transformers` stack) by ``tokenizer_id`` and caches the
  ``Tokenizer`` instance per process. First call hits the bundled
  ``tokenizer.json`` on disk; subsequent calls are ~microseconds per
  message.
- Counts tokens for a single string and for an Anthropic-style messages
  list (handling string content, tool-use blocks, tool-result blocks).
- Falls back to a deterministic ``chars // 4`` approximation when the
  tokenizer can't be loaded — id not in the allowlist, bundled file
  missing (dev environment without ``fetch-bundled-tokenizers.sh``
  having been run), or the tokenizers package itself is missing. The
  fallback is imprecise but keeps the system functional.

Why not transformers / tiktoken:

- ``transformers`` pulls torch + huggingface_hub and is several hundred MB.
  ``tokenizers`` is a single Rust-backed Python package, ~2 MB on disk.
- ``tiktoken`` is for OpenAI tokenizers only (cl100k / o200k). Catalog
  models are Qwen / Granite / gpt-oss; tiktoken would not match any of
  them, so its counts would still be approximations.
- For Anthropic chat the local catalog isn't relevant — the chat router
  short-circuits compaction when no local model entry is in play, and the
  Anthropic SDK manages its own context window server-side.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

# Process-wide cache of loaded Tokenizer instances. Keyed by tokenizer_id.
# The HF tokenizers library is thread-safe for encode() once constructed,
# so reads of the cache after population don't need the lock.
_TOKENIZER_CACHE: Dict[str, Any] = {}
# Failed loads are sticky — we don't re-attempt every turn after a failure
# (id not in allowlist, bundled file missing). The value is a short
# reason string for debugging; absence means "never attempted."
_TOKENIZER_FAILURES: Dict[str, str] = {}
_CACHE_LOCK = threading.Lock()

# Char-per-token ratio for the fallback estimator. 4.0 matches the
# pre-ADR-009 estimate used in build_system_prompt_with_stats. Bumping
# this requires re-baselining ADR 011 latency budgets.
_FALLBACK_CHARS_PER_TOKEN = 4.0

# ── Bundled-tokenizer allowlist + path resolution ────────────────────
#
# The catalog ships tokenizer files under
# ``backend/_bundled_tokenizers/<sanitized_id>/tokenizer.json``.
# Sanitization rule: replace '/' with '__' so the HF org/name pair maps
# to a single directory level (e.g. ``Qwen/Qwen3-8B`` →
# ``Qwen__Qwen3-8B``).
#
# This allowlist is the single source of truth for "which tokenizers
# does v1 ship?" — it must equal the set of ``tokenizer_id`` values
# present in services.ollama_service.MODEL_CATALOG. Drift is a
# regression: a new catalog entry without a matching bundled tokenizer
# silently downgrades to char/4 in production. The
# ``test_allowlist_matches_catalog`` test pins the invariant.
_BUNDLED_TOKENIZER_IDS: frozenset[str] = frozenset({
    # Qwen3 family (Apache-2.0)
    "Qwen/Qwen3-1.7B",
    "Qwen/Qwen3-4B",
    "Qwen/Qwen3-8B",
    "Qwen/Qwen3-4B-Instruct-2507",
    "Qwen/Qwen3-14B",
    "Qwen/Qwen3-30B-A3B-Instruct-2507",
    "Qwen/Qwen3-30B-A3B-Thinking-2507",
    # IBM Granite 4 family (Apache-2.0)
    "ibm-granite/granite-4.0-h-micro",
    "ibm-granite/granite-4.0-h-tiny",
    "ibm-granite/granite-4.0-h-small",
    # OpenAI gpt-oss (Apache-2.0)
    "openai/gpt-oss-120b",
})


def _sanitized_id(tokenizer_id: str) -> str:
    """Map a HF tokenizer id to its bundled-cache directory name.

    ``Qwen/Qwen3-8B`` → ``Qwen__Qwen3-8B``. The single-level layout
    avoids needing per-org directories and matches the simple-string
    allowlist above.
    """
    return tokenizer_id.replace("/", "__")


def _bundled_tokenizers_root() -> Path:
    """Resolve the on-disk root of the bundled tokenizer cache.

    PyInstaller's onefile bundle unpacks data files under
    ``sys._MEIPASS``; ``desktop/sidecar/jarvis-sidecar.spec`` ships the
    cache as ``_bundled_tokenizers/...``. When not frozen (dev / pytest),
    use the in-repo path so dev runs hit the same on-disk layout the
    bundled build will see at runtime.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "_bundled_tokenizers"
    # Dev path — relative to this file: backend/services/token_counting.py
    return Path(__file__).resolve().parent.parent / "_bundled_tokenizers"


def _bundled_tokenizer_path(tokenizer_id: str) -> Optional[Path]:
    """Path to the bundled ``tokenizer.json`` for ``tokenizer_id``, or None.

    Returns None when ``tokenizer_id`` is not in the allowlist (caller
    should fall back to char/4 cleanly) or when the bundled file is
    missing on disk (dev environment without
    ``fetch-bundled-tokenizers.sh`` having been run).
    """
    if tokenizer_id not in _BUNDLED_TOKENIZER_IDS:
        return None
    candidate = _bundled_tokenizers_root() / _sanitized_id(tokenizer_id) / "tokenizer.json"
    return candidate if candidate.is_file() else None


def _load_tokenizer(tokenizer_id: str):
    """Load a HuggingFace tokenizer by id from the bundled offline cache.

    Returns the loaded ``Tokenizer`` or None on any failure. Failures are
    silent (caller falls back to char/4) and sticky in the cache so we
    don't re-stat the missing file on every turn. Network access is
    structurally impossible — we only ever call ``Tokenizer.from_file``
    against an in-bundle path.

    Failure modes that all return None:
      - ``tokenizer_id`` not in ``_BUNDLED_TOKENIZER_IDS`` allowlist
      - Bundled ``tokenizer.json`` missing on disk
      - ``tokenizers`` package not installed (stripped install)
      - HF tokenizer file is corrupt / unparseable
    """
    path = _bundled_tokenizer_path(tokenizer_id)
    if path is None:
        return None
    try:
        from tokenizers import Tokenizer  # local import — keep import surface optional
    except ImportError:
        logger.info("tokenizers package not installed; falling back to char/4 estimator")
        return None
    try:
        return Tokenizer.from_file(str(path))
    except Exception as exc:  # noqa: BLE001 — HF raises many distinct types
        logger.warning(
            "Failed to load bundled tokenizer %s from %s (%s); using char/4 fallback",
            tokenizer_id, path, type(exc).__name__,
        )
        return None


def get_tokenizer(tokenizer_id: Optional[str]):
    """Return a cached Tokenizer for ``tokenizer_id``, or None.

    None means "use the char/4 fallback" — every call site must accept
    that return cleanly. None is returned for empty/missing ids, when
    offline, when the load fails, or when the package is missing.
    """
    if not tokenizer_id:
        return None
    cached = _TOKENIZER_CACHE.get(tokenizer_id)
    if cached is not None:
        return cached
    if tokenizer_id in _TOKENIZER_FAILURES:
        return None
    with _CACHE_LOCK:
        cached = _TOKENIZER_CACHE.get(tokenizer_id)
        if cached is not None:
            return cached
        if tokenizer_id in _TOKENIZER_FAILURES:
            return None
        tok = _load_tokenizer(tokenizer_id)
        if tok is None:
            _TOKENIZER_FAILURES[tokenizer_id] = "load_failed"
            return None
        _TOKENIZER_CACHE[tokenizer_id] = tok
        return tok


def reset_cache_for_tests() -> None:
    """Clear caches. Test-only — production has no reason to call this."""
    with _CACHE_LOCK:
        _TOKENIZER_CACHE.clear()
        _TOKENIZER_FAILURES.clear()


def _fallback_count(text: str) -> int:
    """Char/4 estimator. Identical floor as the legacy stats helper."""
    if not text:
        return 0
    return max(1, int(len(text) / _FALLBACK_CHARS_PER_TOKEN))


def count_tokens(text: str, tokenizer_id: Optional[str] = None) -> int:
    """Token count for a single string.

    Uses the configured tokenizer when available, else char/4 fallback.
    Empty / None input returns 0.
    """
    if not text:
        return 0
    tok = get_tokenizer(tokenizer_id)
    if tok is None:
        return _fallback_count(text)
    try:
        return len(tok.encode(text, add_special_tokens=False).ids)
    except Exception:
        # HF tokenizer occasionally raises on pathological input
        # (e.g. unsupported codepoint range). Fall back gracefully so
        # one bad message doesn't take down the whole turn.
        return _fallback_count(text)


def _flatten_message_text(message: Dict[str, Any]) -> str:
    """Render a message into a single string for tokenization.

    Anthropic-style messages may carry content as a string OR as a list
    of typed blocks (text, tool_use, tool_result, image). We approximate
    each block's token contribution by serializing its salient payload —
    tool_use input dicts get JSON-stringified, tool_results get their
    string payload (truncated when not a string).
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", "") or "")
        elif btype == "tool_use":
            # Tool calls cost roughly: name + JSON-rendered input.
            name = block.get("name", "")
            tool_input = block.get("input")
            try:
                import json as _json
                rendered_input = _json.dumps(tool_input, ensure_ascii=False) if tool_input else ""
            except (TypeError, ValueError):
                rendered_input = str(tool_input) if tool_input is not None else ""
            parts.append(f"{name} {rendered_input}")
        elif btype == "tool_result":
            payload = block.get("content")
            if isinstance(payload, str):
                parts.append(payload)
            elif isinstance(payload, list):
                # Nested block list — recurse one level (rare in practice).
                for sub in payload:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        parts.append(sub.get("text", "") or "")
            else:
                parts.append(str(payload) if payload is not None else "")
    return " ".join(p for p in parts if p)


def count_message_tokens(message: Dict[str, Any], tokenizer_id: Optional[str] = None) -> int:
    """Token count for a single message dict (string-or-blocks content)."""
    flat = _flatten_message_text(message)
    return count_tokens(flat, tokenizer_id=tokenizer_id)


def count_messages_tokens(
    messages: Iterable[Dict[str, Any]],
    tokenizer_id: Optional[str] = None,
) -> int:
    """Sum of token counts across all messages in the iterable.

    Anthropic and Ollama both add framing tokens (role markers, message
    separators) on top of the content tokens. We deliberately don't model
    that framing here — it's small (~4–8 tokens per message) and varies
    by provider/template; the compaction trigger is conservative enough
    that the framing slack stays inside the 70% threshold's headroom.
    """
    return sum(count_message_tokens(m, tokenizer_id=tokenizer_id) for m in messages)
