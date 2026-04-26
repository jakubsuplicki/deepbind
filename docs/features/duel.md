---
title: Duel Mode
status: active
type: feature
sources:
  - backend/services/council.py
  - backend/services/duel_presets.py
  - backend/routers/chat.py
  - backend/tests/test_duel_backend.py
  - frontend/app/composables/useDuel.ts
  - frontend/app/components/DuelSetup.vue
  - frontend/app/components/DuelDebateView.vue
  - frontend/app/components/DuelScoreBar.vue
depends_on: [chat, specialists, knowledge-graph, retrieval]
last_reviewed: 2026-04-26
---

# Duel Mode

## Summary

Duel Mode runs a structured 2-round debate between exactly two specialists on a user-defined topic, with Jarvis acting as an impartial judge scoring both sides on 5 criteria. The verdict, full debate transcript, and graph edges are saved to `memory/decisions/` so the outcome stays in context for subsequent conversations.

## How It Works

### Backend — `council.py`

`DuelOrchestrator.run()` is an async generator that yields `DuelEvent` objects throughout the debate. The WebSocket handler consumes these and forwards them to the frontend in real time. The full flow:

1. **Validation**: `validate_duel_config()` enforces exactly 2 specialists and a non-empty topic capped at 500 characters. This runs before any API calls.
2. **Context retrieval**: `build_context()` runs a retrieval pass over the workspace notes and returns a shared context string injected into every specialist prompt. Both specialists see the same context.
3. **Round 1 — Opening Positions**: Each specialist receives `build_round1_prompt()`, which includes their role, rules, opponent name/role, and the shared context. Prompts instruct opinionated, non-balanced positions (max 250 words). Responses stream as `specialist_delta` events.
4. **Round 2 — Counter-Arguments**: Each specialist receives `build_round2_prompt()`, which injects their own Round 1 text and the opponent's Round 1 text. Prompts require directly engaging with the opponent's weakest point (max 200 words).
5. **Judging**: A single non-streaming call collects the full judge response, then `parse_judge_verdict()` extracts a `DuelScores` object. The judge prompt instructs Jarvis to output only valid JSON with scores, winner, reasoning, recommendation, and action items.
6. **Token accounting**: Input and output tokens are accumulated across all 5 API calls in `session.token_usage`. If the total exceeds `TOTAL_TOKEN_BUDGET` (25,000), a warning is logged but the duel is not aborted.
7. **Memory save**: `save_duel_to_memory()` writes a Markdown file to `memory/decisions/{date}-duel-{slug}.md` with `type: duel-debate` frontmatter, the full debate body, and the verdict. Graph edges are created: `debated_by` for both specialists and `won_by` for the winner. An additional `duel_recommendation` edge is emitted to any issue keys (e.g. `PROJ-123`) found anywhere in the debate text, weighted by the score margin.
8. **Session history**: After `judge_done`, the verdict summary is appended to the session as an assistant message so the user can continue chatting with the outcome in context.

All prompts append a language-matching rule (`_LANG_RULE`) that instructs the specialist to respond in the same language as the topic.

### Duel Presets — `duel_presets.py`

Four built-in presets are seeded on first use into `memory/duel_presets/` as JSON files. Presets are scenario templates with suggested topics and pre-labelled stances (e.g. "Delivery Planner vs Risk Analyst"). They are exposed via `GET /api/chat/duel-presets` and `GET /api/chat/duel-presets/{id}`. Users can add their own by dropping additional JSON files in that directory.

### WebSocket Integration — `chat.py`

The main WS loop routes any message with `"type": "duel_start"` to `_handle_duel()`. That function validates config, constructs a `DuelOrchestrator` using the same LLM service as regular chat (including the per-connection cached instance), then iterates the async generator. Each `DuelEvent` is mapped to a WS JSON message by prefixing the event type with `duel_`. The `round_num` field is re-keyed to `round` in the outgoing payload. The LLM provider, model, and API key are forwarded the same way as regular chat messages.

