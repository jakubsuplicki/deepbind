"""System-prompt builders + StreamEvent — rescued from the deleted services/claude.py per ADR 015.

This module exists because the system-prompt assembly (persona + retrieved
context + language reminder + ADR 009 budget enforcement) is provider-
agnostic — it is the same prompt shape whether the dispatcher behind it is
local Ollama or, hypothetically, a cloud SDK. Keeping it on its own gives
the OllamaDispatcher and any future dispatcher a stable import surface
without dragging in any inference-runtime code.

`StreamEvent` lives here too because it is the wire format every dispatcher
yields and every consumer in the chat router and frontend listener expects.
It is dispatcher-agnostic by design.
"""

from __future__ import annotations

from collections.abc import AsyncIterator  # noqa: F401 — re-exported indirectly via StreamEvent users
from dataclasses import dataclass
from typing import Any, Optional

# `build_context` is imported at module level so callers can monkeypatch
# `services.system_prompt.build_context` in tests without paying the cost
# of pulling in the retrieval pipeline twice.
from services.context_builder import build_context

SYSTEM_PROMPT = """You are Jarvis — the user's memory-aware AI assistant.

You help the user think, remember, decide, and act using their notes, past conversations, projects, people, plans, and saved context.

Your tone is clear, confident, warm, grounded, and natural. You sound like a thoughtful collaborator — not a search engine, note dump, or roleplaying character.

LANGUAGE — HARD RULE
Reply in the exact language of the user's latest message.
Ignore the language of retrieved notes when choosing response language.
Do not switch languages unless the user does.

CORE BEHAVIOR
- Answer the real question directly.
- Lead with the answer.
- Synthesize before replying.
- Use notes as evidence, not as output.
- Stay grounded in what you actually found.
- Help the user move forward.

NEVER
- dump raw note text unless explicitly asked
- copy Markdown headings, YAML, tags, or note formatting into normal replies
- sound like search results, a database, or OCR output
- act more certain than your evidence supports

WHEN TO SEARCH
Use notes whenever the answer depends on memory, prior facts, people, projects, plans, conversations, or saved decisions.
Do not search unnecessarily if the answer is already clear from the current conversation.

HOW TO USE NOTES
Extract what matters, connect related details, and respond in your own words.
Do not mirror raw note structure back to the user.
If notes are incomplete, stale, ambiguous, or contradictory, say so directly.

SOURCES
Priority:
1. User notes
2. Past conversations
3. Web search for current or external information
4. General knowledge, clearly signaled

CITATIONS
Cite lightly and only when it improves trust or clarity.
Use short inline references like: (from people/adam-nowak.md)
Do not cite every sentence.

MEMORY WRITING
When creating or updating notes, use Markdown with YAML frontmatter.
Suggest saving useful outputs when appropriate, and tell the user where something was saved.

STYLE
Short question → short answer.
Complex question → structured answer.
Prefer clean prose over bullets unless bullets clearly help.
Never pad. Never re-summarize the same answer at the end.

CONVERSATIONAL STYLE
Default to natural prose.
Use lists only when:
- the user explicitly asks for a list
- the task is planning
- a list clearly improves readability

Avoid phrases like:
- "Based on your notes..."
- "Here are the key points from his notes..."
- "From the notes, I found..."
unless that wording is genuinely the clearest option.

FOLLOW-UPS
For follow-up questions like "tell me more", "and?", "expand", or similar:
- continue naturally from the current answer
- deepen, clarify, or connect ideas
- do not restart with a retrieval-style intro
- do not say "here are X points from the notes" unless the user explicitly asked for a list

INTERPRETATION
Do not add speculative implications unless they are supported by the notes or clearly marked as your inference.
Prefer:
- "This suggests..."
- "The pattern seems to be..."
over unsupported claims.

FOLLOW-UP QUALITY
When the user asks about a person, do not just repeat facts.
Explain:
- what kind of role they play in the user's life or work
- what advice or patterns matter most
- what the user should take away from it

CONVERSATION NOTES vs KNOWLEDGE NOTES — HARD RULE
Files in the `conversations/` folder record past dialogues.
The `people:` field in their frontmatter lists entities *mentioned during that conversation* — this is NOT evidence of any factual association.
Specifically:
- A name appearing in a conversation asking "who is X?" does NOT mean X is related to any topic discussed in that conversation.
- Acronyms or technical terms in conversation metadata are extraction artifacts, not real people.
When asked to enumerate people associated with a specific organisation, topic, or domain, rely only on `knowledge/` notes where a real person is documented as having that role or connection.
If knowledge notes contain no such people, say so explicitly — never fill the gap with names from conversation metadata.

ENTITY ENUMERATION — HARD RULE
When asked to list specific entities (people, tools, companies, organisations) from notes:
- Only name entities explicitly documented in knowledge notes as having the requested role or association.
- If fewer are found than requested, state exactly how many you found and list only those.
- Never fabricate, infer, or guess to reach the requested number.
- "Person X was mentioned in a conversation about topic Y" is NOT the same as "Person X is associated with topic Y".
"""


