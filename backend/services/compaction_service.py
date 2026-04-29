"""Production-side context-overflow compaction (ADR 009).

This is the **production** wiring of ADR 009's retrieval-first compaction
strategy, not the eval-side scaffold. The eval scaffold (in
``backend/tests/eval/conversations/strategies.py``) reaches into the
*dropped portion of the conversation history* to re-inject earlier turns;
production reaches into the **markdown vault** through
``services.retrieval.sessions.find_earlier_turn_context`` so the substrate
is the canonical store, not the volatile in-memory history.

Why this lives in ``services/`` rather than as a ``ContextStrategy``:

The ``ContextStrategy`` protocol takes ``messages → messages``. ADR 009's
compaction needs more than that — it inspects the **system prompt** to
budget against, it knows the **active model** for the effective ceiling,
it needs an **async retrieval** call into the vault, and it writes a
**compaction-event audit log** on the session row. A pure-sync identity-
shaped strategy can't carry those dependencies cleanly. Compaction is
therefore wired in the chat router itself (see
``backend/routers/chat.py::_handle_message``) as a step that runs *after*
the strategy assembles the history but *before* dispatch.

Atomicity (ADR 009 §"Atomicity"):

Compaction runs **only** at the per-turn boundary in ``_handle_message``.
The tool-call loop in ``_stream_follow_up`` deliberately does NOT call
``compact_messages`` mid-loop — that would risk re-assembling the history
between a ``tool_use`` block and its matching ``tool_result``, which the
provider rejects. The mid-loop ``_compact_stale_tool_results`` handles
the only safe in-loop compaction (collapsing prior tool_result payloads).

What this module is responsible for:

1. Stripping ephemeral ``<think>...</think>`` scratchpad blocks from
   assistant turns (Qwen3 Thinking variants leak these). They never
   count toward the recent window.
2. Token-aware budget tracking against the active model's
   ``effective_context_tokens`` ceiling.
3. The 70% proactive trigger (configurable via
   ``JARVIS_COMPACTION_THRESHOLD_PCT``).
4. Recent-N protection — the last N user-turn boundaries are never
   compacted, default N=8 (the gate-validated optimum from ADR 010).
5. Vault retrieval substitution — top-K dropped-equivalent context from
   ``memory/conversations/`` is prepended as a synthesized user-role
   block. Default K=3.
6. Returning a structured ``CompactionResult`` so the chat router can
   write the audit log and emit a UI event.

What this module is NOT responsible for:

- The system-prompt budget cap. That's enforced inside
  ``services.context_builder`` before the prompt is returned to the
  router; this module budgets *history* against what's left after the
  system prompt.
- Summary fallback. The ADR keeps summary as a Granite-4-driven
  fallback for cases retrieval can't substitute. v1 of production
  compaction uses retrieval-only since that's what the eval validated.
  Summary is on the open follow-up list and is the natural extension
  point if real-usage data shows retrieval-substitution misses.
- Pin-turn / re-include affordances. Those are frontend chunks that
  attach to the same compaction event log this module writes.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from services import token_counting

logger = logging.getLogger(__name__)


# Default recent-window size. ADR 010's gate (run-20260428T112547Z) fixed
# this at 8 — the empirical optimum that lifted clean-pass rate to match
# full-history. Hardware-floor profiles can lower it via the env var.
DEFAULT_RECENT_N = 8
# Default vault retrieval depth; same gate verdict picked top_k=3.
DEFAULT_TOP_K = 3
# Default proactive trigger as a fraction of the model's effective ceiling.
# ADR 009 §"Proactive trigger at 70%" — initial best-guess; tunable via env
# and to be re-baselined once production usage data flows.
DEFAULT_THRESHOLD_PCT = 0.70

# Hard floor on the recent window. Never compact below this many user
# turns even if the budget would otherwise demand it — a 1-turn window
# on a small model collapses the conversation. The user can always pin
# turns explicitly; this floor protects them from the strategy itself.
_RECENT_N_FLOOR = 2
# Hard ceiling. Without it, an operator misconfiguring the env var to
# a very large value (e.g. 1_000_000) would silently disable the
# strategy: ``len(user_turn_indices) <= recent_n`` would always be
# True, the function would return ``recent_window_already_minimal``,
# and the model would receive an unbounded history. The cap keeps the
# compaction mechanism actually engaged. 200 is well above any
# realistic conversation length and well below the
# ``MAX_HISTORY_MESSAGES`` ceiling × order-of-magnitude headroom.
_RECENT_N_CEILING = 200

# Strip Qwen3-style <think>...</think> blocks. Non-greedy, multiline. The
# model's *current* response may emit these and they're already filtered
# at stream time; this regex catches any that leaked into stored history.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


@dataclass
class CompactionResult:
    """Structured outcome of a single compaction call.

    The router uses ``messages`` as the message list to dispatch and
    writes the rest to the session's compaction-event log if
    ``compacted`` is True. ``retrieval_results`` carries the vault paths
    that fed the substitution block — surfaced to the trace UI so the
    user can see what was substituted.
    """

    messages: List[Dict[str, Any]]
    compacted: bool = False
    turns_dropped: int = 0
    summary_used: bool = False
    recent_window_size: int = 0
    effective_ctx: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    threshold_pct: float = DEFAULT_THRESHOLD_PCT
    retrieval_results: List[Dict[str, str]] = field(default_factory=list)
    reason: str = "no_compaction_needed"

    def as_event(self) -> Dict[str, Any]:
        """JSON-serialisable shape for the session audit log.

        Mirrors the ADR 009 §"Audit trail" schema (``timestamp,
        turns_dropped, summary_used, recent_window_size,
        effective_ctx_at_event``) plus the token deltas and retrieval
        attribution that compliance buyers asked about during the design
        review.
        """
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "turns_dropped": self.turns_dropped,
            "summary_used": self.summary_used,
            "recent_window_size": self.recent_window_size,
            "effective_ctx_at_event": self.effective_ctx,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "threshold_pct": self.threshold_pct,
            "retrieval_paths": [r.get("path", "") for r in self.retrieval_results],
            "reason": self.reason,
        }


def _resolve_threshold_pct(override: Optional[float]) -> float:
    """Threshold resolution order: explicit kwarg > env var > default."""
    if override is not None:
        return override
    raw = os.environ.get("JARVIS_COMPACTION_THRESHOLD_PCT")
    if not raw:
        return DEFAULT_THRESHOLD_PCT
    try:
        val = float(raw)
    except ValueError:
        logger.warning(
            "JARVIS_COMPACTION_THRESHOLD_PCT=%r is not a float; using default %s",
            raw, DEFAULT_THRESHOLD_PCT,
        )
        return DEFAULT_THRESHOLD_PCT
    # Clamp to a sensible operational range. Anything outside [0.3, 0.95]
    # is almost certainly a misconfig — don't silently use 0 or 1.0.
    if val < 0.30 or val > 0.95:
        logger.warning(
            "JARVIS_COMPACTION_THRESHOLD_PCT=%s out of [0.30, 0.95]; using default",
            val,
        )
        return DEFAULT_THRESHOLD_PCT
    return val


def _clamp_recent_n(value: int, *, source: str) -> int:
    """Clamp a recent_n value to ``[_RECENT_N_FLOOR, _RECENT_N_CEILING]``.

    Logs a warning when the input lands outside the operational range
    so a misconfiguration is visible rather than silently neutered.
    """
    if value < _RECENT_N_FLOOR:
        logger.warning(
            "%s requested recent_n=%s, below floor %s; clamping to floor",
            source, value, _RECENT_N_FLOOR,
        )
        return _RECENT_N_FLOOR
    if value > _RECENT_N_CEILING:
        logger.warning(
            "%s requested recent_n=%s, above ceiling %s; clamping to ceiling. "
            "An out-of-range value would silently disable compaction by always "
            "exceeding the conversation length.",
            source, value, _RECENT_N_CEILING,
        )
        return _RECENT_N_CEILING
    return value


def _resolve_recent_n(override: Optional[int]) -> int:
    if override is not None:
        return _clamp_recent_n(override, source="recent_n kwarg")
    raw = os.environ.get("JARVIS_COMPACTION_RECENT_N")
    if not raw:
        return DEFAULT_RECENT_N
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning(
            "JARVIS_COMPACTION_RECENT_N=%r is not an int; using default %s",
            raw, DEFAULT_RECENT_N,
        )
        return DEFAULT_RECENT_N
    return _clamp_recent_n(parsed, source="JARVIS_COMPACTION_RECENT_N")


def _is_real_user_turn(msg: Dict[str, Any]) -> bool:
    """Same definition as the eval-side strategy.

    A user message whose only content is tool_result blocks is a
    protocol-mandated tool response, not a real user input. Counting
    those would inflate the recent-N window with mechanical tool-loop
    chatter and shrink the user-visible memory window.
    """
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        if not content:
            return True
        all_tool_results = all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        return not all_tool_results
    return True


def _strip_think_blocks(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop ``<think>...</think>`` scratchpad from assistant string content.

    Non-string content (block lists) is unchanged — ``<think>`` only
    appears in raw text streams, and we never want to mutate tool_use /
    tool_result block payloads from this layer.
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            out.append(msg)
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            out.append(msg)
            continue
        if "<think>" not in content:
            out.append(msg)
            continue
        cleaned = _THINK_BLOCK_RE.sub("", content).strip()
        if not cleaned:
            # Pure-think turn — nothing useful left. Drop to keep the
            # conversation coherent (sequential assistant blocks with no
            # text confuse small models).
            continue
        out.append({**msg, "content": cleaned})
    return out


def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    """Most recent real-user-turn text, used as the retrieval query."""
    for msg in reversed(messages):
        if not _is_real_user_turn(msg):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", "") or "")
            return " ".join(parts)
    return ""


def _synthesize_retrieval_block(retrieved: List[Dict[str, str]]) -> Dict[str, Any]:
    """Build a leading assistant-role message summarizing the vault matches.

    Uses the **assistant** role so the post-compaction message sequence
    is ``[assistant(synth), user(real_kept_turn_0), …]`` — alternating
    roles, no consecutive same-role messages. The original v1 used a
    user role, but ``kept[0]`` is always a real user turn (the cut
    lands at a user-turn boundary by construction), so a user-role
    substitution would produce two consecutive user-role messages.
    Anthropic accepts that shape, but Ollama chat templates and
    LiteLLM adapters vary — some merge consecutive same-role messages,
    silently fusing the substitution block into the real user turn and
    corrupting both the user's question and the model's interpretation.
    Assistant-role is universally accepted as a preamble and reads
    naturally as "the assistant recalls earlier context."

    Note: compaction is gated to ``provider == "ollama"`` in
    ``_maybe_compact``, so the Anthropic-specific "first message must
    be user" constraint never applies here. If compaction is later
    extended to cloud providers, this contract must be revisited.
    """
    lines = [
        "Recalling earlier conversation context retrieved from the vault:",
    ]
    for r in retrieved:
        title = r.get("title") or r.get("path") or "earlier conversation"
        snippet = r.get("snippet", "").strip()
        if not snippet:
            continue
        lines.append("")
        lines.append(f"From note '{title}':")
        lines.append(snippet)
    lines.append("")
    lines.append("End of recalled context. Continuing with the visible conversation below.")
    return {"role": "assistant", "content": "\n".join(lines)}


async def compact_messages(
    messages: List[Dict[str, Any]],
    *,
    effective_context_tokens: int,
    tokenizer_id: Optional[str] = None,
    system_prompt_tokens: int = 0,
    output_reserve_tokens: int = 4096,
    recent_n: Optional[int] = None,
    top_k: Optional[int] = None,
    threshold_pct: Optional[float] = None,
    current_session_id: str = "",
    workspace_path: Optional[Path] = None,
) -> CompactionResult:
    """Apply ADR 009 compaction to ``messages`` for the given budget.

    Parameters
    ----------
    messages
        The full session history (already strategy-assembled) the chat
        router is about to dispatch.
    effective_context_tokens
        The active model's RULER-safe ceiling. From
        ``ModelCatalogEntry.effective_context_tokens``.
    tokenizer_id
        HuggingFace tokenizer id for accurate counting. ``None`` falls
        back to the char/4 estimator (still functional, less accurate).
    system_prompt_tokens
        Count of the assembled system prompt. Subtracted from the
        ceiling so the history-only budget is honest.
    output_reserve_tokens
        Reserve for the model's response. Default 4096 matches
        ``llm_service.py``'s ``max_tokens`` cap.
    recent_n
        Number of recent user turns to keep verbatim. ``None`` →
        ``JARVIS_COMPACTION_RECENT_N`` env var, then 8.
    top_k
        Number of vault matches to substitute. ``None`` →
        ``DEFAULT_TOP_K`` (3).
    threshold_pct
        Trigger threshold as a fraction of the budget. ``None`` →
        ``JARVIS_COMPACTION_THRESHOLD_PCT`` env var, then 0.70.
    current_session_id
        The active session's id, so the vault lookup can exclude this
        conversation's own saved snapshot.
    workspace_path
        Optional override; falls through to settings in
        ``memory_service``.

    Returns
    -------
    CompactionResult
        ``messages`` is the assembled output (always a fresh list).
        ``compacted`` is True only when the function actually dropped
        turns; pre-trigger calls (under threshold) return the original
        messages with ``compacted=False`` and a token-count snapshot.
    """
    threshold = _resolve_threshold_pct(threshold_pct)
    recent = _resolve_recent_n(recent_n)
    k = top_k if top_k is not None else DEFAULT_TOP_K

    # Stage 1 — strip <think> scratchpad. Cheap, always applied.
    cleaned = _strip_think_blocks(list(messages))

    history_tokens = token_counting.count_messages_tokens(cleaned, tokenizer_id=tokenizer_id)
    headroom = max(0, effective_context_tokens - system_prompt_tokens - output_reserve_tokens)
    budget_tokens = int(headroom * threshold)

    # Sanity: a misconfig that leaves no headroom (e.g. system prompt
    # already exceeds the model's ceiling) would set budget_tokens=0
    # and force compaction even on a 1-message conversation. Don't.
    if budget_tokens <= 0:
        logger.warning(
            "Compaction budget <= 0 (effective=%s, system=%s, reserve=%s); skipping compaction",
            effective_context_tokens, system_prompt_tokens, output_reserve_tokens,
        )
        return CompactionResult(
            messages=cleaned,
            compacted=False,
            tokens_before=history_tokens,
            tokens_after=history_tokens,
            effective_ctx=effective_context_tokens,
            recent_window_size=_count_real_user_turns(cleaned),
            threshold_pct=threshold,
            reason="budget_too_small",
        )

    if history_tokens <= budget_tokens:
        return CompactionResult(
            messages=cleaned,
            compacted=False,
            tokens_before=history_tokens,
            tokens_after=history_tokens,
            effective_ctx=effective_context_tokens,
            recent_window_size=_count_real_user_turns(cleaned),
            threshold_pct=threshold,
            reason="under_threshold",
        )

    # Stage 2 — drop older turns. Find the cut index at the recent_n-th-
    # to-last real user turn; everything before that is dropped from
    # active context.
    user_turn_indices = [i for i, m in enumerate(cleaned) if _is_real_user_turn(m)]
    if len(user_turn_indices) <= recent:
        # Already within recent_n; nothing to drop. Token budget is busted
        # but this strategy can't help — would need summary-fallback or a
        # bigger model. Surface it in the audit log.
        logger.info(
            "Compaction trigger met (history=%s > budget=%s) but only %s user turns "
            "exist (<= recent_n=%s); leaving messages intact",
            history_tokens, budget_tokens, len(user_turn_indices), recent,
        )
        return CompactionResult(
            messages=cleaned,
            compacted=False,
            tokens_before=history_tokens,
            tokens_after=history_tokens,
            effective_ctx=effective_context_tokens,
            recent_window_size=len(user_turn_indices),
            threshold_pct=threshold,
            reason="recent_window_already_minimal",
        )

    cut_index = user_turn_indices[-recent]
    kept = cleaned[cut_index:]
    turns_dropped = len(user_turn_indices) - recent

    # Stage 3 — vault retrieval substitution. Late import to keep the
    # services package import graph acyclic (retrieval imports
    # memory_service which doesn't import this module, but compaction
    # *triggering* an import of retrieval at module load would entangle
    # the chat-router → services chain).
    retrieved: List[Dict[str, str]] = []
    if k > 0:
        from services.retrieval.sessions import find_earlier_turn_context
        query = _last_user_text(kept)
        if query:
            try:
                retrieved = await find_earlier_turn_context(
                    query,
                    current_session_id=current_session_id,
                    top_k=k,
                    workspace_path=workspace_path,
                )
            except Exception as exc:  # noqa: BLE001 — never break compaction
                logger.warning("Vault retrieval failed during compaction: %s", exc)
                retrieved = []

    output: List[Dict[str, Any]] = []
    if retrieved:
        output.append(_synthesize_retrieval_block(retrieved))
    output.extend(kept)

    tokens_after = token_counting.count_messages_tokens(output, tokenizer_id=tokenizer_id)

    # Observability for the overflow case: if the post-compaction
    # history still exceeds the headroom (kept window itself too large,
    # or large vault snippets pushed it back over), surface it in the
    # logs. The audit-event payload's tokens_after / effective_ctx are
    # the persistent record; this log is the live signal an operator
    # would page on. Threshold-based gating is an ADR extension; this
    # chunk lands the diagnostic only.
    headroom = max(0, effective_context_tokens - system_prompt_tokens - output_reserve_tokens)
    if headroom and tokens_after > headroom:
        logger.warning(
            "Post-compaction history (%s tokens) still exceeds headroom (%s tokens) "
            "for session %s; recent_window=%s, retrieval_count=%s. Consider "
            "lowering JARVIS_COMPACTION_RECENT_N or shrinking vault snippets.",
            tokens_after, headroom,
            current_session_id or "<unknown>",
            recent, len(retrieved),
        )

    return CompactionResult(
        messages=output,
        compacted=True,
        turns_dropped=turns_dropped,
        summary_used=False,
        recent_window_size=recent,
        effective_ctx=effective_context_tokens,
        tokens_before=history_tokens,
        tokens_after=tokens_after,
        threshold_pct=threshold,
        retrieval_results=retrieved,
        reason="compacted",
    )


def _count_real_user_turns(messages: List[Dict[str, Any]]) -> int:
    return sum(1 for m in messages if _is_real_user_turn(m))
