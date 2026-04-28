---
title: Chat & LLM Integration
status: active
type: feature
sources:
  - backend/routers/chat.py
  - backend/services/chat/__init__.py
  - backend/services/chat/context_strategy.py
  - backend/services/claude.py
  - backend/services/llm_service.py
  - backend/services/_anthropic_client.py
  - backend/services/tools/__init__.py
  - backend/services/tools/definitions.py
  - backend/services/tools/executor.py
  - backend/services/web_search.py
  - backend/services/token_tracking.py
  - frontend/app/composables/useChat.ts
  - frontend/app/composables/useWebSocket.ts
  - frontend/app/components/ChatPanel.vue
  - frontend/app/components/TraceList.vue
depends_on: [retrieval, sessions, specialists, api-key-management, preferences-settings]
last_updated: 2026-04-28
last_reviewed: 2026-04-28
---

# Chat & LLM Integration

## Summary

This is the core conversation loop of Jarvis: a WebSocket channel carries user messages to the backend, which retrieves relevant memory context, calls a LLM provider with streaming enabled, and forwards response tokens back to the browser in real time. The backend supports multiple LLM providers (Anthropic, OpenAI, Google AI) via LiteLLM, with Anthropic as the native default. Claude can also invoke tools during a response — reading or writing notes, querying the graph, creating plans — with the tool call chain completing before the browser finalises the message.

## How It Works

### Connection lifecycle

The frontend opens a single persistent WebSocket to `GET /api/chat/ws` on page load. The backend immediately creates a session and sends `session_start` with a `session_id`. That ID travels with every subsequent message so the backend appends to the correct conversation history. If the connection drops, `useWebSocket` schedules an exponential backoff reconnect (1 s → 2 s → 4 s → … capped at 15 s) and sends a synthetic `disconnected` event so the chat UI can reset its loading state without waiting.

A heartbeat ping is sent from the frontend every 25 seconds to keep the connection alive through proxies. The backend silently ignores `ping` frames.

### Per-message flow

For each user message the backend (`_handle_message` in `chat.py`) runs the following sequence:

1. **Provider routing** — The WS message may include `provider`, `model`, and `api_key` fields. If `provider` is non-Anthropic (e.g. "openai" or "google"), an `LLMService` is created wrapping LiteLLM. If provider is "anthropic" or absent, the native `ClaudeService` is used for best streaming fidelity. Client-provided keys are used per-request and never persisted; if no key is provided, the server-stored Anthropic key is used as fallback.
2. **Context assembly via `ContextStrategy`** — Session history is fetched via `session_service.get_messages` and passed through the active `ContextStrategy` (default: `FullHistoryStrategy`, which is identity). The strategy is the swap point through which compaction and retrieval-substitution alternatives plug in (ADR 010). Production today behaves exactly as before; the eval harness injects alternative strategies via the `context_strategy` parameter on `_handle_message`.
3. **Context retrieval** — `build_system_prompt` calls `build_context` (from `context_builder`) to find relevant notes and appends them to the base system prompt. If a specialist is active, its prompt fragment is injected first.
4. **Tool filtering** — `specialist_service.filter_tools` narrows the full `TOOLS` list to those permitted by the active specialist (or all tools if none is active).
5. **First LLM stream** — The service's `stream_response` opens a streaming request. `text_delta` events are forwarded to the WebSocket immediately as they arrive. For non-Anthropic providers, `LLMService` converts Anthropic-format tools to OpenAI format and handles message format conversion automatically.
6. **Tool call handling** — If the LLM emits a `tool_use` block, its JSON input is accumulated across streaming deltas by `_ToolAccumulator` (ClaudeService) or `_LiteLLMToolAccumulator` (LLMService) and yielded as a single `StreamEvent` only when complete. The router then executes the tool, appends both the assistant tool-use block and the tool result to the message list, and calls `_stream_follow_up` to get the LLM's next response.
7. **Recursive tool rounds** — `_stream_follow_up` is recursive. Each recursive call increments a `depth` counter. The chain is cut off at `MAX_TOOL_ROUNDS = 5` to prevent runaway loops. Within the loop, `_compact_stale_tool_results` collapses older tool_result payloads to a short reminder (the model has already read them) — this is an in-loop optimization separate from the `ContextStrategy` swap point above.
8. **Session save** — `add_message` auto-persists after each append (crash protection). On WebSocket disconnect the session is additionally written to `memory/` as a Markdown note.
9. **Token logging** — Accumulated `input_tokens` + `output_tokens` from all rounds in a turn are written to `app/logs/token_usage.jsonl` via `log_usage` with `provider` and `model` fields for accurate cost tracking. Non-Anthropic providers use LiteLLM's `model_cost` for pricing estimates.

