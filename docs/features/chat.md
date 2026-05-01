---
title: Chat & LLM Integration
status: active
type: feature
sources:
  - backend/routers/chat.py
  - backend/services/chat/__init__.py
  - backend/services/chat/context_strategy.py
  - backend/services/ollama_dispatcher.py
  - backend/services/system_prompt.py
  - backend/services/compaction_service.py
  - backend/services/token_counting.py
  - backend/services/retrieval/sessions.py
  - backend/services/tools/__init__.py
  - backend/services/tools/definitions.py
  - backend/services/tools/executor.py
  - backend/services/web_search.py
  - backend/services/token_tracking.py
  - frontend/app/composables/useChat.ts
  - frontend/app/composables/useChatHealth.ts
  - frontend/app/composables/useWebSocket.ts
  - frontend/app/components/ChatPanel.vue
  - frontend/app/components/TraceList.vue
depends_on: [retrieval, sessions, specialists, preferences-settings, local-models]
last_updated: 2026-05-01
last_reviewed: 2026-05-01
---

# Chat & LLM Integration

## Summary

This is the core conversation loop of Jarvis: a WebSocket channel carries user messages to the backend, which retrieves relevant memory context, dispatches to the local Ollama runtime with streaming enabled, and forwards response tokens back to the browser in real time. Per [ADR 015](../architecture/decisions/015-single-target-local-only-stack.md), Ollama is the **only** dispatch target — no LiteLLM, no cloud SDKs, no provider abstraction. The model can invoke tools during a response — reading or writing notes, querying the graph, creating plans — with the tool call chain completing before the browser finalises the message.

## How It Works

### Connection lifecycle

The frontend opens a single persistent WebSocket to `GET /api/chat/ws` on page load. The backend immediately creates a session and sends `session_start` with a `session_id`. That ID travels with every subsequent message so the backend appends to the correct conversation history. If the connection drops, `useWebSocket` schedules an exponential backoff reconnect (1 s → 2 s → 4 s → … capped at 15 s) and sends a synthetic `disconnected` event so the chat UI can reset its loading state without waiting.

A heartbeat ping is sent from the frontend every 25 seconds to keep the connection alive through proxies. The backend silently ignores `ping` frames.

### Per-message flow

For each user message the backend (`_handle_message` in `chat.py`) runs the following sequence:

1. **Dispatcher construction** — The WS message carries `model` (the user-pinned chat model, e.g. `qwen3:8b`) and `base_url` (the Ollama endpoint — defaults to the loopback runtime spawned by the Tauri shell). `_make_llm` constructs an [`OllamaDispatcher`](../../backend/services/ollama_dispatcher.py); there is no provider branch and no API-key path because Ollama runs in-process on the user's machine.
2. **Context assembly via `ContextStrategy`** — Session history is fetched via `session_service.get_messages` and passed through the active `ContextStrategy` (default: `FullHistoryStrategy`, which is identity). The strategy is the swap point through which compaction and retrieval-substitution alternatives plug in (ADR 010). Production today behaves exactly as before; the eval harness injects alternative strategies via the `context_strategy` parameter on `_handle_message`.
3. **Context retrieval — retrieval lives in the user-message position** (per ADR 009 amendment 2026-05-01). `build_system_prompt_with_stats` calls `build_context` (from `context_builder`) to find relevant notes and returns the resulting block as `stats["retrieval_block"]` — a separate string, not embedded in the system prompt. The chat router glues it onto the just-sent user message via `attach_retrieval_to_user_message` before dispatch. The system prompt itself stays byte-identical session-long (persona + specialist directives + JARVIS extensions + language reminder), so Ollama's KV cache prefix-match reuses the long stable prefix on warm follow-up turns. Empirically this collapses warm TTFT from ~7.7 s to under 1 s on M5 24 GB. If a specialist is active, its prompt fragment is injected into the (still-stable) system prompt first.
4. **Tool filtering** — `specialist_service.filter_tools` narrows the full `TOOLS` list to those permitted by the active specialist (or all tools if none is active).
5. **First LLM stream** — `OllamaDispatcher.stream_response` opens an `ollama.AsyncClient.chat(stream=True)` request. The adapter maps Ollama chunks to `StreamEvent` shapes (`text_delta`, `tool_use`, `done`, `error`) so the chat router and WS event consumers see the same surface they did before ADR 015. The request always sets `think: False` — without this, qwen3-family models emit a `<think>…</think>` block before the actual answer, the response surfaces as `message.thinking` tokens with empty `message.content` for several seconds per turn (5–10 s of dead air visible to the user). Models that don't natively support `think` (gemma4, ministral, devstral, gpt-oss in our catalog) silently ignore the flag — no harm. Models that *should* honor it but don't (the chain-of-thought leak class) are caught by the chat-model-probe and not auto-recommended on first run.
6. **Tool call handling** — If the model emits a tool call, the dispatcher synthesizes a stable id (Ollama's wire format omits ids), accumulates the JSON `function.arguments`, and yields a `tool_use` `StreamEvent` once complete. The router executes the tool, appends both the assistant tool-use block and the tool result to the message list, and calls `_stream_follow_up` to get the LLM's next response. The Anthropic-style tool_result block is converted to Ollama's `{role: "tool", tool_name, content}` shape at the dispatcher boundary.
7. **Recursive tool rounds** — `_stream_follow_up` is recursive. Each recursive call increments a `depth` counter. The chain is cut off at `MAX_TOOL_ROUNDS = 5` to prevent runaway loops. Within the loop, `_compact_stale_tool_results` collapses older tool_result payloads to a short reminder (the model has already read them) — this is an in-loop optimization separate from the `ContextStrategy` swap point above.
8. **Session save** — `add_message` auto-persists after each append (crash protection). On WebSocket disconnect the session is additionally written to `memory/` as a Markdown note.
9. **Token logging** — Per-turn `input_tokens` + `output_tokens` are written to `app/logs/token_usage.jsonl` via `log_usage`. Counts come from Ollama's authoritative `prompt_eval_count` / `eval_count` fields on the final chunk; `tiktoken` is retained only for prompt-budget predictions during compaction. `cost_estimate` is invariantly `0.0` (the local model is free at inference time).

### ContextStrategy — the compaction swap point

`backend/services/chat/context_strategy.py` defines the `ContextStrategy` Protocol: a small `assemble(messages) -> messages` interface that decides which session-history messages get sent to the model on each turn. The production default is `FullHistoryStrategy` — a literal identity over the input — so the abstraction landed behavior-preserving when introduced (ADR 010, 2026-04-27). All future compaction policy (naive recent-N truncation, retrieval-first substitution per ADR 009) attaches at this point. The eval-side runner injects alternative strategies via the `context_strategy` parameter on `_handle_message` to compare them against `full-history` on frozen conversation fixtures.

Why an abstraction when only the identity strategy ships today: ADR 010's gate decision compares the production strategy against alternatives, and that comparison cannot run if the production path has no swap point. The Protocol is the smallest possible swap point that makes the comparison legal.

A `TypeError` guard at the strategy boundary in `_handle_message` rejects strategies that return non-list values, so a buggy custom strategy fails with a clear message at the boundary instead of a confusing provider-level error deep in the LLM stack. The in-loop tool-result compaction (`_compact_stale_tool_results`) is intentionally outside the strategy's scope — it operates on tool_use / tool_result payload size during a multi-round tool-loop, not on conversation-history selection. This boundary is documented in `_stream_follow_up`'s docstring; future strategies that want to compact mid-tool-loop must extend the contract explicitly to avoid breaking tool_use / tool_result pairing.

### Conversation-replay eval harness

The conversation-replay eval harness lives at [`backend/tests/eval/conversations/`](../../backend/tests/eval/conversations/) and consumes the hand-authored fixtures at [`backend/tests/eval/conversations/fixtures/`](../../backend/tests/eval/conversations/fixtures/). Two production-relevant pieces:

- **[`scorer.py`](../../backend/tests/eval/conversations/scorer.py)** — pure mechanical scoring against `expected_facts` (regex / fuzzy match) and `must_not_contain` guards. Each turn is classified into one of four severity buckets — `clean_pass`, `partial`, `no_answer`, `confabulation` — instead of a flat pass/fail boolean. Confabulation (any guard triggered) is the most severe and dominates regardless of fact outcomes; it's the failure mode compaction strategies must avoid above all others. The fuzzy match uses a sliding window over `difflib.SequenceMatcher` so paraphrase-correct answers don't fail on punctuation drift. `score.passed` remains as a convenience property mapped to `severity == CLEAN_PASS`.
- **[`runner.py`](../../backend/tests/eval/conversations/runner.py)** — replays a fixture under a given `ContextStrategy` and chat callable. The `ChatCallable` Protocol decouples the runner from any specific provider (Anthropic, Ollama, stub-for-tests). Scripted turns are replayed verbatim into the running history, with tool_use / tool_result blocks paired by id to satisfy the Anthropic protocol. At each `assistant_target` turn the strategy assembles context from history, the chat callable produces a response, and the scorer records the result.
  - **Multi-seed support** via `run_fixture_multi_seed(fixture, *, strategy, chat_factory, seeds)`. The factory produces a fresh `ChatCallable` per seed; each seed's run produces its own `TurnResult`s, all aggregated into a single `MultiSeedFixtureResult` with `clean_pass_rate`, `severity_distribution`, and `stdev_clean_pass_rate` computed across the full set. Per-seed pass rates are exposed via `per_seed_clean_pass_rates()` for the bootstrap-CI gate logic.
  - **Production-retrieval wiring** via `retrieval_enabled=True` plus `workspace_path=...`. When set, the runner mirrors production by calling `services.claude.build_system_prompt_with_stats` against the most recent user message at each `assistant_target` turn — so the augmented system prompt the model sees matches the shipped chat path. Default is off so existing tests stay deterministic without a workspace bootstrap; the eval CLI flips it on for canonical baseline runs.
  - Mechanical-only at v1 — judge protocol layers on top later (ADR 010 step 9+, conditional on the gate decision).
- **[`strategies.py`](../../backend/tests/eval/conversations/strategies.py)** — eval-only `ContextStrategy` implementations. Two are exposed today: `NaiveTruncateStrategy(recent_n=N)` keeps only the last N user turns plus everything after — "user turns" explicitly excludes tool_result wrappers; cuts always fall at user-turn boundaries so tool pairs within the kept suffix remain intact. Per ADR 010 the eval sweeps `recent_n` across multiple values (4, 8, 12, 16) so retrieval-substitution must beat the *best* simple alternative, not the first value tested. `RetrievalSubstitutionV1Strategy(recent_n=N, top_k=K)` is ADR 009's retrieval-first stance: same truncation as naive at recent-N, then before returning, score each *dropped* user turn by lower-cased content-word overlap with the latest user turn and prepend a synthesized retrieval block containing the top-K dropped (user, assistant) pairs in chronological order. The block uses the user role rather than system so every chat adapter accepts it without provider-specific handling. Tokenization filters a small embedded English+Polish stop-word list (no external NLP dependency) so retrieval is fully deterministic across runs. The gate at matched N — `naive-truncate-N vs retrieval-substitution-v1-nN-kK` — is what determines whether retrieval earns its complexity over the cheap baseline.
- **[`chat_adapters.py`](../../backend/tests/eval/conversations/chat_adapters.py)** — concrete `ChatCallable` implementations. **`OllamaChat` is the default** (talks to `http://127.0.0.1:11434`, model `qwen3:30b-a3b` — the v1 canonical chat model, temperature 0, fixed seed) so the eval measures the production stack honestly. **`AnthropicChat` is opt-in** (hosted Claude, faster iteration but different model family — must not be promoted to canonical baseline). The `make_chat(provider=...)` factory picks between them; `make_chat_factory(provider=...)` returns a `seed → ChatCallable` factory used by the multi-seed runner. Anthropic-style content blocks (text / tool_use / tool_result) are translated to each provider's native message shape at the adapter boundary, including the tool_use → tool_calls and tool_result → tool-role-message conversions for Ollama. **Chain-of-thought stripping** (`_strip_thinking`) runs on every Ollama response: Ollama 0.18 with `think: false` strips the opening `<think>` tag but leaves the model's reasoning prose plus `</think>` in the body, so the adapter discards everything up to and including the last `</think>` before returning. Without this, `must_not_contain` guards regex over the chain-of-thought and trip on rejected-candidate names that the model only mentioned in reasoning — false-positive confabulation rates as high as 30+ pp on guard-heavy fixtures.
- **[`rescore.py`](../../backend/tests/eval/conversations/rescore.py)** — re-score a captured baseline JSON without re-running the model. When the scorer or the thinking-strip changes, an existing run can be re-interpreted in <1 s rather than re-spending the 45 min – 2 h a 30B grid takes. Loads the baseline, strips chain-of-thought from each `response_text`, re-runs `score_turn` against the matching fixture turn, regenerates aggregations and gate decisions, and writes a new baseline tagged with `rescored_from_timestamp_utc` so artifact lineage is unambiguous. CLI: `python -m tests.eval.conversations.rescore <baseline.json> [--out <path>]`.
- **[`gate.py`](../../backend/tests/eval/conversations/gate.py)** — bootstrap-CI strategy comparison. Replaces the original fixed-threshold gate language ("if naive scores within 5 points of full-history…") with a 95% confidence interval on the difference between two strategies' per-fixture clean-pass rates. The bootstrap is paired by fixture (preserving fixture-level variance) and seeded for determinism. The verdict is one of `improvement` / `regression` / `equivalent` / `insufficient_data` based on whether the CI excludes zero. The `adr_009_gate(full_history, naive_truncate, retrieval_substitution=None)` convenience runs ADR 010's two named questions in one call: (1) does naive truncation match full-history? (2) does retrieval-substitution beat naive? Each returns its own `GateDecision`.
- **[`run_eval.py`](../../backend/tests/eval/conversations/run_eval.py)** — the CLI. `python -m tests.eval.conversations.run_eval --strategies full-history,naive-truncate-4,naive-truncate-8 --provider ollama --seeds 1,2,3` orchestrates every (strategy × fixture × seed) combination, computes the gate decisions, and writes a stable-key JSON baseline file. Default strategies sweep multiple naive-truncate values so retrieval-substitution must beat the *best* simple alternative, not the first value tested. Default provider is Ollama; `--provider anthropic` is the opt-in iteration loop. `--retrieval` enables the production-retrieval wiring; `--workspace-path` pins the retrieval workspace for reproducibility.

Neither file is in the production path; the runner is dev infra that the engineer invokes locally before merging compaction-affecting changes.

### Production-side context compaction (ADR 009)

Long-running conversations eventually exceed the chat model's safe context window. ADR 009's retrieval-first compaction now runs in production at the per-turn boundary. The eval gate verdict (run-20260428T112547Z) validated `recent_n=8, top_k=3, threshold=0.70` as the canonical configuration: retrieval-substitution at those values matches full-history quality (Δ ≈ 0 pp) at a small fraction of the context budget, while naive truncation regresses against full-history at every aggressive window size (-26 pp at N=8, -53 pp at N=4).

How a turn is compacted:

1. **Strategy assembly first.** `ContextStrategy.assemble` runs as before. Compaction is a *separate* step that runs after the strategy returns and before dispatch, so any future strategy that wants to compact in its own way is independent of (and composes with) the production compaction policy.
2. **Active-model lookup.** `_resolve_system_prompt_budget` and `_maybe_compact` both look up the active local model in `MODEL_CATALOG` via `get_model_by_litellm` (the helper retains its name for backwards-compat; the lookup keys are still in the form `ollama_chat/<tag>`).
3. **System-prompt budget enforcement.** `build_system_prompt_with_stats` now accepts `system_prompt_budget_tokens` and `tokenizer_id`. The chat router computes the budget as `0.30 × effective_context_tokens` of the active model. When the assembled prompt exceeds budget, `_enforce_system_prompt_budget` truncates the retrieved-context block (not the base persona, not the language reminder) until it fits, keeping highest-priority retrieved content first. The `prompt_stats["context_truncated"]` field surfaces whether truncation kicked in.
4. **Per-turn compaction.** `compact_messages` in `backend/services/compaction_service.py` is called with `effective_context_tokens`, `tokenizer_id`, and the system-prompt token count. It strips `<think>...</think>` scratchpad blocks unconditionally, then checks if the projected token total exceeds the proactive trigger (default 70%). When the trigger fires, it cuts at the `recent_n`-th-to-last *real* user-turn boundary (tool_result-only user messages don't count), reaches into the markdown vault via `find_earlier_turn_context` for the top `top_k` matches against the latest user turn, and prepends a synthesized user-role substitution block.
5. **Atomicity (ADR 009 §"Atomicity").** Compaction runs **only** at the per-turn boundary in `_handle_message`, never inside the tool-call loop in `_stream_follow_up`. Mid-loop compaction would risk re-assembling between a `tool_use` block and its matching `tool_result`, which the provider rejects. The mid-loop `_compact_stale_tool_results` handles the only safe in-loop compaction (collapsing prior tool_result payloads).
6. **Audit trail (ADR 009 §"Audit trail").** Every compaction event is recorded on the session row via `session_service.record_compaction_event` and persisted into the saved session JSON. The event captures `{timestamp, turns_dropped, summary_used, recent_window_size, effective_ctx_at_event, tokens_before, tokens_after, threshold_pct, retrieval_paths, reason}`. Compliance buyers can see exactly what the model "saw" at each turn alongside the per-turn `model_id`.
7. **WS surfacing.** When compaction fires the router emits a `compaction` event over the WebSocket carrying the same numbers as the audit event. The frontend in-active-context indicator and pin-turn affordances (separate ADR 009 §"UI surface" chunk) attach here.

Token counting uses the HuggingFace `tokenizers` package via `services.token_counting`, which lazily loads the per-model tokenizer (`Qwen/Qwen3-8B`, `ibm-granite/granite-4.0-h-micro`, etc.) and caches it for the process. When the tokenizer is unavailable (offline, gated, missing), the wrapper falls back to a `chars / 4` estimator — same approximation the codebase used pre-ADR-009. Token counts are accurate to the active model on Polish/Chinese/Japanese/Arabic content, where the char/4 approximation drifts 20–40%.

Each catalog entry in `ollama_service.py::ModelCatalogEntry` now carries `effective_context_tokens` (RULER-safe ceiling, not the advertised number — typically half the native window for dense transformers, full native for hybrid Mamba) and `tokenizer_id` (the HF reference for accurate counting). The system-prompt budget and the compaction trigger both reference these fields.

Thresholds and recent-N defaults are tunable per-deployment via the env vars `JARVIS_COMPACTION_THRESHOLD_PCT` (clamped to `[0.30, 0.95]`) and `JARVIS_COMPACTION_RECENT_N` (clamped at the floor of 2). Hardware-floor profiles can lower the recent window to fit tighter context budgets; the default 8 is the empirical optimum from ADR 010's gate.

### Latency benchmark harness

A sibling harness at [`backend/tests/eval/latency/`](../../backend/tests/eval/latency/) measures user-perceived chat latency (TTFT, decode tokens/sec, end-to-end wall clock) on the actual ICP hardware. Same discipline as the conversation harness — committed JSON baselines, opt-in pre-merge gate, bootstrap-CI verdict — applied to the speed axis instead of answer-quality. Runs the streaming Ollama HTTP `/api/chat` endpoint with `temperature: 0` and a fixed seed, captures TTFT from the first non-empty content delta, and reads `eval_count / eval_duration` from the `done` event for honest decode throughput. One Anthropic Claude Sonnet reference scenario provides the explicit "are we faster than the cloud option?" benchmark; skips silently when no API key. CLI: `python -m tests.eval.latency.run_bench`. See [`docs/concepts/latency-baseline.md`](../concepts/latency-baseline.md) and [ADR 011](../architecture/decisions/011-latency-benchmark-harness.md).

### Per-turn telemetry & health watcher (ADR 005 §C trigger 2)

Every chat turn carries Ollama's authoritative per-stage timings from the `done` event through to the frontend, where they drive two surfaces: a per-turn readout next to each assistant message, and a health watcher that compares observed decode speed against the install-time probe baseline.

**Backend wire path.** [`OllamaDispatcher`](../../backend/services/ollama_dispatcher.py) captures `eval_duration`, `prompt_eval_duration`, `load_duration`, and `total_duration` (all nanoseconds) on the final Ollama chunk and forwards them on the `usage` `StreamEvent`. The chat router (`_handle_message` in [`backend/routers/chat.py`](../../backend/routers/chat.py)) folds them into a per-turn accumulator: decode and prefill durations sum across all rounds (a tool-using turn has multiple); TTFT is captured from the first round's load + prefill (the user's *felt* initial latency, not the per-round average). On the `done` WS event the accumulator renders into a `metrics` payload — `{decode_tps, prefill_tps, ttft_ms, load_ms, total_ms, eval_count, prompt_eval_count}` — with `decode_tps` and `prefill_tps` precomputed server-side so the frontend never has to repeat the math. Turns that report no timings (older Ollama versions, error states) omit the field entirely; consumers treat absent `metrics` as "no telemetry this turn" and degrade silently.

**First-turn bootstrap surface.** [`ChatFirstTurnWarmup.vue`](../../frontend/app/components/ChatFirstTurnWarmup.vue) replaces the generic typing-dots indicator for the very first turn of a fresh session — the one turn where the wait is genuinely long (~16 s on M5 24 GB) because Ollama is paying the cold model-load + first-re-prefill cost. Subsequent turns reuse the KV-cache prefix (per ADR 009 amendment 2026-05-01) and land in 1-2 s, so they keep the existing low-drama typing-dots indicator. The warmup surface is monospace-only with hairline borders and a single phosphor-amber accent on the active stage; four time-driven stages (`load model` → `read notes` → `compose first` → `stream`) tick through against a calibrated 15 s estimate so the user's mental model tracks reality without the component pretending to drive real telemetry. Gated strictly on "no prior assistant message exists in this session" — resumed sessions and follow-up turns never see it.

**Frontend telemetry surface.** [`useChat`](../../frontend/app/composables/useChat.ts) attaches the `metrics` payload to the just-completed `ChatMessage`. [`ChatPanel.vue`](../../frontend/app/components/ChatPanel.vue) renders a compact mono pill in the existing meta row — `12.4 t/s · 0.85s` — with the full breakdown (decode/prefill TPS, load_ms when cold, total wall clock, both token counts) in the native `title` tooltip so the row stays uncluttered. The pill is tinted by current health status: cyan-tinted neutral when healthy or unknown, amber when the watcher classifies the latest sample as `slow` for that model, soft green when it's `fast`. Status leaks honestly into historical bubbles — a turn that *was* slow keeps reading slow even after the model recovers, which is the truthful reading.

**Health watcher (`useChatHealth`).** A new composable at [`frontend/app/composables/useChatHealth.ts`](../../frontend/app/composables/useChatHealth.ts) loads per-machine baselines from `GET /api/local/chat-model-probe` (the persisted [ADR 012](../architecture/decisions/012-chat-model-self-test.md) probe record's `realistic_tps` per candidate model — already captured during install, regenerable via "Re-run probe"). Each turn's observed `decode_tps` is appended to a per-model rolling window of 5 samples. When the *full* window stays below 50% of baseline, a single soft-hint snackbar surfaces (cooldown 10 min/model): *"{model} is running at ~{n}% of expected speed. Try a smaller model or close other apps."* The action button lands the user at `/settings#local-models` for an in-place re-test. When the full window stays above 105% of baseline AND the user has a heavier installed catalog rung that *also* has a passing probe baseline, a complementary upgrade hint surfaces (cooldown 24h/model): *"You may have headroom for {heavier rung}."* A single turn that swings high or low is noise — only the full window fires. ChatPanel additionally renders an inline "Re-test models" advisory banner for the duration of any sustained-slow window.

**Strict non-goals.** The watcher never gates dispatch, never auto-swaps models, never refuses a turn. It is purely advisory. Actual memory exhaustion is caught downstream by the OOM-retry-walk-the-ladder loop ([ADR 005 §C trigger 1](../architecture/decisions/005-hardware-tiered-model-stack-and-first-run-policy.md#c-downgrade-ladder)). This replaces the disabled pre-flight RAM check ([ADR 005 §C trigger 2 amendment, 2026-05-01](../architecture/decisions/005-hardware-tiered-model-stack-and-first-run-policy.md#c-downgrade-ladder)) — instead of *predicting* whether a model will run via `psutil.available × 0.8 ≥ footprint` (a prediction that fights macOS unified memory), we *observe* what it actually does and surface the data to the user.

**Prefill-cost diagnostic.** [`_prefill_log` in `chat.py`](../../backend/routers/chat.py) emits one structured INFO line per chat turn:
```
chat_turn session=… turn=N sp_hash=<sha12> prefix_stable=True/False rb_hash=<sha12>
  sp_total_tok=… sp_ctx_tok=… ctx_truncated=…
  prefill_count=… prefill_ms=… ttft_ms=… load_ms=… decode_tps=… prefill_tps=…
  tool_calls=… tool_rounds=… model=…
```
Originally added to confirm the per-turn TTFT regression; kept as ongoing diagnostic now that ADR 009 amendment 2026-05-01 (stable-prefix shift) is in place. The `sp_hash` should hold turn-to-turn within a session (`prefix_stable=True`), proving the system prompt is byte-identical and Ollama's KV cache is reusing the prefix. The `rb_hash` is the SHA of the retrieval block attached to the latest user message — it *will* mutate per turn (different user message → different retrieval) and that's the design, not a regression. If `prefix_stable=False` reappears in a future turn, retrieval has leaked back into the system prompt and the warm-turn TTFT regression is back. Per-session state (turn counter + last system-prompt SHA12) is capped at 256 sessions FIFO so it can't grow unbounded.

**Where the logs land in the bundled app.** The Tauri shell drains the sidecar's stdout/stderr only until the READY handshake (`desktop/src-tauri/src/lib.rs::await_sidecar_ready`), so anything `logger.info(...)` prints after that goes into a dropped channel. To make the diagnostic actually surface in production, [`backend/scripts/run_frozen.py::_setup_logging`](../../backend/scripts/run_frozen.py) installs a `RotatingFileHandler(5 MB × 3)` on the root logger at INFO and writes to platform-conventional log paths — `~/Library/Logs/DeepFilesAI/sidecar.log` on macOS, `%LOCALAPPDATA%\DeepFilesAI\Logs\sidecar.log` on Windows, `${XDG_STATE_HOME:-~/.local/state}/DeepFilesAI/logs/sidecar.log` elsewhere. Operator workflow: launch the bundled .app, run a few chat turns, then `tail -f ~/Library/Logs/DeepFilesAI/sidecar.log | grep chat_turn`.

### Canonical chat model — Qwen3-14B (until per-machine probe lands)

The eval-pinned canonical chat model is currently `qwen3:14b`. This was changed from `qwen3:30b-a3b` on 2026-04-28 after the latency benchmark surfaced a chain-of-thought leak on Ollama 0.18.0 — the 30B-A3B emits internal monologue despite `think: false`, producing 8× worse perceived latency on realistic chat. See [ADR 010 Issue 4](../architecture/decisions/010-conversation-replay-eval-harness.md#issue-4-2026-04-28-evening--qwen3-30b-a3b-thinkfalse-leak-canonical-chat-model-swap-to-qwen3-14b) for the full reproduction.

The "single hard-coded canonical model" choice is the wrong shape long-term — different customer environments have different `think: false` behavior, hardware tiers, and RAM budgets. [ADR 012](../architecture/decisions/012-chat-model-self-test.md) files the install-time self-test that replaces this static choice with a per-machine probe-driven pick. Implementation: [`backend/services/chat_model_probe.py`](../../backend/services/chat_model_probe.py).

### Error handling in OllamaDispatcher

`ollama.RequestError`, `ollama.ResponseError`, `httpx.TimeoutException`, and `httpx.ConnectError` are all caught at the adapter boundary and converted to `StreamEvent(type="error", content=…)` with user-readable messages. The OOM-retry loop in `_handle_message` walks one further rung of the downgrade ladder when an OOM-shaped error fires before any text streamed (see [docs/features/local-models.md](local-models.md#memory-pressure-monitor--downgrade-ladder-runtime-adr-005-c)). The frontend detects whether an error message is retryable (matches "try again", "out of memory", "reconnect") and shows a Retry button if so.

### Frontend state

`useChat` owns all conversation state: the message list, the streaming `currentResponse` buffer, loading/error flags, and `toolActivity` (a label like "Searching notes…" shown while a tool runs). It delegates connection management entirely to `useWebSocket`. When a `done` event arrives, `currentResponse` is flushed into the `messages` array and loading state clears.

`ChatPanel.vue` renders messages with `marked` + `DOMPurify` for safe Markdown output. The streaming response is rendered live with a blinking cursor appended. A URL detection feature watches the input field: if a URL is typed, a toolbar appears offering to save it to memory via `ingestUrl` (bypassing the chat dispatch — this is a direct REST call to the ingest endpoint).

### Token budget constants

`token_tracking.py` defines soft budget caps intended for use in context assembly:

| Constant | Value |
|---|---|
| `TOTAL_BUDGET` | 4000 tokens |
| `CONTEXT_BUDGET` | 2500 tokens |
| `PREFERENCES_BUDGET` | 500 tokens |
| `SPECIALIST_BUDGET` | 500 tokens |
| `HISTORY_BUDGET` | 500 tokens |

`check_budget()` is called in `chat.py` before each dispatch. When the accumulated token count exceeds `TOTAL_BUDGET`, the call is blocked and an error event is sent to the client. When the count exceeds the warning threshold, a notification event is sent but the call proceeds. `cost_estimate` on every entry is `0.0` — local inference has no per-token cost — but the per-conversation token total is still useful for prompt-budgeting and for surfacing "this conversation is getting expensive in context" warnings.

## Key Files

- `backend/routers/chat.py` — WebSocket endpoint, per-message orchestration, single-target dispatch (`_make_llm` constructs the [`OllamaDispatcher`](../../backend/services/ollama_dispatcher.py)), recursive tool chain loop, OOM-retry ladder integration, session save-on-disconnect.
- `backend/services/ollama_dispatcher.py` — Streaming adapter from `ollama.AsyncClient.chat(stream=True)` events to the existing `StreamEvent` shape; tool-call id synthesis; Anthropic ↔ Ollama message converter; error mapping. ADR 015 §B.
- `backend/services/system_prompt.py` — `StreamEvent` dataclass; `build_system_prompt` / `build_system_prompt_with_stats` for context injection; `_SYSTEM_PROMPT_BUDGET_FRACTION` and `_enforce_system_prompt_budget` for budget-aware truncation. Rescued from the deleted `services/claude.py` per ADR 015 chunk 4.
- `backend/services/tools/definitions.py` — Tool input schemas (Anthropic-style; the dispatcher remaps them to Ollama's `function`-shaped tool spec at the boundary).
- `backend/services/tools/executor.py` — `execute_tool` dispatcher: maps tool names to underlying service calls (memory CRUD, graph queries, web search, Jira). All tool errors are converted to user-readable strings here.
- `backend/services/tools/__init__.py` — Re-exports `TOOLS` and `execute_tool` so callers can keep using `from services.tools import …` after the package split.
- `backend/services/web_search.py` — Thin DuckDuckGo wrapper. The offline-mode entitlement check ([`services/privacy.py`](../../backend/services/privacy.py)) gates outbound network use; when blocked, returns `[{"error": "<reason>"}]`.
- `backend/services/token_tracking.py` — Append-only JSONL usage log with per-day and all-time aggregation helpers; records `provider="ollama"` and `model` per entry. `cost_estimate` is invariantly `0.0` — there is no inference cost on local models.
- `frontend/app/composables/useWebSocket.ts` — Persistent WebSocket with heartbeat, exponential-backoff reconnect, and multi-listener message dispatch
- `frontend/app/composables/useChat.ts` — Conversation state manager; maps raw WebSocket events to UI state; handles retry logic
- `frontend/app/components/ChatPanel.vue` — Message list, streaming cursor, typing indicator, tool activity label, error bar with retry, URL ingest toolbar

## API / Interface

### WebSocket protocol — `ws /api/chat/ws`

All frames are JSON. The client sends; the server sends back a stream of events until `done`.

**Client → Server**

```
// Start or resume a conversation turn
{
  "type": "message",
  "content": string,
  "session_id"?: string,
  "provider"?: "anthropic" | "openai" | "google",
  "model"?: string,           // e.g. "gpt-4o", "gemini-2.5-flash"
  "api_key"?: string          // per-request key from browser storage
}

// Keep-alive (silently ignored by server)
{ "type": "ping" }
```

`session_id` is optional on the first message. `provider`, `model`, and `api_key` are optional — if omitted the server uses the stored Anthropic key with `ClaudeService`. If `provider` is set to a non-Anthropic value, `api_key` must be provided (there is no server-stored key for OpenAI/Google).

**Server → Client**

```
{ "type": "session_start", "session_id": string }
// Sent immediately on connect, and again after each reconnect.

{ "type": "text_delta", "content": string }
// One or more per turn. Concatenate in order to form the full response.

{ "type": "tool_use", "name": string, "input": object }
// Claude is about to invoke a tool.

{ "type": "tool_result", "name": string, "content": string }
// Tool finished. Claude continues generating.

{ "type": "trace", "items": TraceItem[] }
// Step 28a — sent right before `done` when context retrieval was non-empty.
// Each item describes one note that fed the prompt: path, title, score,
// reason ("primary" | "expansion"), dominant signal (`via`), edge_type/tier
// for graph-expansion entries, and a `signals` map of raw per-signal scores.
// Older clients ignore unknown event types; safe to skip.

{ "type": "done", "session_id": string,
  "model"?: string, "provider"?: string, "tool_mode"?: string,
  "metrics"?: {
    "decode_tps"?: number, "prefill_tps"?: number,
    "ttft_ms": number, "load_ms": number, "total_ms": number,
    "eval_count": number, "prompt_eval_count": number
  } }
// Turn is complete. The final assembled response is already in session history.
// `tool_mode` is sent for ollama provider so the frontend can show tool support.
// `metrics` carries Ollama's authoritative per-stage timings when at least one
// round in the turn reported them — decode and prefill durations sum across
// rounds; TTFT is the *first* round's load + prefill (the user-felt initial
// latency); `decode_tps` and `prefill_tps` are precomputed server-side and
// rounded to 2 decimals; `load_ms` is non-zero only on a cold first round.
// Absent when no round emitted timings (older Ollama, error path) — clients
// must treat absence as "no telemetry this turn."

{ "type": "error", "content": string }
// Recoverable or fatal error. Check content for user-facing message.
```

`disconnected` is a synthetic client-side-only event emitted by `useWebSocket` on socket close; it never comes from the server. It is included in the `WsEvent` union type as `WsDisconnected`.

### Tool definitions

Tools are defined in the `tools/` package: schemas in `definitions.py` (the exported `TOOLS` list), dispatch in `executor.py` (the `execute_tool` function). Both are re-exported from `tools/__init__.py` so callers see a single `from services.tools import TOOLS, execute_tool` interface.

| Tool | Required inputs | What it does |
|---|---|---|
| `search_notes` | `query` | Keyword/tag search across the memory index; optional `folder` and `limit` |
| `read_note` | `path` | Reads full Markdown content of a note at `memory/{path}` |
| `write_note` | `path`, `content` | Creates or overwrites a note with full Markdown + frontmatter |
| `append_note` | `path`, `content` | Appends content to an existing note |
| `create_plan` | `title`, `items` | Generates a checklist Markdown note saved to `memory/plans/` |
| `update_plan` | `path`, `task_index`, `checked` | Toggles a checkbox in an existing plan |
| `summarize_context` | `content` | Saves a summary note to `memory/summaries/{date}-{slug}.md` |
| `save_preference` | `rule` | Persists a user behavior rule to `memory/preferences/` |
| `query_graph` | `entity` | Traverses the knowledge graph from an entity up to `depth` hops |
| `ingest_url` | `url` | Fetches a YouTube transcript or web article and saves it to memory |
| `web_search` | `query` | DuckDuckGo HTML search; returns up to 5 `{title, url, snippet}` hits |

The Jira-aware tools (`jira_list_issues`, `jira_describe_issue`, `jira_blockers_of`, `jira_depends_on`, `jira_sprint_risk`, `jira_cluster_by_topic`) are also dispatched from `executor.py` but live in `tools/jira_tools.py` — they are documented under [jira-strategist.md](jira-strategist.md).

All `path` values are relative to `memory/`. Tool errors are caught and returned as strings rather than exceptions, so Claude receives the error text and can decide how to respond.

`web_search` is gated by the privacy kill-switch in `services/privacy.py`: when offline mode is on or the per-feature toggle is off, the tool returns a single `{"error": "..."}` entry instead of calling DuckDuckGo, so Claude sees the block reason and can fall back to local search. See [preferences-settings.md](preferences-settings.md) for the privacy layer.

### `useChat` composable interface

```typescript
const {
  messages,        // Ref<ChatMessage[]> — completed turns only
  currentResponse, // Ref<string> — streaming buffer, cleared on done
  isLoading,       // Ref<boolean>
  toolActivity,    // Ref<string> — e.g. "Searching notes..." or ""
  error,           // Ref<string> — auto-clears after 8 s
  canRetry,        // Ref<boolean> — true for transient errors
  sessionId,       // Ref<string>
  isConnected,     // Ref<boolean>
  init,            // () => void — call once on mount
  sendMessage,     // (content: string) => void
  retry,           // () => void — resends last message
  disconnect,      // () => void
} = useChat()
```

## Gotchas

**Tool input arrives fragmented.** The Anthropic streaming API sends tool input JSON as multiple `input_json_delta` events. `_ToolAccumulator` concatenates these strings and only parses the complete JSON on `content_block_stop`. If Claude sends malformed JSON for a tool input (rare but possible), the accumulator silently returns an empty dict `{}` rather than raising — so tools that require inputs may behave unexpectedly without a visible error.

**Multiple tool calls per Claude turn are now handled.** The router accumulates all `tool_use` blocks from a single streaming pass into a list and executes each one in order before calling the follow-up stream. This removed the earlier limitation where only the last tool call in a pass was processed.

**Session ID switching mid-connection.** A client can pass a different `session_id` in a message to resume a previous conversation. The server will switch context silently if that session exists. The frontend never does this intentionally today, but it means a replayed or forged frame could hijack session context.

**Token usage is logged per-turn, not per-streaming-event.** The `usage_acc` list accumulates tokens across all rounds (initial stream + all follow-up streams). A single conversational turn with three tool calls produces one log entry covering all four Claude API calls. This is intentional for cost tracking but means individual tool-call costs are not individually attributable.

**Error messages auto-clear after 8 seconds** in `useChat`. If the user does not notice and act (e.g. click Retry), the error disappears silently. The underlying WebSocket may still be reconnecting.

**URL ingest in ChatPanel bypasses Claude.** When the user types a URL and clicks "Save to memory", the `ingestUrl` REST call happens directly without going through the WebSocket or adding a user message to the history. The save result is shown in a transient status bar that disappears after 4 seconds and is not recorded in the session.