### Frontend

**`useDuel.ts`** is the state hub for the entire feature. All duel state (phase, events, currentTexts, verdict, specialists, errorMsg) lives in Nuxt `useState` stores. `bindSend()` must be called once by the parent to wire the WS send function before `start()` is called. The composable attaches the active provider/model/API key from `useApiKeys` to the outgoing `duel_start` message. `getRoundTexts(roundNum)` replays the event log to reconstruct full round text for a given round, used by `DuelDebateView` after a round completes.

Phase progression tracked in `useDuel.ts`:
- `idle` → `round1` (on `start()`)
- `round1` → `round2` (on `duel_round_start` with `round === 2`)
- `round2` → `judging` (on `duel_judge_start`)
- `judging` → `verdict` (on `duel_judge_done`)
- `verdict` → `done` (on `duel_done`)
- any → `error` (on `duel_error`)

**`DuelSetup.vue`** is an inline panel (slides up above the chat input bar) with a topic textarea and a specialist toggle list. Adding a third specialist is blocked at the UI level once two are selected. The setup panel also shows the currently active LLM provider and model. A session spend warning appears when `sessionDuelSpend > 1.0`.

**`DuelDebateView.vue`** replaces the chat panel while a duel is active. It reads specialist identity from the `duel_setup` event and accumulates streaming text from `duel_specialist_delta` events via `currentTexts` (for live display) and the event log (for completed rounds via `getRoundTexts`). Round 1 auto-collapses when phase transitions to `round2` and can be toggled back open. The judging phase renders a full-size Jarvis Orb with a pulsing "Evaluating arguments..." label. The verdict phase renders a Jarvis Orb mini-header followed by `DuelScoreBar` inline. Markdown in specialist responses is rendered via `marked` and sanitized with DOMPurify. The cancel button changes label to "Close Duel" in the error phase. After verdict, a "Back to Chat" button is shown instead of Cancel.

**`DuelScoreBar.vue`** renders the verdict. Specialist A is colored cyan, specialist B is purple — these are fixed visual roles, not tied to which specialist wins. The main bar shows percentage split by total score. The criteria breakdown shows 5 mirrored bars (A grows right-to-left, B grows left-to-right). The winner section uses amber/gold with a pulsing glow animation. The footer always shows "Saved to memory · Graph updated".

## Key Files

| File | Purpose |
|------|---------|
| `backend/services/council.py` | DuelOrchestrator, data models, prompt builders, verdict parsing, memory save |
| `backend/services/duel_presets.py` | Built-in preset definitions, seed/list/get operations |
| `backend/routers/chat.py` | WS duel routing, `_handle_duel()`, preset REST endpoints |
| `backend/tests/test_duel_backend.py` | Config validation, prompt building, verdict parsing, memory save tests |
| `frontend/app/composables/useDuel.ts` | All duel state, WS event routing, phase transitions |
| `frontend/app/components/DuelSetup.vue` | Setup panel with topic input, specialist picker, cost hint |
| `frontend/app/components/DuelDebateView.vue` | Live debate timeline, round collapse, judging indicator, verdict display |
| `frontend/app/components/DuelScoreBar.vue` | Main percentage bar, per-criterion breakdown, winner badge |

## API / Interface

### WebSocket — Client to Server

```json
{
  "type": "duel_start",
  "topic": "Should we refactor before adding 2FA?",
  "specialist_ids": ["delivery-planner", "risk-analyst"],
  "provider": "anthropic",
  "model": "claude-sonnet-4-20250514",
  "api_key": "sk-ant-..."
}
```

### WebSocket — Server to Client (event stream)

| Event type | Key fields |
|---|---|
| `duel_setup` | `specialists: [{id, name, icon}]`, `topic`, `duel_id` |
| `duel_round_start` | `round: 1\|2`, `label` |
| `duel_specialist_start` | `specialist`, `round` |
| `duel_specialist_delta` | `specialist`, `content`, `round` |
| `duel_specialist_done` | `specialist`, `round` |
| `duel_judge_start` | — |
| `duel_judge_done` | `scores`, `winner`, `reasoning`, `recommendation`, `action_items`, `token_usage` |
| `duel_done` | `saved_path`, `duel_id` |
| `duel_error` | `content` |

