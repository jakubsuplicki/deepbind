# ADR 009 — Retrieval-first context-overflow compaction

**Status:** Accepted
**Date:** 2026-04-27
**Related:** [ADR 004](004-inference-router-architecture.md) · [ADR 008](008-conversation-pinned-chat-model.md) · [`docs/features/retrieval.md`](../../features/retrieval.md) · [`docs/features/sessions.md`](../../features/sessions.md) · [`docs/research/product-direction-v1-v2.md`](../../research/product-direction-v1-v2.md) §5

## Context

A long-running conversation eventually exceeds the chat model's context window. The current codebase compacts on one narrow surface — tool results within a single turn — and nowhere else.

What exists today, per code inspection:

- **Tool result truncation** ([`chat.py:79–105`](../../../backend/routers/chat.py#L79)). Single tool result soft-capped at 8000 chars (~2000 tokens) with head+tail truncation. Stale tool results across rounds collapsed to 600 chars (~150 tokens). Tested in [`test_chat_compaction.py`](../../../backend/tests/test_chat_compaction.py).
- **Per-note truncation in retrieval** ([`context_builder.py:121,263,660`](../../../backend/services/context_builder.py#L121)). Notes truncated at 500–1500 chars when surfaced into the system prompt. Retrieval-side, not history-side.
- **Output cap** of `max_tokens: 4096` per response ([`llm_service.py:80`](../../../backend/services/llm_service.py#L80)).

What does not exist:

- **Conversation history compaction across turns.** [`session_service.get_messages()`](../../../backend/services/session_service.py) returns the full history every turn. After enough turns, the model's context window is exceeded and the inference call errors out.
- **Token-aware budget tracking.** All caps are in *characters*. Different models have different tokenizers; character-to-token ratio varies by 20–40% on non-English content (Polish in particular).
- **Effective-context awareness per model.** [`model-research-1.md`](../../research/models/model-research-1.md) §"Long-context performance" was emphatic: advertised context is roughly 2× the RULER-safe context. The codebase has no concept of "this model's safe budget is 32K, not the advertised 128K."
- **Proactive overflow trigger.** Compaction fires reactively only across tool rounds in one turn.
- **System-prompt-overflow handling.** A retrieval surfacing 50 notes can balloon the system prompt past the model.

The product has a structural advantage that most multi-turn-chat products don't: **the conversation history is already markdown-backed.** [`docs/features/sessions.md`](../../features/sessions.md) describes session-to-memory persistence; conversations are saved as markdown files in the canonical vault. When older turns drop out of the active context window, they are not lost — they remain in the vault and are re-discoverable via the existing retrieval pipeline.

This makes a different compaction strategy possible than the standard summarization-only pattern: **retrieval substitution**, where dropped turns are re-injected via search when relevant, rather than collapsed into a lossy summary.

## Decision drivers

1. **Compaction must not be silent.** Hidden context loss in a compliance product is a credibility leak. The user must be able to see what is in active context and what has been deferred to retrieval-substitution.
2. **The canonical Markdown vault is authoritative.** Compaction touches active context, never the stored history. Anything dropped from active context remains queryable.
3. **Token counting must use the model's actual tokenizer.** Character approximation breaks across languages and models. HuggingFace tokenizers are available for every model in the strict-OSI catalog.
4. **Effective context, not advertised context, defines the budget.** Default to `min(native, RULER_safe_estimate)` — typically ~32K for the Qwen3 family.
5. **Recent turns are the place small-model attention is weakest.** [Product-direction §5](../../research/product-direction-v1-v2.md) is explicit. Compaction must protect the recent window aggressively, not strip it.
6. **Compaction cannot fire mid-stream or mid-tool-loop.** Same atomicity policy as [ADR 008](008-conversation-pinned-chat-model.md).

## Decision

### Strategy: retrieval-first, summary-fallback

When approaching the context budget, the dispatcher applies in order:

1. **Strip ephemeral content** — `<think>` blocks (Qwen3 Thinking variants) are scratchpad, not output. Drop from history aggressively. They never count toward the recent window.
2. **Compact stale tool results** — already implemented at [`chat.py:108`](../../../backend/routers/chat.py#L108). Extend across turns, not just within a turn.
3. **Drop older turns from active context, keep them in the vault.** The recent N turns (configurable per profile, default 8–10) stay verbatim. Older turns are removed from the active message list but remain in the session row + canonical Markdown vault.
4. **Re-inject by retrieval when relevant.** Subsequent turns referencing dropped content trigger the existing retrieval pipeline ([`retrieval`](../../features/retrieval.md)) to pull the relevant earlier turn back as a regular retrieved chunk. Same retrieval path that surfaces notes.
5. **Summary fallback** for cases retrieval cannot substitute — long mid-conversation tool exchanges where re-retrieving the right slice is hard. Generated via the **plumbing model** (Granite 4 H-Micro), not the conversational model. Keeps the chat model's attention undisturbed.

### Token-aware budget tracking

`ModelCatalogEntry` ([`ollama_service.py:113`](../../../backend/services/ollama_service.py#L113)) gains:

- `effective_context_tokens` — the operational ceiling, not the advertised number. Default: `min(native_ctx, RULER_safe_estimate)`. Per [`model-research-1.md`](../../research/models/model-research-1.md), this is typically ~32K for Qwen3 dense and ~64K for Qwen3 MoE.
- `tokenizer_id` — the HuggingFace tokenizer reference for accurate token counting.

The chat router computes `system_prompt_tokens + history_tokens + new_message_tokens` against `effective_context_tokens` before sending. **The cap is in tokens, not characters.**

### Proactive trigger at 70% of effective budget

Compaction fires when projected tokens exceed 70% of the effective ceiling. The threshold is a configurable parameter per profile (`ProfilePack.context_compaction_threshold`), with reasonable defaults; users on heavy long-context work can lower it.

70% is initial-best-guess and should be tuned with the [`eval-baseline`](../../concepts/eval-baseline.md) harness once compaction is wired.

### Recent verbatim window

The last N user/assistant turn pairs are never compacted. N defaults to 8 (configurable per profile via `ProfilePack.context_recent_n`). The recent window protects the conversation's working memory and the place small-model attention adherence is most fragile.

### System-prompt budget enforcement

[`build_system_prompt_with_stats()`](../../../backend/services/context_builder.py) currently truncates per-note at 500–1500 chars. It must additionally enforce a total system-prompt budget against `effective_context_tokens`. A retrieval that surfaces too many notes is capped against the total budget, with the same retrieval-priority order — most relevant retrieved first, lower-priority retrieved truncated or dropped first.

### Specialist persona / system invariants are re-injected aggressively as the window fills

[Product-direction §5](../../research/product-direction-v1-v2.md) describes a "background-agent re-injection loop" pattern for keeping small models on-instruction. Compaction is the natural place to implement it: as the recent window approaches the threshold, re-inject the active specialist persona and any active workflow invariants more frequently. The chat router gains a re-injection knob: at compaction time, ensure the system prompt's persona block is in the recent window (not just the system message), so attention sees it nearer the current turn.

### Atomicity

Compaction is **locked during a tool-call loop and during a stream.** Same in-flight lock as [ADR 008](008-conversation-pinned-chat-model.md). It runs only at safe boundaries — between turns, before the next request is dispatched.

### Cross-model swap interaction

When a manual model swap happens ([ADR 008](008-conversation-pinned-chat-model.md)), the new model gets the **compacted** history, not the full pre-compaction history. The compacted view is the canonical active context; the swap doesn't undo compaction.

### UI surface

The conversation UI shows which turns are **in active context** vs **in vault, retrievable**. Affordances:

- **Pin a turn** to keep it in active context regardless of compaction.
- **Re-include manually** — promote a vault turn back into active context.
- **See what was compacted** — turn-level summary visible when expanded.

This is the conversation analog of the runtime-model panel ([ADR 004](004-inference-router-architecture.md)) — context state is visible, not magic.

### Audit trail

Every compaction event writes to the session row: `{timestamp, turns_dropped, summary_used: bool, recent_window_size, effective_ctx_at_event}`. Compliance buyers can see what the model "saw" at each turn alongside the per-turn `model_id` from [ADR 008](008-conversation-pinned-chat-model.md).

## Alternatives considered

### A. Summarization-only compaction (LangChain default)
Summarize older turns through a model into a single "earlier we discussed: …" message. Industry-standard. Lossy by design — compresses through a model, throws away fidelity. Wrong fit for a product sold on rigor where the canonical vault is sitting right there as a higher-fidelity source. **Rejected as primary.** Used as fallback only.

### B. Sliding-window only (drop older turns, no recovery mechanism)
Brutal but predictable. Information loss is real and irreversible mid-conversation. Worse than retrieval-first since the vault is available. **Rejected.**

### C. Character-based budget approximation (preserve current shape)
Avoids tokenizer integration. Off by 20–40% on Polish; off by similar amounts on Chinese, Japanese, Arabic. Real EU customers in the target market will see misbehavior. **Rejected** — cost of correctness is small, cost of wrongness is real.

### D. Trust the advertised context window
Models advertise 128K, 256K, 1M. RULER says safe context is roughly 2× less. A budget against advertised context will see graceful degradation in best case, hallucination in worst case, well before the budget formally fills. **Rejected.**

### E. Compact silently with no UI surface
Maintains "the conversation just keeps working" magic. Same failure mode as silent model swap — unexplained behavior in a compliance context. **Rejected.**

### F. Per-turn summarization through the chat model itself
Saves the plumbing-model dependency. Disturbs the chat model's attention with summarization work; adds latency to every conversation turn. **Rejected.** Granite 4 H-Micro is small, fast, and exactly suited.

## Consequences

### Positive
- Long conversations remain usable. The wall is moved from "advertised ctx breaks" to "the user notices retrieval is doing its job."
- The product's source-of-truth doctrine pays off here: the markdown vault provides a higher-fidelity recovery path than summary compression.
- Token-correct budget tracking removes a class of language-correlated misbehavior.
- The audit trail proves what the model saw on each turn — a compliance asset.
- The recent-window protection plus persona re-injection addresses the small-model attention drift that [product-direction §5](../../research/product-direction-v1-v2.md) flagged as a v1 risk.

### Negative
- Retrieval substitution quality on "find the specific earlier turn that mentioned X" is unmeasured. The existing retrieval pipeline is tuned for note retrieval, not turn retrieval. If quality is poor, the user gets visible "I don't remember discussing X" effects in long conversations. Mitigated by audit trail, by the recent window, and by the manual re-include affordance.
- Tokenizer integration is a per-model dependency. HuggingFace tokenizers cover the catalog but add a Python dependency surface.
- The 70% threshold and recent-N defaults are educated guesses that need eval-driven tuning.
- Summary-fallback's quality varies with the plumbing model. Granite 4 H-Micro is small; its summary quality on conversation-style input is unmeasured in the research record.

### What this changes about existing code
- [`ModelCatalogEntry`](../../../backend/services/ollama_service.py#L113) gains `effective_context_tokens`, `tokenizer_id`, and (per [ADR 004](004-inference-router-architecture.md)) `bytes_per_kv_token`, `attention_arch`, `slot_class`.
- New `backend/services/token_counting.py` (or extension of an existing service) wrapping HuggingFace tokenizers.
- New `backend/services/compaction_service.py` — the compaction policy logic. Called from chat router before dispatch.
- [`chat.py:_handle_message`](../../../backend/routers/chat.py#L286) calls compaction at the per-turn boundary (never mid-stream, never mid-tool-loop).
- [`session_service`](../../../backend/services/session_service.py) gains a `compaction_event` log per session.
- [`context_builder`](../../../backend/services/context_builder.py) enforces a total system-prompt budget against `effective_context_tokens`, in addition to the existing per-note caps.
- [`retrieval`](../../features/retrieval.md) gains a "find earlier turn" entry point (re-uses the same retrieval pipeline with a session-scoped filter).
- New conversation UI elements: in-active-context indicator, pin-turn affordance, re-include affordance, compaction expand-summary view.
- [`ProfilePack`](../decisions/005-profile-driven-model-stacks.md) gains `context_recent_n`, `context_compaction_threshold`, `context_perf_mode_default`.

## Open follow-ups (non-blocking)

1. **Eval-driven tuning of thresholds.** The 70% trigger and recent-N=8 defaults need empirical validation via [`eval-baseline`](../../concepts/eval-baseline.md). Build the tuning harness alongside the compaction implementation.
2. **Retrieval-substitution quality measurement.** Specifically, "given a question that references a dropped turn, can retrieval surface it?" Add an eval scenario.
3. **Tokenizer caching.** HuggingFace tokenizer instances are not free to instantiate; cache per active loadout.
4. **Persona re-injection cadence.** The §5 background-agent pattern is sketched as "more aggressive as the window fills." Specific cadence needs measurement.
5. **Vault-roundtrip-as-context-strategy** ([product-direction §5](../../research/product-direction-v1-v2.md) "Main thread split into sub-threads") — sub-agent contexts that return distilled results to the main thread. Adjacent to compaction; recorded here as a related future direction, not a v1 commitment.
