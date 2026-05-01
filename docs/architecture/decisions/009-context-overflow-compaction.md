# ADR 009 — Retrieval-first context-overflow compaction

**Status:** Accepted — production wiring landed
**Date:** 2026-04-27 (initial), amended 2026-04-28 (eval-side `retrieval-substitution-v1` landed; gate verdict justified production wiring), 2026-04-29 (production-side compaction wiring landed; same-day code-review fixes pass), 2026-05-01 (stable system-prompt prefix — retrieval moves to user-message position to restore Ollama KV-cache reuse on warm turns)
**Related:** [`docs/features/retrieval.md`](../../features/retrieval.md) · [`docs/features/sessions.md`](../../features/sessions.md) · [`docs/features/chat.md`](../../features/chat.md) · [`docs/research/product-direction-v1-v2.md`](../../research/product-direction-v1-v2.md) §5

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
6. **Compaction cannot fire mid-stream or mid-tool-loop.** Compaction runs only at turn boundaries — never during a streaming response or inside a tool-call loop.

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

Compaction fires when projected tokens exceed 70% of the effective ceiling. The threshold is a configurable parameter with a reasonable default; users on heavy long-context work can lower it.

70% is initial-best-guess and should be tuned with the [`eval-baseline`](../../concepts/eval-baseline.md) harness once compaction is wired.

### Recent verbatim window

The last N user/assistant turn pairs are never compacted. N defaults to 8 (configurable). The recent window protects the conversation's working memory and the place small-model attention adherence is most fragile.

### System-prompt budget enforcement

[`build_system_prompt_with_stats()`](../../../backend/services/context_builder.py) currently truncates per-note at 500–1500 chars. It must additionally enforce a total system-prompt budget against `effective_context_tokens`. A retrieval that surfaces too many notes is capped against the total budget, with the same retrieval-priority order — most relevant retrieved first, lower-priority retrieved truncated or dropped first.

### Specialist persona / system invariants are re-injected aggressively as the window fills

[Product-direction §5](../../research/product-direction-v1-v2.md) describes a "background-agent re-injection loop" pattern for keeping small models on-instruction. Compaction is the natural place to implement it: as the recent window approaches the threshold, re-inject the active specialist persona and any active workflow invariants more frequently. The chat router gains a re-injection knob: at compaction time, ensure the system prompt's persona block is in the recent window (not just the system message), so attention sees it nearer the current turn.

### Atomicity

Compaction is **locked during a tool-call loop and during a stream.** It runs only at safe boundaries — between turns, before the next request is dispatched.

### Cross-model swap interaction

When a manual model swap (or future memory-pressure auto-downgrade) happens, the new model gets the **compacted** history, not the full pre-compaction history. The compacted view is the canonical active context; the swap doesn't undo compaction.

### UI surface

The conversation UI shows which turns are **in active context** vs **in vault, retrievable**. Affordances:

- **Pin a turn** to keep it in active context regardless of compaction.
- **Re-include manually** — promote a vault turn back into active context.
- **See what was compacted** — turn-level summary visible when expanded.

Context state is visible, not magic — the user sees what the model "saw" at each turn.

### Audit trail

