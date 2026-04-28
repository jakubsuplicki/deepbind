# ADR 008 — Conversation-pinned chat model; mid-session swap policy

**Status:** Accepted
**Date:** 2026-04-27 (initial), amended 2026-04-28 (Ollama thinking-toggle mechanism finding)
**Related:** [ADR 004](004-inference-router-architecture.md) · [ADR 009](009-context-overflow-compaction.md) · [`docs/research/models/model-research-4.md`](../../research/models/model-research-4.md) · [`docs/features/duel.md`](../../features/duel.md) · [`docs/features/sessions.md`](../../features/sessions.md)

## Context

[ADR 004](004-inference-router-architecture.md) commits to a multi-model dispatcher with dynamic load/unload. That raises a question the dispatcher's per-request shape does not answer: **what happens to an active conversation when the dispatcher would route the conversational slot to a different model mid-session?**

Re-prefill from the canonical conversation history mechanically works. Every inference engine handles "give me the full conversation, I'll prefill from scratch." That is true at the bytes level. It is also true that:

- Model B continues the conversation as model B would have continued it from the start, not as model A would have continued from where it was.
- Model B reads model A's prior turns as if model B had said them, and may double down on positions B would not have taken or contradict itself relative to B's own prior style.
- Even same-model re-prefill is non-deterministic at the token level due to floating-point trajectory differences. Cross-model is materially more drift.
- Industry practice is the evidence: ChatGPT, Claude.ai, Cursor, Continue lock the model per conversation. Mid-conversation cross-model swap is rare in shipped products precisely because it produces visibly different output from "same model continuing."

A user noticing "Jarvis got worse mid-sentence" or "Jarvis is now answering as if it forgot what we discussed" is the worst kind of credibility leak in a compliance-positioned product. A dispatcher that swaps invisibly under memory pressure trades a memory failure for a continuity failure that is harder to diagnose.

There are also classes of in-flight state that a swap would corrupt outright: an open WebSocket stream mid-response, a tool-call loop in progress, a `<think>` block being emitted, a duel-mode comparison running across multiple models on the same prompt.

A correction to [`model-research-4.md`](../../research/models/model-research-4.md) is also surfaced by this ADR. The research doc says reasoning mode "switched from same base; no separate weights load." This is correct for the original Qwen3-30B-A3B (May 2025), which is a hybrid model with `/think` and `/no_think` prompt directives on a single set of weights. It is **incorrect** for the Qwen3-30B-A3B-**Instruct-2507** and -**Thinking-2507** variants the research doc primarily recommends — those are separate fine-tunes with separate weights. Switching between them is a full model swap, not a free toggle. This ADR records the choice between the two and the consequences.

## Decision drivers

1. **Continuity in a single conversation must be felt as continuous.** A user's perception of "Jarvis just got dumber / different / repetitive" mid-conversation is the failure to avoid.
2. **Stateless slots should still route freely.** The dispatcher's value proposition (smart routing) is real for embeddings, plumbing, vision, code-pass, TTS. None of these share conversational attention state with chat. Pinning everything is the wrong overcorrection.
3. **In-flight state is sacrosanct.** Streaming responses, tool-call loops, and reasoning blocks must never be torn down mid-flight by a swap.
4. **OOM behavior must be explicit.** Silent swap on memory pressure is worse than an explicit "memory full, here are your options" banner. The product's runtime panel principle ([ADR 004](004-inference-router-architecture.md)) is to make state visible, not magic it away.
5. **Cross-base swap, when it happens, must be a user gesture.** Auto-swap mid-conversation is the wrong default. Manual swap is fine because the user accepts the cost in the same gesture.
6. **Per-turn auditability.** Every assistant turn must record which model wrote it. Required for audit, for redo, and for explaining swap-induced quality changes after the fact.

## Decision

### Pin the chat slot per conversation

The chat slot is **pinned for the conversation's lifetime**. The dispatcher selects a model when the conversation starts (taking memory pressure, profile, and active perf-mode into account); subsequent turns in the same conversation route to the pinned model.

A conversation gets a **loadout snapshot** at start: the set of slot-to-model bindings active for that conversation. New conversations consult the latest profile config; existing conversations keep their snapshot. Profile changes do not retroactively affect active conversations ([ADR 005](005-profile-driven-model-stacks.md)).