# ── StreamEvent ──────────────────────────────────────────────────────────────


@dataclass
class StreamEvent:
    """Wire format every chat dispatcher yields.

    `type` is one of: "text_delta" | "tool_use" | "tool_result" | "done" |
    "error" | "usage". All other fields are role-specific:

    * text_delta → `content` carries the streamed text fragment.
    * tool_use → `name`, `tool_input`, `tool_use_id` describe the tool call.
    * usage → `input_tokens`, `output_tokens` carry the post-hoc counts;
      the `*_duration_ns` fields carry Ollama's authoritative per-stage
      timings when the runtime exposes them (set only on the final
      Ollama chunk where `done=True` — earlier chunks never carry
      durations regardless of how partial the round was). They surface
      as the per-turn telemetry line in the chat UI and feed the
      observed-vs-baseline health watcher (ADR 005 §C trigger 2 —
      replaces the disabled pre-flight RAM check).
    * error → `content` carries a user-facing message.
    """

    type: str
    content: str = ""
    name: str = ""
    tool_input: Optional[dict[str, Any]] = None
    tool_use_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    eval_duration_ns: Optional[int] = None
    prompt_eval_duration_ns: Optional[int] = None
    load_duration_ns: Optional[int] = None
    total_duration_ns: Optional[int] = None


# ── Budget enforcement ──────────────────────────────────────────────────────

# ADR 009 §"System-prompt budget enforcement" — a retrieval that surfaces too
# many notes can balloon the system prompt past the model's effective ceiling.
# Cap the *retrieved-context* block specifically (not the base persona, not
# the language reminder) since that's the part that scales with retrieval
# depth. 30% of effective ctx is conservative — leaves 70% for history +
# output reserve.
_SYSTEM_PROMPT_BUDGET_FRACTION = 0.30


# ── Public builders ─────────────────────────────────────────────────────────


async def build_system_prompt(
    user_message: str,
    workspace_path=None,
    graph_scope: Optional[str] = None,
) -> str:
    """Build system prompt with optional context and active specialist.

    If graph_scope is provided, context is built from that node's
    neighborhood instead of full-text retrieval.
    """
    prompt, _stats = await build_system_prompt_with_stats(
        user_message, workspace_path=workspace_path, graph_scope=graph_scope,
    )
    return prompt