Every compaction event writes to the session row: `{timestamp, turns_dropped, summary_used: bool, recent_window_size, effective_ctx_at_event}`. Compliance buyers can see what the model "saw" at each turn alongside the per-turn `model_id`.

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
- [`ModelCatalogEntry`](../../../backend/services/ollama_service.py#L113) gains `effective_context_tokens`, `tokenizer_id`. (`bytes_per_kv_token` and `attention_arch` already shipped for the future memory-pressure auto-downgrade.)
- New `backend/services/token_counting.py` (or extension of an existing service) wrapping HuggingFace tokenizers.
- New `backend/services/compaction_service.py` — the compaction policy logic. Called from chat router before dispatch.
- [`chat.py:_handle_message`](../../../backend/routers/chat.py#L286) calls compaction at the per-turn boundary (never mid-stream, never mid-tool-loop).
- [`session_service`](../../../backend/services/session_service.py) gains a `compaction_event` log per session.
- [`context_builder`](../../../backend/services/context_builder.py) enforces a total system-prompt budget against `effective_context_tokens`, in addition to the existing per-note caps.
- [`retrieval`](../../features/retrieval.md) gains a "find earlier turn" entry point (re-uses the same retrieval pipeline with a session-scoped filter).
- New conversation UI elements: in-active-context indicator, pin-turn affordance, re-include affordance, compaction expand-summary view.
- Compaction config (`context_recent_n`, `context_compaction_threshold`) lives in `app/config.json`.

## Implementation status (2026-04-28)

The strategy described above is the production target. As of this amendment date, only the **eval-side scaffold** has landed; production-side compaction is unwired.

### What exists

- **ContextStrategy swap point in production.** [`backend/services/chat/context_strategy.py`](../../../backend/services/chat/context_strategy.py) defines the `ContextStrategy` Protocol; [`backend/routers/chat.py`](../../../backend/routers/chat.py) routes session history through it. The default is `FullHistoryStrategy` — identity over the input — so production behavior is unchanged. This is the swap point through which the eventual compaction strategy attaches; it landed empty so [ADR 010](010-conversation-replay-eval-harness.md)'s gate could compare alternatives against the real production path.
- **`retrieval-substitution-v1` exists, eval-side only.** [`backend/tests/eval/conversations/strategies.py`](../../../backend/tests/eval/conversations/strategies.py) implements `RetrievalSubstitutionV1Strategy(recent_n, top_k)`. It truncates to recent-N user turns (identical to `NaiveTruncateStrategy`), then scores each *dropped* user turn by deterministic content-token overlap with the latest user turn and prepends the top-K dropped (user, assistant) pairs in chronological order as a synthesized user-role block. ADR 010's gate compares it against `naive-truncate-N` at matched N to test "does retrieval-substitution earn its complexity over the cheap baseline."
- **Eval-side substrate vs production substrate diverges intentionally.** The eval-side v1 reaches into the *dropped portion of the conversation history* — the conversation is the corpus, because the eval fixtures don't have a populated workspace. The production strategy described above reaches into the **markdown vault** via the existing retrieval pipeline. Both test the same hypothesis (targeted retrieval can substitute for full-history) against the substrate available. The production strategy is the next iteration; the eval-side v1 isolates the retrieval-substitution variable from the workspace-population variable.

### What does not exist

- **Production-side compaction is unwired.** No `compaction_service.py`, no token-aware budget, no proactive 70% trigger, no system-prompt budget enforcement, no compaction event log, no UI surface. Conversations still receive the full history every turn until the model errors out.
- **Effective-context awareness per model.** `ModelCatalogEntry` does not yet have `effective_context_tokens` or `tokenizer_id`. The codebase still operates on character approximations.
- **Vault-roundtrip retrieval for dropped turns.** The retrieval pipeline does not yet have a "find earlier turn" entry point. Production compaction will need this.

### Sequencing note

The eval results from ADR 010 (pending the next 30B grid run with depth-pressure fixtures) will inform whether the production wiring should:

- **(a)** mirror the eval-side `retrieval-substitution-v1` directly (history-self-retrieval), or
- **(b)** follow the original ADR 009 design (vault-retrieval over markdown sessions),
- **(c)** or pick a different strategy entirely if both lose to a wider naive-truncate window.

A production-side wiring against the wrong choice is wasted work. Wait for the gate verdict, then commit.

### Naming clarification

"`retrieval-substitution-v1`" in the ADR text refers to the *eval-side scaffold*, not the production target. The production strategy will be a different class with a clearer name once the gate verdict picks one. This ADR is amended (rather than rewritten) so the design intent stays auditable; the production-target text above stands, but the v1 name in the codebase belongs to the eval-side variant.

## Gate verdict (2026-04-28 evening) — ADR 009 stands; production wiring justified

The conversation eval grid completed against `qwen3:14b` on M5 Pro 24 GB / Ollama 0.18.0 (run `tests/eval/conversations/baselines/run-20260428T112547Z.json`). The bootstrap-CI gate produced decisive verdicts on every comparison ADR 010 was filed to settle.

### Naive truncation vs full-history (the "is naive enough?" check)

| Comparison | Δ (B − A) | 95% CI | Verdict |
|---|---:|---|---|
| `full-history` vs `naive-truncate-4`  | −0.526 | [−0.789, −0.263] | **regression** |
| `full-history` vs `naive-truncate-8`  | −0.263 | [−0.474, −0.105] | **regression** |
| `full-history` vs `naive-truncate-12` | −0.211 | [−0.421, −0.053] | **regression** |
| `full-history` vs `naive-truncate-16` | −0.158 | [−0.316, +0.000] | equivalent (CI kisses zero) |

Naive truncation regresses against full-history at every aggressive window size. Even at N=16 the CI just barely includes zero — one more failing fixture flips it to regression. **Naive truncation is not a viable substitute for full-history on long-conversation fixtures.**

### Retrieval-substitution vs naive truncation at matched N (the "does retrieval earn its complexity?" check)

| Comparison | Δ (B − A) | 95% CI | Verdict |
|---|---:|---|---|
| `naive-truncate-4` vs `retrieval-substitution-v1-n4-k3` | **+0.474** | [+0.263, +0.684] | **improvement** |
| `naive-truncate-8` vs `retrieval-substitution-v1-n8-k3` | **+0.263** | [+0.105, +0.474] | **improvement** |

Both gates' CIs exclude zero on the improvement side at 95%. Retrieval-substitution lifts clean-pass rate by **+47 pp at recent_n=4** and **+26 pp at recent_n=8** over naive truncation at the same window size.

### Reconstructed absolute clean-pass rates (relative to full-history)

| Strategy | Distance from full-history clean-pass rate |
|---|---:|
| `full-history` | baseline (reference) |
| `retrieval-substitution-v1-n8-k3` | **≈ matches full-history** (Δ ≈ 0 pp) |
| `retrieval-substitution-v1-n4-k3` | −5 pp |
| `naive-truncate-16` | −16 pp |
| `naive-truncate-12` | −21 pp |
| `naive-truncate-8` | −26 pp |
| `naive-truncate-4` | −53 pp |

`retrieval-substitution-v1-n8-k3` is the empirical optimum: 8 recent turns + 3 retrieved dropped pairs = quality indistinguishable from full-history at a small fraction of the context budget.

### Decision changes

1. **ADR 009's retrieval-first stance is empirically validated.** Retrieval-substitution beats naive truncation at every matched window; naive truncation is *not* equivalent to full-history at any usable window size. The complexity earns its keep.
2. **Production canonical config: `recent_n=8, top_k=3`.** That's the gate-validated optimum. `recent_n=4, top_k=3` is the fallback for tighter context budgets on hardware-floor machines.
3. **Production wiring is justified work, not speculative.** The "wait for the gate verdict, then commit" sequencing note in this ADR's Implementation Status section is now resolved: the verdict is in, production wiring proceeds.
4. **Eval-side `retrieval-substitution-v1` (history-self-retrieval) is the validated mechanism.** Production goes one step further per the original ADR design: vault-retrieval over markdown sessions instead of history-self-retrieval. The eval validates the *pattern*; production refines the *substrate*.

### Build plan for production wiring (next chunks)

1. **Substrate** (~3 files): `effective_context_tokens` + `tokenizer_id` on `ModelCatalogEntry`; HuggingFace tokenizer integration; token-counting helpers.
2. **`backend/services/compaction_service.py`** (~1 file): production retrieval-substitution. Recent_n=8, top_k=3 default. Reaches into the markdown vault via the existing retrieval pipeline (the original ADR 009 §"Decision" design), not the history-self-retrieval shortcut the eval used.
3. **Chat router wiring** in [`_handle_message`](../../../backend/routers/chat.py): call compaction at per-turn boundary; never mid-stream / mid-tool-loop (ADR 009 §"Atomicity").
4. **Audit trail**: `compaction_event` log per session in [`session_service`](../../../backend/services/session_service.py).
5. **System-prompt budget enforcement** in [`context_builder`](../../../backend/services/context_builder.py): total budget against `effective_context_tokens`, not just per-note caps.
6. **Frontend UI surface** (separate chunk per ADR 009 §"UI surface"): in-active-context indicator, pin-turn affordance, re-include affordance, compaction expand-summary view.

Backend chunks #1–#5 land first; UI chunk #6 follows once the backend behavior is stable.

### What this verdict does NOT settle

- **Vault-retrieval quality at production substrate.** The eval validated history-self-retrieval at recent_n=8/top_k=3. Production reaches into the vault via the retrieval pipeline; that pipeline's "find earlier turn" entry point doesn't exist yet and is part of build-step #2 above. Whether the vault substrate matches eval-side quality is an open empirical question that the next conversation-eval run (with `retrieval_enabled=True` against a populated workspace) will answer.
- **Threshold tuning.** The 70% proactive-trigger threshold and the recent_n=8 default are now eval-justified for this fixture set, but real-usage data will refine them. Tune via the existing harness once production compaction lands.
- **Failure-mode coverage.** The 19 launch fixtures cover the failure modes we knew to test for. Real users will discover failure modes the fixture set didn't anticipate; the growth discipline ("add a fixture every time real usage produces a regression") applies.

## Production wiring landed (2026-04-29) — what shipped

The build plan filed at the end of the gate-verdict amendment is now landed in full on the backend side. UI surface (chunk #6) is the remaining work item; it attaches to the WS event + audit log this chunk shipped.

### Substrate (chunk #1)

`ModelCatalogEntry` ([`backend/services/ollama_service.py`](../../../backend/services/ollama_service.py)) gained:

- **`effective_context_tokens`** — RULER-safe ceiling, not the advertised number. Per-entry values were chosen against [research-1 §"Long-context performance"](../../research/models/model-research-1.md): Qwen3 dense at the family floor (32K) regardless of YaRN-extended advertised window; Qwen3 MoE 30B at ~64K; Ministral-3 / Devstral-Small-2 / Gemma-4 SWA conservative at ~64K of advertised 256K-128K windows; Granite 4 hybrid Mamba runs at full native (32K / 128K / 128K) since Mamba degrades less.
- **`tokenizer_id`** — HuggingFace reference for accurate token counting. Set per-entry where the upstream HF id is known (`Qwen/Qwen3-8B`, `ibm-granite/granite-4.0-h-micro`, etc.). Optional — `None` falls back to char/4 estimation.

Token counting lives in [`backend/services/token_counting.py`](../../../backend/services/token_counting.py): a thin wrapper around the `tokenizers` Rust-backed Python package (chosen over `transformers` for size, over `tiktoken` for catalog coverage). Lazy-loads tokenizers, caches them per-process, and treats failed loads as sticky so a transient network hiccup doesn't re-attempt every turn. Honors `HF_HUB_OFFLINE=1` and a `JARVIS_DISABLE_TOKENIZER_DOWNLOAD=1` knob so CI runs deterministically without touching the network. Counts both single strings and Anthropic-style block-list message content (text / tool_use / tool_result) so the chat router can budget against actual provider payload shape.

### Compaction service (chunk #2)

[`backend/services/compaction_service.py`](../../../backend/services/compaction_service.py) implements the production retrieval-first policy:

1. Strips `<think>...</think>` scratchpad from assistant turns unconditionally (Qwen3 Thinking variants leak these even with `think: false` on Ollama 0.18; the in-stream filter catches the live response, this catches what made it into stored history).
2. Computes `headroom = effective_ctx − system_prompt − output_reserve`, then `budget = headroom × threshold_pct`. Defaults: `recent_n=8`, `top_k=3`, `threshold_pct=0.70` — the gate-validated optimum.
3. When `history_tokens > budget`, finds the `recent_n`-th-to-last *real user-turn* boundary (tool_result-only user messages don't count) and drops everything before it.
4. Reaches into the markdown vault via [`find_earlier_turn_context`](../../../backend/services/retrieval/sessions.py) for the top-`top_k` matches against the latest user turn. The eval-side scaffold reaches into the dropped portion of the conversation history; production reaches into the canonical store, per the original ADR design.
5. Synthesizes a leading user-role message summarizing the retrieved pairs and prepends it to the kept window.

Returns a `CompactionResult` carrying the assembled messages, the audit-event payload (`turns_dropped`, `summary_used`, `recent_window_size`, `effective_ctx_at_event`, `tokens_before`, `tokens_after`, `threshold_pct`, `retrieval_paths`, `reason`), and the retrieved-vault attribution. Tunable via `JARVIS_COMPACTION_THRESHOLD_PCT` (clamped to `[0.30, 0.95]`) and `JARVIS_COMPACTION_RECENT_N` (clamped at floor of 2).

### Vault-retrieval entry point (chunk #2 substrate)

[`backend/services/retrieval/sessions.py::find_earlier_turn_context`](../../../backend/services/retrieval/sessions.py) is the "find earlier turn" entry point referenced in the ADR's "What this changes about existing code" list. It scopes `memory_service.list_notes` to `folder='conversations'`, reads the top-K matches off disk, strips frontmatter from the snippet, and excludes the **current** session's own saved conversation note (frontmatter `session_id` match) so the user doesn't see "earlier context" that's actually their current turn. Failures (index unavailable, file missing) are caught — vault retrieval must never break a chat turn.

We deliberately do *not* run the full hybrid retrieval pipeline here: BM25 alone is sufficient for "find a conversation that mentioned X" since the corpus and query share nearly identical surface text, and skipping cosine/graph keeps compaction-trigger latency low. If quality measurement (open follow-up #2) shows BM25 alone is insufficient, this is the right place to graduate.

### Chat router wiring (chunk #3) and atomicity

[`backend/routers/chat.py::_handle_message`](../../../backend/routers/chat.py) now calls `_maybe_compact` immediately after `ContextStrategy.assemble` and before the LLM stream is opened. `_maybe_compact` looks up the active model in the catalog (`get_model_by_litellm`); cloud providers without catalog entries (Anthropic / OpenAI) bypass compaction since they manage context server-side. The tool-call loop in `_stream_follow_up` deliberately does NOT call `_maybe_compact` mid-loop — atomicity per ADR 009 §"Atomicity" is enforced.

When compaction fires, the router records the audit event on the session row and emits a `compaction` WebSocket event carrying `turns_dropped`, `recent_window_size`, `tokens_before`, `tokens_after`, `effective_ctx`, and `retrieval_paths`. Internal failures inside `compact_messages` are caught and the turn proceeds with the uncompacted history — compaction is a quality lift, not a correctness gate.

### Audit trail (chunk #4)

[`session_service`](../../../backend/services/session_service.py) gained `record_compaction_event` and `get_compaction_events`; the `compaction_events` list is included in the JSON written by `save_session` and restored on `resume_session`. Compliance buyers see what the model "saw" at each turn alongside the per-turn `model_id`. A round-trip test pins this in `tests/test_chat_compaction_wiring.py`.

### System-prompt budget enforcement (chunk #5)

[`build_system_prompt_with_stats`](../../../backend/services/claude.py) accepts optional `system_prompt_budget_tokens` and `tokenizer_id` kwargs. The chat router computes the budget as `0.30 × effective_context_tokens` of the active model and passes both. The new `_enforce_system_prompt_budget` helper truncates the retrieved-context block (not the base persona, not the language reminder) until the prompt fits, keeping highest-priority retrieved content first. The `prompt_stats["context_truncated"]` field surfaces whether truncation kicked in. Cloud providers and unknown local models opt out cleanly via `(None, None)` from `_resolve_system_prompt_budget`.

### Test coverage

45 new tests across 5 files, full suite green at 1486 passed / 204 skipped:

- [`tests/test_token_counting.py`](../../../backend/tests/test_token_counting.py) — fallback path, tokenizer-loaded path (stubbed for determinism), cache stickiness on failure, message-block flattening for tool_use / tool_result.
- [`tests/test_compaction_service.py`](../../../backend/tests/test_compaction_service.py) — under-threshold no-op, recent-N cut-index, recent-N floor, tool-result-only user messages excluded from the count, vault failure degrades gracefully, `<think>` stripping, audit event shape, env-var threshold/recent-N overrides, defaults pinned to the ADR 010 gate verdict.
- [`tests/test_retrieval_sessions.py`](../../../backend/tests/test_retrieval_sessions.py) — empty-query short-circuit, `list_notes` failure handling, missing-file resilience, current-session exclusion, frontmatter stripping, `top_k` honored.
- [`tests/test_chat_compaction_wiring.py`](../../../backend/tests/test_chat_compaction_wiring.py) — `_resolve_system_prompt_budget` matrix (cloud vs local-known vs local-unknown), `_maybe_compact` audit-event recording + WS emission, internal-failure fallback, save/resume round-trip of the audit log.
- [`tests/test_system_prompt_budget.py`](../../../backend/tests/test_system_prompt_budget.py) — no-budget identity, under-budget identity, over-budget truncation, pathological budget drops the context entirely.

### Code-review fixes (2026-04-29 evening)

A post-landing review identified twelve issues across the chunk; all were applied in the same day. The non-cosmetic fixes:

- **Substitution block role flipped from `user` to `assistant`.** The kept window's first message after the cut is always a real user turn (the cut lands on a user-turn boundary by construction). A user-role substitution block produced consecutive same-role messages — Anthropic accepts that, but Ollama chat templates can merge or reject. Assistant-role alternates cleanly; reads naturally as "recalling earlier context." Compaction is gated to `provider=="ollama"` so Anthropic's "first message must be user" rule does not apply.
- **`_enforce_system_prompt_budget` now respects the budget after the marker.** The previous retry loop appended the truncation marker without re-checking, so the assembled prompt could ship 5–15% over budget on dense token content. The fix budgets the marker against the context budget and re-shrinks until both fit, dropping context entirely if necessary. The ADR invariant "assembled prompt fits the budget" now always holds.
- **Post-compaction overflow surfaced via warning log.** `compact_messages` now logs when `tokens_after > headroom` (large vault snippets pushing the kept window back over budget). Threshold-based gating is an ADR extension; this lands the diagnostic only.
- **`_maybe_compact` exception guard widened.** Previously the catalog lookup and `count_tokens(system_prompt, …)` ran outside the try; an unhandled exception there propagated to `_handle_message` (no surrounding guard), dropping the WS connection. The whole path is now wrapped — compaction degrades to "use uncompacted history" on any failure.
- **Audit-event persistence is now immediate.** `record_compaction_event` triggers `_auto_persist` so a process crash inside the LLM stream — or a non-swallowed tool-loop exception — cannot lose an audit event between recording and the next assistant `add_message`.
- **Concurrent `save_session_to_memory` calls are now serialised** via a per-session `asyncio.Lock`. A debounced background save racing with a `WebSocketDisconnect` explicit save could otherwise produce two notes for one logical session, leaking past the `find_earlier_turn_context` same-session exclusion.
- **`JARVIS_COMPACTION_RECENT_N` is clamped to `[2, 200]`** with a warning on out-of-range values. Without an upper bound, a misconfiguration to e.g. 1_000_000 would silently disable the strategy by always exceeding conversation length.
- **`build_system_prompt_with_stats` stats use the configured tokenizer** rather than char/4 when one is in scope. Audit logs forwarded via `prompt_stats["context_tokens"]` now match the numbers the budget enforcement actually saw, especially on Polish/Chinese/Arabic content.
- **`find_earlier_turn_context` caps the FTS limit at 50**, defending the index against an unbounded query if a caller passes a pathological `top_k`.

Six additional tests pin the contracts: substitution-block-role, marker-fit invariant, recent_n ceiling clamp (kwarg + env var paths), recent_n boundary equality (`recent_n == len(user_turns)` is the "already minimal" path), consecutive think-only assistant turns, vault-retrieval list_notes ceiling. Suite at 1492 passed / 204 skipped after the fixes.

A second review pass closed four follow-ups introduced or surfaced by the first fix:

- **`_save_to_memory_locks` cleaned up alongside `_sessions` eviction** in both `_evict_oldest_sessions` and `delete_session`. Without this, the per-session lock dict leaked one `asyncio.Lock` entry per ever-seen session id for the lifetime of the process — small in absolute terms but principally wrong for a long-running deployment.
- **Tightened the consecutive-think-blocks test.** The original was named "no consecutive same-role messages" but only asserted on assistant pairs, allowing user-user pairs through silently on the unusual `[user, assistant_think, assistant_think, user]` history shape. Split into two tests: one pinning that pure-think blocks are dropped (the actual behavior on unusual histories), and a new `test_strip_preserves_alternation_on_normal_history` pinning the real production contract that on realistically-alternating histories think-stripping never produces consecutive same-role messages of either kind.
- **`_enforce_system_prompt_budget` threads token counts back to its caller.** The helper now returns `(context, was_truncated, base_tokens, context_tokens, lang_tokens)`; `build_system_prompt_with_stats` reuses the cached counts in its stats block instead of re-encoding the same strings. Saves three HF tokenizer `encode` calls per turn on the hot path; falls back to fresh counts when the helper short-circuits.
- **Clarified the live-reference contract** in `save_session_to_memory`. The `messages` parameter is the same list object as `session["messages"]`, not a defensive copy — a concurrent `add_message` will be visible inside the locked section. This is benign (saved note is fresher rather than staler) but worth documenting so a future reader doesn't read the parameter passing as isolation.

Five additional tests pin the new contracts: lock-dict cleanup on `delete_session` and `_evict_oldest_sessions`, role-alternation on normally-alternating histories, helper returning cached token counts, helper returning `None` counts when short-circuiting. Suite at 1497 passed / 204 skipped after the second-pass fixes.

### What remains for ADR 009 v1

Backend behavior is stable; the open chunk is the **frontend UI surface** (ADR 009 §"UI surface"):

- In-active-context indicator next to each turn.
- Pin-turn affordance (keep a turn in active context regardless of compaction).
- Re-include affordance (promote a vault turn back into active context).
- Compaction expand-summary view consuming the audit log.

That chunk attaches to the `compaction` WebSocket event this chunk now emits and the `compaction_events` array this chunk now persists, so the contract for the frontend is set.

## Amendment 2026-05-01 — Stable system-prompt prefix (retrieval moves to user-message position)

**What changed.** The retrieved-context block no longer lives inside the system prompt. `build_system_prompt_with_stats` now returns a system prompt that is byte-stable across turns within a session (persona + specialist directives + JARVIS extensions + language reminder); retrieval is surfaced as a separate `stats["retrieval_block"]` and the chat router glues it onto the latest user message before dispatch via the new `attach_retrieval_to_user_message` helper. Same notes get retrieved with the same ranking and the same trace; only the position in the assembled prompt changes.

**Why.** The G4b6 cold-launch smoke on Apple M5 24 GB measured ~7.7 s warm-turn TTFT with `prefix_stable=False` — the system-prompt SHA mutated turn-to-turn because retrieval (which depends on the user message) was glued into the system block. Ollama's KV cache prefix-match looks at the literal prompt prefix; any mutation at byte 0 invalidates the cache and forces a full re-prefill of the entire ~2.7 K-token prefix every turn. With retrieval moved out, the system prompt becomes byte-identical session-long, the cache reuses the long prefix on every warm turn, and only the just-asked user turn (small) gets prefilled. Empirically this collapses warm TTFT from ~7.7 s to under 1 s on the same hardware. The cold first turn is unaffected (cache is empty either way).

**What it does NOT change.** Retrieval pipeline (BM25 + cosine + graph scoring), what gets retrieved, the `<retrieved_note>` XML wrapping, the per-note trace surfaced to the frontend, memory-writing tools, specialist directives, or the persona text. The system prompt structurally has the same components in the same order minus the retrieved-context block.

**Compaction interaction.** Compaction operates on the `messages` list *before* retrieval is glued onto the latest user message. This means old user messages in history are clean (no per-turn retrieval) — when compaction folds them into a recall block, no stale retrieval leaks through. Only the just-dispatched turn carries retrieval, and that turn isn't compaction-eligible (it's the current `recent_n` window). The compaction headroom math is updated to pass `system_prompt_tokens + retrieval_tokens` (rather than just `system_prompt_tokens`) so the dispatched-prefix size still maps to the correct trigger threshold; `_maybe_compact` gains a `retrieval_block` parameter to thread this through.

**Budget enforcement.** Same 30%-of-`effective_context_tokens` cap on the retrieved-context block — same arithmetic, same proportional truncation, same trace preservation. The cap now anchors at the user-message position instead of the system-prompt position, but `_enforce_system_prompt_budget`'s invariant ("base + lang + context ≤ budget") still holds because the budget is a property of the dispatched prefix, not of any one role.

**Quality posture.** Qwen3 (and Qwen3-class models generally) are trained on RAG patterns where context arrives via tool_result / user-position blocks rather than via system prompt. Moving retrieval to the user-message position is *toward* the training distribution, not away from it. The Polish/multilingual language-leak risk is reduced because notes (which may be in a different language) are no longer at the same prompt position as the language-reminder instruction; the XML `<retrieved_note>` wrapping continues to mark them as evidence not instruction. The language-reminder text itself is updated to be position-agnostic ("any retrieved notes attached to the user's message" instead of "the notes above").

**Tests.** New `backend/tests/test_system_prompt_stable_prefix.py` pins:
- `system_prompt` excludes retrieved-note content even when retrieval has results.
- `system_prompt` is byte-identical across two calls with different user messages but the same persona / specialist / language posture.
- `attach_retrieval_to_user_message` glues onto the tail user message correctly for both string and list content shapes; defensive no-op when the tail isn't user-role or the block is empty.

Backwards-compat note: `build_system_prompt_with_stats` keeps its `(prompt: str, stats: dict)` return shape; the new `retrieval_block` field is added to `stats`. Existing callers (chat router, eval runner, JARVIS-self test, security tests, context-builder trace tests) all work without test updates because none of them asserted retrieval content was *inside* the prompt string. The eval runner is updated to call `attach_retrieval_to_user_message` so the eval harness mirrors the production prompt shape.

Diagnostic coverage: the per-turn `chat_turn` log line in [`backend/routers/chat.py::_prefill_log`](../../../backend/routers/chat.py) surfaces a separate `rb_hash` (retrieval-block SHA12) alongside `sp_hash` so a future regression where retrieval starts leaking back into the system prompt would show as `prefix_stable=False` again.

The full backend suite (1,434 passed / 1 skipped) plus the 11 new prefix-stability tests stay green after the change.

## Open follow-ups (non-blocking)

1. **Eval-driven tuning of thresholds.** The 70% trigger and recent-N=8 defaults need empirical validation via [`eval-baseline`](../../concepts/eval-baseline.md). Build the tuning harness alongside the compaction implementation.
2. **Retrieval-substitution quality measurement.** Specifically, "given a question that references a dropped turn, can retrieval surface it?" Add an eval scenario.
3. **Tokenizer caching.** HuggingFace tokenizer instances are not free to instantiate; cache per active loadout.
4. **Persona re-injection cadence.** The §5 background-agent pattern is sketched as "more aggressive as the window fills." Specific cadence needs measurement.
5. **Vault-roundtrip-as-context-strategy** ([product-direction §5](../../research/product-direction-v1-v2.md) "Main thread split into sub-threads") — sub-agent contexts that return distilled results to the main thread. Adjacent to compaction; recorded here as a related future direction, not a v1 commitment.