### Stateless slots route per request

Embeddings, plumbing classification, vision-on-PDF, code-execution-pass, TTS — these route freely. They share no attention state with chat. The dispatcher gains apply uncontested here.

### Swap is allowed only at safe boundaries

A "boundary" is any of:

- **Between user turns,** with no in-flight stream and no open tool-call loop.
- **On explicit user gesture** — a "switch model" UI affordance in the conversation header. Picking it makes the swap explicit. The UI surfaces "you're moving from A to B; B will re-read the conversation; tone and detail may shift." User accepts; new turns happen on B.
- **On forced OOM that cannot be otherwise resolved** — and only if a non-conflicting swap point is reachable. Otherwise the request fails cleanly with a banner (below).

**Within a turn, the model is locked.** Streams and tool loops are atomic. The router enforces this via a per-conversation in-flight lock acquired at request start, released when the turn (including any tool-loop recursion in [`chat.py:_stream_follow_up`](../../../backend/routers/chat.py#L191)) terminates.

### OOM is an explicit user choice, not a silent action

When memory pressure spikes during an active conversation and the dispatcher cannot route the next turn at the pinned chat slot:

- A banner surfaces with options: **"Free memory by closing other apps"**, **"End conversation and restart on smaller model"**, **"Override and continue with risk of slowdown"**.
- The runtime panel ([ADR 004](004-inference-router-architecture.md)) shows the underlying cause (free RAM, what's loaded, what would need to swap).
- No swap happens until the user picks an option.

This is the right shape because OOM is rare in well-sized profiles. Optimizing for the common case (no OOM) and making the rare case explicit beats designing primarily for the rare case and putting magic at the center.

### Tool-loop atomicity

The `_stream_follow_up` recursion at [`chat.py:191`](../../../backend/routers/chat.py#L191) does up to 5 rounds of tool use within one turn. The chat slot is locked from the first tool call until the loop terminates or aborts. A swap can never happen between rounds in the same turn.

### Stream atomicity

A swap can never happen mid-stream. The in-flight lock acquired at the start of `_handle_message` ([`chat.py:286`](../../../backend/routers/chat.py#L286)) is released only when the WebSocket emits `done` or `error`. Swaps queued during a stream wait for the stream to finish.

### Cross-base swap is a deliberate, visible action

When the user explicitly swaps mid-conversation:

1. The current loadout snapshot is updated.
2. The current conversation's pinned chat slot changes.
3. The next turn's prefill happens against the canonical conversation history, with the new model.
4. The UI surfaces a **conversation-level marker** at the swap point ("Switched to Qwen3-14B at 14:23; tone may shift"). The marker is part of the conversation's audit trail, not just chrome.
5. Per-turn `model_id` records continue against the new pinned model.

### Same-base mode toggle (if architecture allows)

The reasoning mode toggle (Instruct ↔ Thinking) depends on which Qwen3 variant the chat slot uses:

- **If the slot is the original Qwen3-30B-A3B (hybrid, May 2025):** Instruct ↔ Thinking is a runtime flag on the resident model. Free. Exposed as a per-conversation "thinking depth" toggle. The toggle mechanism is non-obvious; see "Thinking-toggle mechanism on Ollama" below for the load-bearing detail.
- **If the slot is the -2507 split (Qwen3-30B-A3B-Instruct-2507 + Qwen3-30B-A3B-Thinking-2507):** Thinking is a separate model. Engaging it is a swap, with all the swap caveats above. **Required to be a user gesture, not auto-route.**

The choice between hybrid and split is a real trade documented below (D12 trade); v1 adopts the **hybrid Qwen3-30B-A3B** for the chat + reasoning slot to enable free mode toggle. The -2507 variants are recorded as a future-quality upgrade path, conditional on storage budget and observed user demand for higher-tier reasoning.

### Thinking-toggle mechanism on Ollama (added 2026-04-28)

The hybrid Qwen3-30B-A3B documents two prompt directives, `/think` and `/no_think`, that should toggle thinking-mode on a per-message basis. **On Ollama 0.18 these directives are not honored when placed in the system or user message** — the model still decodes the full chain-of-thought, Ollama strips the opening `<think>` tag but leaves the prose plus the closing `</think>` in the response body. Putting `/no_think` in the system prompt did not change observed latency or token output in the conversation-replay eval harness.

The mechanism that **does** work on Ollama 0.18 is the top-level `think: false` boolean on the `/api/chat` request body:

```json
{
  "model": "qwen3:30b-a3b",
  "messages": [...],
  "think": false,
  "options": {"temperature": 0, "seed": 42}
}
```

Empirically the cost difference is roughly 20× on a "say hi" turn (≈10s with thinking on, ≈500ms with thinking off on Qwen3-14B). The eval harness adopted this as the default for ADR 010's gate runs because the production `/no_think` posture must actually be measurable; without it, every measurement was inflated by a thinking-decode pass the directive failed to suppress.

Production implication: when the chat slot toggles thinking depth per conversation (the hybrid's "free toggle" property), the toggle must be applied via the API parameter, not by mutating the system prompt with a directive. The `OllamaChat` adapter at [`chat_adapters.py`](../../../backend/tests/eval/conversations/chat_adapters.py) shows the working shape; production wiring should match.

A secondary finding worth recording: even with `think: false`, the Qwen3 model still emits chain-of-thought *content* (the model's reasoning prose), and Ollama 0.18 only strips the opening `<think>` tag — leaving the prose plus closing `</think>` in the response body. The eval-side adapter strips everything up to and including the last `</think>` before returning the response, on the grounds that production with a properly-configured `/no_think` would not surface the prose either. Production wiring should apply the same strip until upstream (Ollama or the model) handles the close tag cleanly.

(Cross-version note: this finding is specific to Ollama 0.18.0, which the project is pinned to because Ollama 0.21.x segfaults on Apple M5 + macOS 26 — see [ADR 010](010-conversation-replay-eval-harness.md). When upgrading past 0.21.x once that regression is fixed upstream, re-verify the toggle mechanism — Ollama may honor the prompt directive in a later version.)

### Per-turn `model_id` in session schema

Every assistant turn record in the session row gains a `model_id` field — the model that produced it. Required for:

- **Audit** — what model wrote what; subpoena-defensible chain of custody.
- **Redo on previous model** — UI affordance to re-run a turn on a previous model after a manual swap.
- **Compaction events** — see [ADR 009](009-context-overflow-compaction.md), where compaction events also write to the session row.

The schema delta is small but load-bearing across audit, swap, and compaction.

### Duel-mode interaction

[`duel`](../../features/duel.md) runs the same prompt across multiple models for comparison. **Duel pins all participating models for its duration.** If memory cannot hold them simultaneously, duel refuses to start with a clear "needs Tier B+ for this configuration" message. A mid-duel swap would invalidate the comparison.

### KV cache reuse across models is excluded

Some inference frameworks claim cross-model KV reuse. The product treats this as out of scope. Re-prefill from canonical history is correct; KV reuse across architectures is fragile and silently produces wrong-shaped attention.

## D12 trade — hybrid Qwen3-30B-A3B vs -2507 split

The decision between the two Qwen3-30B-A3B variants for the chat + reasoning slot, recorded inside this ADR because it interacts with the swap policy.

| Dimension | Original Qwen3-30B-A3B (May 2025) | Qwen3-30B-A3B-Instruct-2507 + -Thinking-2507 |
|---|---|---|
| Disk | ~17 GB (one model) | ~34 GB (two models) |
| Loaded RAM at any time | ~17 GB + KV | ~17 GB + KV (one resident; swap to engage other) |
| Mode switch | Prompt-directive on resident model — free | Full model swap — incurs swap cost |
| Benchmark scores | Lower (per-mode) | Higher (per-mode, specialized fine-tunes) |
| Continuity for reasoning toggle | Native — same model continues | Requires explicit user gesture per swap policy |
| Tier-A footprint | Comfortable | Tight; second model materializes as on-demand only |

**v1 picks the hybrid.** It is consistent with the conservative continuity policy in this ADR and saves disk on Tier A. The -2507 split is recorded as a future option:

- Profile may opt into -2507 if the user has Tier B+ and explicitly chooses higher reasoning quality at the cost of swap behavior.
- License (`feature_flags.split_reasoning_models`) can gate the option behind a SKU if pricing requires.

[`docs/research/models/model-research-4.md`](../../research/models/model-research-4.md) §"Why mixing models" and §"Final model lineup" need a correction note pointing here. The "free mode switch" claim in that doc applies only to the hybrid; it has been amended.

## Alternatives considered

### A. Turn-level auto-swap (the prior proposal)
The dispatcher swaps at every turn boundary based on current conditions. Maximum dispatcher utility. Maximum continuity damage — the user perceives a model that drifts in style and depth across the conversation. **Rejected.**

### B. Session-pinned (no swap until conversation ends, no manual switch)
More conservative than this ADR. The user can never change models mid-conversation, even deliberately. Eliminates the small "switch model" UX surface. Also eliminates a legitimate user need: trying a heavier model on a hard turn. **Rejected** — the pin should be the default, but user agency to break the pin is worth the small UX surface.

### C. Silent OOM-driven swap
Maintains "Jarvis just keeps working" magic at the cost of unexplained quality changes. Wrong shape for a compliance product where state must be inspectable. **Rejected.**

### D. Always re-prefill from scratch with no KV reuse, even within the same model
Over-correct. Same-model KV reuse within a conversation is the standard inference behavior and is correct. The point is not to forbid KV reuse; it is to forbid cross-model KV reuse and to require re-prefill on cross-model swap. **Not the policy here** — this ADR allows same-model KV reuse and re-prefills on swap.

## Consequences

### Positive
- Conversational continuity is felt as continuous within a conversation.
- Stateless slot routing remains free — the dispatcher's value proposition for embeddings / plumbing / vision / code / TTS is preserved.
- OOM behavior is honest — the user sees the cause and picks the response.
- Per-turn `model_id` makes the conversation auditable and supports redo affordances.
- The hybrid-vs-split trade is recorded and decidable; the v1 choice (hybrid) preserves continuity by default.

### Negative
- The dispatcher cannot relieve memory pressure invisibly mid-conversation. Some users will see OOM banners on undersized hardware. Mitigated by the profile-driven stack ([ADR 005](005-profile-driven-model-stacks.md)) sized to fit at install time, not runtime emergency.
- The hybrid choice for v1 means lower per-mode benchmarks vs the -2507 split. This is a material capability trade against continuity. Acceptable for v1; revisitable.
- The conversation loadout snapshot adds state to session rows.
- The duel feature gains a constraint (can't start if memory won't hold all participants); a real customer with Tier-A hardware may find some duels unrunnable.

### What this changes about existing code
- Session schema (sessions feature): assistant turn records gain `model_id`. Migration: existing turns get `model_id = null`; new turns populate.
- Session loadout snapshot: new field on the session row capturing slot-to-model bindings for the conversation. Populated at conversation start; immutable for the conversation's lifetime unless a manual swap rebinds.
- [`chat.py:_handle_message`](../../../backend/routers/chat.py#L286) acquires an in-flight lock per conversation; releases on `done` or `error`.
- [`chat.py:_stream_follow_up`](../../../backend/routers/chat.py#L191) the tool-loop recursion is part of the same lock — the lock is held across all 5 rounds.
- New "switch model" affordance in the chat header UI; conversation-level marker rendered at swap points.
- [`useChat.ts`](../../../frontend/app/composables/useChat.ts) tracks pinned-model state per conversation; surfaces swap UI when user requests.
- [`duel`](../../features/duel.md) feature gains a memory-feasibility precheck before starting.
- [`local-models`](../../features/local-models.md) feature: warm-up policy (`keep_alive`) becomes per-slot per [ADR 004](004-inference-router-architecture.md); the chat slot's `keep_alive` is bounded by conversation activity, not a fixed 30 minutes.

## Open follow-ups (non-blocking)

1. **Conversation loadout snapshot migration.** Existing sessions don't have one. New conversations populate; old conversations get a default-from-current-config or remain unannotated.
2. **"Redo this turn on previous model" UX.** Once per-turn `model_id` is recorded, the affordance becomes implementable. v1 records the data; the UX may land in v1.x.
3. **OOM-banner localization** — Polish, then English; copy needs to be specific to the actual cause ("free 4 GB to keep using Qwen3-30B" beats "memory full").
4. **Duel-feasibility precheck** needs the live-load probe ([ADR 004](004-inference-router-architecture.md)) to be honest. Don't ship duel before that.
5. **Research-4 correction.** [`docs/research/models/model-research-4.md`](../../research/models/model-research-4.md) §"Why mixing models" "no separate weights load" claim needs a correction note pointing here.