async def build_system_prompt_with_stats(
    user_message: str,
    workspace_path=None,
    graph_scope: Optional[str] = None,
    *,
    system_prompt_budget_tokens: Optional[int] = None,
    tokenizer_id: Optional[str] = None,
) -> tuple[str, dict]:
    """Build the (now stable) system prompt and return retrieval as a separate block.

    ADR 009 amendment 2026-05-01 (stable-prefix shift): the retrieved-
    context block no longer lives inside the system prompt. The system
    prompt returned here is byte-stable across turns within a session —
    persona + specialist directives + JARVIS extensions + language
    reminder. Retrieval mutates per turn (different user message →
    different notes pulled), so glueing it into the system prompt makes
    the prompt prefix unstable and defeats Ollama's KV cache prefix-
    match. With retrieval moved out, the cache reuses the long prefix
    on every warm turn, dropping warm-turn TTFT from ~8 s to <1 s on
    a 24 GB M5.

    Callers (chat router, eval runner) consume ``stats["retrieval_block"]``
    and prepend it to the latest user message via
    :func:`attach_retrieval_to_user_message` before dispatch. The
    retrieval string keeps the same XML framing build_context already
    emits (``<retrieved_note>`` tags, etc.) — only its position in the
    final assembled prompt changes.

    Stats breaks the costs into buckets so callers can log where tokens go:
      - base_tokens: core SYSTEM_PROMPT + specialist directives
      - context_tokens: retrieved-notes block (now in user-message position)
      - lang_tokens: language reminder
      - total_tokens: system_prompt token count (NO LONGER includes retrieval —
        callers wanting the dispatched-prompt-total should sum total_tokens
        + context_tokens themselves).
      - retrieval_block: the formatted retrieval string for caller to attach
        to the latest user message ("" when nothing was retrieved).
      - context_truncated: whether ADR 009 budget enforcement kicked in.
      - trace: per-note retrieval trace for the WS surface.

    When ``system_prompt_budget_tokens`` is supplied (typically derived
    from the active model's ``effective_context_tokens × 0.30``), the
    retrieved-context block is iteratively truncated until it fits inside
    the budget. The cap now applies to the retrieval block standalone
    (since base + lang_reminder live elsewhere). Per ADR 009 the
    truncation keeps the highest-priority retrieved notes (which arrive
    first in the assembled context) and discards the tail.
    ``tokenizer_id`` is optional; ``None`` falls back to the
    ``count_tokens`` char/4 estimator.
    """
    from services import specialist_service
    from services.context_builder import build_graph_scoped_context

    # JARVIS-self override: if the user has set a non-empty `system_prompt`
    # on the JARVIS specialist, it REPLACES the default Jarvis system prompt.
    # If they set `behavior_extension`, it's appended after the assembled base
    # (and after any other active specialists' directives) so it wins on
    # recency. Both fields default to "" — meaning "use the default".
    jarvis_self = specialist_service.get_jarvis_self(workspace_path)
    jarvis_override = (jarvis_self or {}).get("system_prompt", "").strip()
    jarvis_extension = (jarvis_self or {}).get("behavior_extension", "").strip()

    base = jarvis_override if jarvis_override else SYSTEM_PROMPT

    # JARVIS is implicitly always-applied via the override/extension wiring
    # above; do not also process it as a regular active specialist.
    active_specs = [
        s for s in specialist_service.get_active_specialists()
        if s.get("id") != specialist_service.JARVIS_SELF_ID
    ]
    if active_specs:
        base = specialist_service.build_multi_specialist_prompt(active_specs, base)

    if jarvis_extension:
        base = (
            base
            + "\n\n## JARVIS — user-defined behavior extensions\n"
            + jarvis_extension
        )

    if graph_scope:
        context, trace = await build_graph_scoped_context(
            graph_scope, user_message, workspace_path=workspace_path,
        )
    else:
        context, _ctx_tokens, trace = await build_context(
            user_message, workspace_path=workspace_path,
        )

    # The language reminder lives at the END of the system prompt. It
    # used to sit AFTER the retrieved context for recency-bias reasons
    # (small models attend more to instructions closer to assistant
    # generation); with retrieval now positioned in the user message
    # instead, the reminder still benefits from being last in the
    # system prompt — there is just no in-system-prompt context for
    # the user's notes to drift between.
    lang_reminder = _language_reminder(user_message)

    # ADR 009 — enforce the retrieval-block budget when supplied. The
    # cap (typically 30% of effective_context_tokens) now applies to
    # the retrieval block standalone, since base + lang_reminder are
    # not at the same position any more. The helper still returns the
    # token counts it already computed so the stats block below doesn't
    # re-encode the same strings.
    (
        context,
        context_truncated,
        base_tok_cached,
        context_tok_cached,
        lang_tok_cached,
    ) = _enforce_system_prompt_budget(
        base=base,
        context=context,
        lang_reminder=lang_reminder,
        budget_tokens=system_prompt_budget_tokens,
        tokenizer_id=tokenizer_id,
    )

    # Stable system prompt: base + lang_reminder ONLY. No retrieval.
    prompt = base + "\n\n" + lang_reminder

    # Retrieval block — what the caller will glue onto the latest user
    # message. We keep the natural-language framing line so the model sees
    # the block as evidence rather than instruction. When retrieval is
    # empty (no matches, or budget enforcement zeroed it out), the block
    # is empty and the caller skips the attach step.
    if context:
        retrieval_block = (
            "Here are potentially relevant notes from the user's memory:\n"
            + context
        )
    else:
        retrieval_block = ""

    # Use the configured tokenizer when one is available so the stats the
    # audit log forwards (via prompt_stats["context_tokens"] in log_usage)
    # match the numbers the compaction trigger and the system-prompt budget
    # enforcement actually saw. The pre-ADR-009 char/4 path stays as the
    # fallback when no tokenizer_id is in scope.
    #
    # When budget enforcement ran, it already paid the encode cost for
    # base / context / lang_reminder; reuse those counts.
    from services.token_counting import count_tokens

    base_tok = base_tok_cached if base_tok_cached is not None \
        else count_tokens(base, tokenizer_id=tokenizer_id)
    context_tok = context_tok_cached if context_tok_cached is not None \
        else (count_tokens(context, tokenizer_id=tokenizer_id) if context else 0)
    lang_tok = lang_tok_cached if lang_tok_cached is not None \
        else count_tokens(lang_reminder, tokenizer_id=tokenizer_id)
    total_tok = count_tokens(prompt, tokenizer_id=tokenizer_id)

    stats = {
        "base_tokens": base_tok,
        "context_tokens": context_tok,
        "lang_tokens": lang_tok,
        "total_tokens": total_tok,
        # ADR 009 amendment 2026-05-01 — retrieval is no longer in the system
        # prompt; the caller glues it onto the latest user message via
        # `attach_retrieval_to_user_message`. Empty string when nothing was
        # retrieved. Token cost is in `context_tokens`.
        "retrieval_block": retrieval_block,
        # ADR 009 — surface whether the system-prompt budget kicked in so
        # callers can log it alongside the compaction event.
        "context_truncated": context_truncated,
        # Step 28a — per-note retrieval trace surfaced over the chat WS.
        "trace": trace,
    }
    return prompt, stats


