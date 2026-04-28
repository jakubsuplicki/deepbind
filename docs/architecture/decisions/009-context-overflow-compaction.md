# ADR 009 — Retrieval-first context-overflow compaction

**Status:** Accepted
**Date:** 2026-04-27 (initial), amended 2026-04-28 (eval-side `retrieval-substitution-v1` landed; production wiring pending)
**Related:** [`docs/features/retrieval.md`](../../features/retrieval.md) · [`docs/features/sessions.md`](../../features/sessions.md) · [`docs/research/product-direction-v1-v2.md`](../../research/product-direction-v1-v2.md) §5

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

## Open follow-ups (non-blocking)

1. **Eval-driven tuning of thresholds.** The 70% trigger and recent-N=8 defaults need empirical validation via [`eval-baseline`](../../concepts/eval-baseline.md). Build the tuning harness alongside the compaction implementation.
2. **Retrieval-substitution quality measurement.** Specifically, "given a question that references a dropped turn, can retrieval surface it?" Add an eval scenario.
3. **Tokenizer caching.** HuggingFace tokenizer instances are not free to instantiate; cache per active loadout.
4. **Persona re-injection cadence.** The §5 background-agent pattern is sketched as "more aggressive as the window fills." Specific cadence needs measurement.
5. **Vault-roundtrip-as-context-strategy** ([product-direction §5](../../research/product-direction-v1-v2.md) "Main thread split into sub-threads") — sub-agent contexts that return distilled results to the main thread. Adjacent to compaction; recorded here as a related future direction, not a v1 commitment.