### REST Endpoints (presets)

```
GET /api/chat/duel-presets          → list all presets (seeds built-ins on first call)
GET /api/chat/duel-presets/{id}     → single preset or 404
```

### TypeScript Composable

```typescript
const {
  isActive,       // Ref<boolean>
  topic,          // Ref<string>
  phase,          // Ref<DuelPhase>  — 'idle'|'round1'|'round2'|'judging'|'verdict'|'done'|'error'
  verdict,        // Ref<DuelVerdict | null>
  events,         // Ref<DuelEvent[]>
  currentTexts,   // Ref<Record<string, string>>  — live streaming text keyed by specialist name
  specialists,    // Ref<{id,name,icon}[]>
  showSetup,      // Ref<boolean>
  errorMsg,       // Ref<string>
  bindSend,       // (fn: WsSendFn) => void  — must be called before start()
  start,          // (config: DuelConfig) => void
  handleWsEvent,  // (event: DuelEvent) => void  — called by useChat for duel_* events
  cancel,         // () => void
  openSetup,      // () => void
  closeSetup,     // () => void
  getRoundTexts,  // (roundNum: number) => Record<string, string>
} = useDuel()
```

### Scoring Criteria

Each criterion is scored 1–5 by the judge. Maximum total per specialist is 25.

| Criterion | What it measures |
|---|---|
| `relevance` | How directly the argument addresses the user's question |
| `evidence` | Use of concrete facts or references to the user's notes |
| `argument_strength` | Logic quality — sound reasoning with no unsupported leaps |
| `counter_argument` | How effectively Round 2 challenged the opponent's specific points |
| `actionability` | Whether the argument leads to immediately actionable recommendations |

## Gotchas

- **`DuelOrchestrator.run()` must be consumed with `async for`** — it is an async generator, not a coroutine. Awaiting it directly will not work.
- **`bindSend()` must be called before `start()`** in `useDuel.ts`. The composable stores the send function in a module-level closure variable (`_send`). If the parent mounts the duel UI before wiring the WS, the `duel_start` message is silently dropped (`_send` defaults to `() => false`).
- **Judge winner fallback**: if the judge returns a `winner` value that is not one of the two specialist IDs, `parse_judge_verdict()` falls back to whichever specialist has the higher total score. It does not raise an error. This masks model hallucinations but means the winner field in the saved note can differ from what the judge's reasoning describes.
- **Judge markdown fences**: despite the prompt instructing JSON-only output, the judge sometimes wraps the response in ` ```json ``` ` fences. `parse_judge_verdict()` strips these before parsing.
- **Token budget is a warning, not a hard stop**: exceeding 25,000 tokens logs a warning but does not abort the duel. The duel will complete and the cost will exceed the estimated ~$0.08.
- **Graph save errors are swallowed**: the `save_duel_to_memory()` function catches all graph exceptions and logs a warning. A failed graph update does not surface as a `duel_error` event — the `duel_done` event fires regardless. The `DuelScoreBar` footer always shows "Graph updated" even if the update failed.
- **`duel_specialist_start` events exist but are not listed in the old WS table**: the backend emits `duel_specialist_start` before each specialist begins streaming. The frontend does not currently use this event, but it is present in the event stream and in the `DuelEvent` type union.
- **Validation runs twice**: `validate_duel_config()` is called in both `_handle_duel()` (backend WS handler) and inside `DuelOrchestrator.run()`. The WS handler call surfaces errors as plain `error` WS events (not `duel_error`). The orchestrator call would yield a `duel_error` event. In practice only the first call fires because invalid config never reaches the orchestrator.
- **Topic length**: `validate_duel_config()` enforces a 500-character topic limit. The spec does not mention this cap — it was added in the implementation.