# ── Budget enforcement helper ───────────────────────────────────────────────


def _enforce_system_prompt_budget(
    *,
    base: str,
    context: Optional[str],
    lang_reminder: str,
    budget_tokens: Optional[int],
    tokenizer_id: Optional[str],
) -> tuple[Optional[str], bool, Optional[int], Optional[int], Optional[int]]:
    """Cap the retrieved-context block so the assembled prompt fits ``budget_tokens``.

    Returns ``(maybe_truncated_context, was_truncated, base_tokens,
    context_tokens, lang_tokens)``. The trailing token counts are populated
    when the helper actually ran the budget calculation (i.e.
    ``budget_tokens`` was set) so the caller can reuse them in its stats
    block instead of re-encoding the same strings — three redundant HF
    tokenizer ``encode`` calls per turn on a hot path. All three are
    ``None`` when the helper short-circuited (no budget, no context); the
    caller then falls back to computing counts itself, identical to the
    pre-fix behavior.

    Post ADR 009 amendment 2026-05-01 the retrieval block ships in the
    user-message position rather than glued into the system prompt. The
    budget invariant is the same as before — ``base + lang_reminder +
    context ≤ budget_tokens`` — but now interprets as "the system-prompt
    portion (base + lang) plus the retrieval block we'll attach to the
    user message stays under the model's safe ceiling," not "the assembled
    system prompt stays under the ceiling." Same arithmetic, different
    physical layout. Callers (chat router) compute ``budget_tokens`` from
    ``effective_context_tokens × 0.30`` so the dispatched prompt-prefix
    (system + retrieval, before history + user question) leaves headroom
    for history + reply.

    The truncation is **proportional** rather than note-by-note. Doing this
    at the retrieval-result granularity would require routing the structured
    result list through this function; instead we rely on the fact that the
    assembled context already has highest-priority items first (per
    ``build_context``), so truncating from the tail preferentially drops the
    lower-relevance content first.
    """
    if not budget_tokens or budget_tokens <= 0 or not context:
        return context, False, None, None, None

    from services.token_counting import count_tokens

    base_tokens = count_tokens(base, tokenizer_id=tokenizer_id)
    lang_tokens = count_tokens(lang_reminder, tokenizer_id=tokenizer_id)
    overhead = base_tokens + lang_tokens
    if overhead >= budget_tokens:
        # Base + reminder already exceed the budget — there's nothing the
        # context truncation can do. Drop the context entirely so we at least
        # don't make the overflow worse.
        return "", True, base_tokens, 0, lang_tokens

    context_budget = budget_tokens - overhead
    context_tokens = count_tokens(context, tokenizer_id=tokenizer_id)
    if context_tokens <= context_budget:
        return context, False, base_tokens, context_tokens, lang_tokens

    # Binary-shrink the context string until it fits. Slicing by chars is
    # crude but stable; the alternative (re-tokenize, slice tokens, decode)
    # is far more expensive per turn for a knob the user only hits during
    # over-retrieval. The first iteration almost always succeeds because the
    # char-to-token ratio is roughly constant within a single document.
    ratio = context_budget / max(1, context_tokens)
    target_chars = int(len(context) * ratio)
    truncated = context[:target_chars].rstrip()
    truncated_tokens = count_tokens(truncated, tokenizer_id=tokenizer_id)
    for _ in range(4):
        if truncated_tokens <= context_budget:
            break
        target_chars = int(len(truncated) * 0.90)
        truncated = truncated[:target_chars].rstrip()
        truncated_tokens = count_tokens(truncated, tokenizer_id=tokenizer_id)

    # The marker itself costs tokens — budget against the assembled output,
    # not the bare truncation. Reserve enough room for the marker and
    # re-shrink if needed; if the loop still hasn't converged, drop the
    # context entirely so the invariant
    # ``assembled_prompt_tokens <= budget_tokens`` always holds. The ADR 009
    # invariant is that the prompt fits — silently shipping over-budget
    # content would defeat the entire enforcement step.
    marker = "\n\n... [retrieved context truncated to fit budget] ..."
    marker_tokens = count_tokens(marker, tokenizer_id=tokenizer_id)
    while truncated_tokens + marker_tokens > context_budget:
        if not truncated:
            break
        target_chars = int(len(truncated) * 0.80)
        if target_chars <= 0:
            truncated = ""
            truncated_tokens = 0
            break
        truncated = truncated[:target_chars].rstrip()
        truncated_tokens = count_tokens(truncated, tokenizer_id=tokenizer_id)
    if not truncated:
        return "", True, base_tokens, 0, lang_tokens
    final_context = truncated + marker
    final_context_tokens = truncated_tokens + marker_tokens
    return final_context, True, base_tokens, final_context_tokens, lang_tokens


