"""Token counting for ADR 009 production-side compaction.

Why this exists:

ADR 009 §"Token-aware budget tracking" makes the compaction budget a
**token** number, not a character number. Character approximation is off
by 20–40% on Polish/Chinese/Japanese/Arabic — the very languages real EU
customers exercise — and that drift would push compaction to fire too
early or too late on non-English content. Concretely, a Polish question
that the user could ask in 80 chars tokenizes to ~40 tokens with the
Qwen3 tokenizer but to ~60 with the naive ``chars // 4`` rule. Cumulative
across long conversations the estimate drift breaks the 70%-budget
trigger entirely.

What this module does:

- Lazily loads HuggingFace tokenizers (the `tokenizers` package, not the
  full `transformers` stack) by ``tokenizer_id`` and caches the
  ``Tokenizer`` instance per process. First call may hit the HF cache /
  download; subsequent calls are ~microseconds per message.
- Counts tokens for a single string and for an Anthropic-style messages
  list (handling string content, tool-use blocks, tool-result blocks).
- Falls back to a deterministic ``chars // 4`` approximation when the
  tokenizer can't be loaded — offline, gated repo, transient network
  failure, or a model entry without a ``tokenizer_id``. The fallback is
  imprecise but keeps the system functional and is what the rest of the
  codebase has been doing for the entire pre-ADR-009 era.

Why not transformers / tiktoken:

- ``transformers`` pulls torch + huggingface_hub and is several hundred MB.
  ``tokenizers`` is a single Rust-backed Python package, ~2 MB on disk.
- ``tiktoken`` is for OpenAI tokenizers only (cl100k / o200k). Catalog
  models are Qwen / Granite / Mistral / Gemma; tiktoken would not match
  any of them, so its counts would still be approximations.
- For Anthropic chat the local catalog isn't relevant — the chat router
  short-circuits compaction when no local model entry is in play, and the
  Anthropic SDK manages its own context window server-side.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

# Process-wide cache of loaded Tokenizer instances. Keyed by tokenizer_id.
# The HF tokenizers library is thread-safe for encode() once constructed,
# so reads of the cache after population don't need the lock.
_TOKENIZER_CACHE: Dict[str, Any] = {}
# Failed loads are sticky — we don't re-attempt every turn after a failure
# (network down, gated repo, missing CDN). The value is the truthy
# exception type name; absence means "never attempted."
_TOKENIZER_FAILURES: Dict[str, str] = {}
_CACHE_LOCK = threading.Lock()

# Char-per-token ratio for the fallback estimator. 4.0 matches the
# pre-ADR-009 estimate used in build_system_prompt_with_stats. Bumping
# this requires re-baselining ADR 011 latency budgets.
_FALLBACK_CHARS_PER_TOKEN = 4.0


def _is_offline() -> bool:
    """Honor the standard HF offline env vars.

    Tests set these to keep CI deterministic and to avoid the daemon
    thread that the tokenizers package spins up for telemetry.
    """
    return (
        os.environ.get("HF_HUB_OFFLINE") == "1"
        or os.environ.get("TRANSFORMERS_OFFLINE") == "1"
        or os.environ.get("JARVIS_DISABLE_TOKENIZER_DOWNLOAD") == "1"
    )


def _load_tokenizer(tokenizer_id: str):
    """Load a HuggingFace tokenizer by id. Returns None on any failure.

    First call may download from the HF Hub. Subsequent calls are served
    from the on-disk cache (~/.cache/huggingface/hub/). We don't touch
    network when ``_is_offline()`` returns True — the test suite forces
    that via ``HF_HUB_OFFLINE=1`` so the unit tests stay deterministic.
    """
    if _is_offline():
        # Offline mode short-circuits before we even import the package,
        # so a stripped install (no `tokenizers` extras) doesn't blow up.
        return None
    try:
        from tokenizers import Tokenizer  # local import — keep import surface optional
    except ImportError:
        logger.info("tokenizers package not installed; falling back to char/4 estimator")
        return None
    try:
        return Tokenizer.from_pretrained(tokenizer_id)
    except Exception as exc:  # noqa: BLE001 — broad: HF raises many distinct types
        logger.warning(
            "Failed to load tokenizer %s (%s); using char/4 fallback",
            tokenizer_id, type(exc).__name__,
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