### ContextStrategy — the compaction swap point

`backend/services/chat/context_strategy.py` defines the `ContextStrategy` Protocol: a small `assemble(messages) -> messages` interface that decides which session-history messages get sent to the model on each turn. The production default is `FullHistoryStrategy` — a literal identity over the input — so the abstraction landed behavior-preserving when introduced (ADR 010, 2026-04-27). All future compaction policy (naive recent-N truncation, retrieval-first substitution per ADR 009, per-profile compaction per ADR 005) attaches at this point. The eval-side runner injects alternative strategies via the `context_strategy` parameter on `_handle_message` to compare them against `full-history` on frozen conversation fixtures.

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
- **[`chat_adapters.py`](../../backend/tests/eval/conversations/chat_adapters.py)** — concrete `ChatCallable` implementations. **`OllamaChat` is the default** (talks to `http://127.0.0.1:11434`, model `qwen3:30b-a3b` per ADR 008's pinned chat slot, temperature 0, fixed seed) so the eval measures the production stack honestly. **`AnthropicChat` is opt-in** (hosted Claude, faster iteration but different model family — must not be promoted to canonical baseline). The `make_chat(provider=...)` factory picks between them; `make_chat_factory(provider=...)` returns a `seed → ChatCallable` factory used by the multi-seed runner. Anthropic-style content blocks (text / tool_use / tool_result) are translated to each provider's native message shape at the adapter boundary, including the tool_use → tool_calls and tool_result → tool-role-message conversions for Ollama. **Chain-of-thought stripping** (`_strip_thinking`) runs on every Ollama response: Ollama 0.18 with `think: false` strips the opening `<think>` tag but leaves the model's reasoning prose plus `</think>` in the body, so the adapter discards everything up to and including the last `</think>` before returning. Without this, `must_not_contain` guards regex over the chain-of-thought and trip on rejected-candidate names that the model only mentioned in reasoning — false-positive confabulation rates as high as 30+ pp on guard-heavy fixtures.
- **[`rescore.py`](../../backend/tests/eval/conversations/rescore.py)** — re-score a captured baseline JSON without re-running the model. When the scorer or the thinking-strip changes, an existing run can be re-interpreted in <1 s rather than re-spending the 45 min – 2 h a 30B grid takes. Loads the baseline, strips chain-of-thought from each `response_text`, re-runs `score_turn` against the matching fixture turn, regenerates aggregations and gate decisions, and writes a new baseline tagged with `rescored_from_timestamp_utc` so artifact lineage is unambiguous. CLI: `python -m tests.eval.conversations.rescore <baseline.json> [--out <path>]`.
- **[`gate.py`](../../backend/tests/eval/conversations/gate.py)** — bootstrap-CI strategy comparison. Replaces the original fixed-threshold gate language ("if naive scores within 5 points of full-history…") with a 95% confidence interval on the difference between two strategies' per-fixture clean-pass rates. The bootstrap is paired by fixture (preserving fixture-level variance) and seeded for determinism. The verdict is one of `improvement` / `regression` / `equivalent` / `insufficient_data` based on whether the CI excludes zero. The `adr_009_gate(full_history, naive_truncate, retrieval_substitution=None)` convenience runs ADR 010's two named questions in one call: (1) does naive truncation match full-history? (2) does retrieval-substitution beat naive? Each returns its own `GateDecision`.
- **[`run_eval.py`](../../backend/tests/eval/conversations/run_eval.py)** — the CLI. `python -m tests.eval.conversations.run_eval --strategies full-history,naive-truncate-4,naive-truncate-8 --provider ollama --seeds 1,2,3` orchestrates every (strategy × fixture × seed) combination, computes the gate decisions, and writes a stable-key JSON baseline file. Default strategies sweep multiple naive-truncate values so retrieval-substitution must beat the *best* simple alternative, not the first value tested. Default provider is Ollama; `--provider anthropic` is the opt-in iteration loop. `--retrieval` enables the production-retrieval wiring; `--workspace-path` pins the retrieval workspace for reproducibility.

Neither file is in the production path; the runner is dev infra that the engineer invokes locally before merging compaction-affecting changes.

### Error handling in ClaudeService

All Anthropic SDK exceptions are caught and converted to `StreamEvent(type="error")` with user-readable messages. Rate limits, 529 overload responses, 401 authentication failures, and generic 5xx errors each get distinct copy. The frontend detects whether an error message is retryable (matches "try again", "overloaded", "rate limit", or "reconnect") and shows a Retry button if so.

### Frontend state

`useChat` owns all conversation state: the message list, the streaming `currentResponse` buffer, loading/error flags, and `toolActivity` (a label like "Searching notes…" shown while a tool runs). It delegates connection management entirely to `useWebSocket`. When a `done` event arrives, `currentResponse` is flushed into the `messages` array and loading state clears.

`ChatPanel.vue` renders messages with `marked` + `DOMPurify` for safe Markdown output. The streaming response is rendered live with a blinking cursor appended. A URL detection feature watches the input field: if a URL is typed, a toolbar appears offering to save it to memory via `ingestUrl` (bypassing Claude entirely — this is a direct REST call to the ingest endpoint).

### Token budget constants

`token_tracking.py` defines soft budget caps intended for use in context assembly:

| Constant | Value |
|---|---|
| `TOTAL_BUDGET` | 4000 tokens |
| `CONTEXT_BUDGET` | 2500 tokens |
| `PREFERENCES_BUDGET` | 500 tokens |
| `SPECIALIST_BUDGET` | 500 tokens |
| `HISTORY_BUDGET` | 500 tokens |

`check_budget()` is now called in `chat.py` before each Claude API call. When the accumulated token count exceeds `TOTAL_BUDGET`, the call is blocked and an error event is sent to the client. When the count exceeds the warning threshold, a notification event is sent but the call proceeds. Token usage is logged at `$3/MTok` input and `$15/MTok` output (claude-sonnet-4 pricing at time of writing).

## Key Files

- `backend/routers/chat.py` — WebSocket endpoint, per-message orchestration, multi-provider routing through the [InferenceRouter](inference-router.md) (`_route_request` → `_llm_from_decision`; `_make_llm` is the back-compat shim), recursive tool chain loop, session save-on-disconnect
- `backend/services/claude.py` — `ClaudeService` wrapping the Anthropic streaming API; `_ToolAccumulator` for reassembling fragmented tool-input JSON; `build_system_prompt` for context injection
- `backend/services/llm_service.py` — `LLMService` wrapping LiteLLM for OpenAI/Google AI/any LiteLLM-supported provider; `LLMConfig` dataclass; `_LiteLLMToolAccumulator` for OpenAI-format streamed tool calls; format converters (`convert_tools_anthropic_to_openai`, `convert_messages_for_litellm`)
- `backend/services/_anthropic_client.py` — Sync `anthropic.Anthropic` factory, present for test-mocking intent but unused by the codebase (all live paths use `AsyncAnthropic` directly in `ClaudeService`)
- `backend/services/tools/definitions.py` — Anthropic-format input schemas for every tool exposed to Claude (the `TOOLS` list).
- `backend/services/tools/executor.py` — `execute_tool` dispatcher: maps tool names to underlying service calls (memory CRUD, graph queries, web search, Jira). All tool errors are converted to user-readable strings here.
- `backend/services/tools/__init__.py` — Re-exports `TOOLS` and `execute_tool` so callers can keep using `from services.tools import …` after the package split.
- `backend/services/web_search.py` — Thin DuckDuckGo wrapper with privacy gating (`services/privacy.py`). Returns `[{title, url, snippet}, …]` or `[{"error": "<reason>"}]` when blocked.
- `backend/services/token_tracking.py` — Append-only JSONL usage log with per-day and all-time aggregation helpers; records `provider` and `model` per entry; uses LiteLLM pricing for non-Anthropic providers
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
  "route"?: { "provider": string, "model_id": string|null,
              "request_class": string, "slot_class": string,
              "reason": string } }
// Turn is complete. The final assembled response is already in session history.
// `tool_mode` is sent for ollama provider so the frontend can show tool support.
// `route` is the InferenceRouter audit decision (ADR 004) — runtime UI panel
// reads it to render which slot served the turn and why. Older clients
// ignore unknown fields.

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