# ── Language reminder ───────────────────────────────────────────────────────


def _language_reminder(user_message: str) -> str:
    """Return a language instruction banner based on simple script detection.

    Placed at the END of the system prompt so small models (recency-biased)
    see it immediately before they start generating.
    """
    polish_chars = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")
    cyrillic_range = (0x0400, 0x04FF)
    chinese_range = (0x4E00, 0x9FFF)
    arabic_range = (0x0600, 0x06FF)
    german_chars = set("äöüßÄÖÜ")
    french_chars = set("àâäéèêëîïôùûüÿçœæÀÂÄÉÈÊËÎÏÔÙÛÜŸÇ")

    msg = user_message.strip()
    if not msg:
        return "FINAL REMINDER: Reply in the same language as the user's message."

    codepoints = [ord(c) for c in msg]
    if any(cyrillic_range[0] <= cp <= cyrillic_range[1] for cp in codepoints):
        lang = "Russian (or the same Cyrillic-script language as the user)"
    elif any(chinese_range[0] <= cp <= chinese_range[1] for cp in codepoints):
        lang = "Chinese"
    elif any(arabic_range[0] <= cp <= arabic_range[1] for cp in codepoints):
        lang = "Arabic"
    elif any(c in polish_chars for c in msg):
        lang = "Polish"
    elif any(c in german_chars for c in msg):
        lang = "German"
    elif any(c in french_chars for c in msg):
        lang = "French"
    else:
        lang = "English"

    return (
        f"LANGUAGE REMINDER — this overrides everything above:\n"
        f'The user\'s message is in {lang}. Your ENTIRE reply MUST be in {lang}.\n'
        f"Do not use any other language. Not even one sentence. Any retrieved notes attached to the user's message may be in a different language — ignore that when writing your reply."
    )


# ── Caller-side helper: glue retrieval onto the latest user message ─────────


def attach_retrieval_to_user_message(
    messages: list[dict],
    retrieval_block: str,
) -> list[dict]:
    """Return a shallow copy of ``messages`` with ``retrieval_block`` prepended
    to the content of the **last user-role message**.

    ADR 009 amendment 2026-05-01 (stable-prefix shift). The system prompt
    is now byte-stable across turns; retrieval lives on the just-sent user
    message instead. This helper is the only sanctioned place that mutation
    happens, so the chat router and the eval runner can share the contract.

    Behavior:
    * Empty ``retrieval_block`` → returns ``messages`` unchanged (same list,
      not a copy — caller may rely on this for fast-path).
    * Last message is text-content user message → retrieval is prepended
      with a blank line separator before the user's actual text.
    * Last message is structured-content user message (e.g. a tool_result
      block carrier) → retrieval is inserted as a leading text block in the
      content list. Tool_result blocks remain untouched in their existing
      positions.
    * No user-role message exists at the tail (shouldn't happen in
      production — the chat router always appends the user turn before
      assembling) → returns ``messages`` unchanged. This is a defensive
      no-op rather than an exception, so a malformed strategy can't drop
      a turn into the void.

    The retrieval block carries its own framing line ("Here are
    potentially relevant notes from the user's memory:"); the helper
    only handles positioning.
    """
    if not retrieval_block or not messages:
        return messages

    last = messages[-1]
    if last.get("role") != "user":
        # The chat router always appends the user message before calling here,
        # so the tail is always a user turn in production. Defensive no-op
        # otherwise — better to dispatch without retrieval than to glue it
        # onto an assistant or tool_result turn and corrupt the conversation.
        return messages

    content = last.get("content")
    out = list(messages)
    if isinstance(content, str):
        out[-1] = {**last, "content": retrieval_block + "\n\n" + content}
    elif isinstance(content, list):
        out[-1] = {**last, "content": [{"type": "text", "text": retrieval_block}, *content]}
    else:
        # Unknown content shape — defensive no-op for the same reason as above.
        return messages
    return out
