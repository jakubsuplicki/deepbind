---
title: Retrieval Trace UI
status: active
type: feature
sources:
  - frontend/app/components/TraceList.vue
  - backend/services/retrieval/pipeline.py
  - backend/services/context_builder.py
  - backend/tests/test_context_builder_trace.py
depends_on:
  - retrieval
  - chat
  - knowledge-graph
last_reviewed: 2026-04-26
---

# Retrieval Trace UI

## Summary

After every assistant answer, a collapsible "Used context" panel shows which notes were included in the prompt, the dominant retrieval signal for each primary hit (BM25, cosine, graph), and the graph-edge provenance for any expansion notes. The plumbing already existed in the retrieval pipeline and context builder; this feature surfaces it without adding any new endpoints or persisting anything to storage.

## How It Works

### Signal attachment — pipeline.py

During hybrid-fusion scoring in [`backend/services/retrieval/pipeline.py`](../../backend/services/retrieval/pipeline.py) (around line 816), after the weighted sum of `_bm25`, `_cosine`, `_graph`, and `_enrichment` is computed, a `_signals` dict is attached directly to each candidate dict before it is sorted and returned. Only non-zero optional fields (`enrichment`, `boost`) are included, so the dict is minimal for simple queries.

### Trace assembly — context_builder.py

[`backend/services/context_builder.py`](../../backend/services/context_builder.py) builds two kinds of trace entries:

- **Primary entries** — built inline as each retrieved candidate is formatted into the prompt. The dominant signal is derived by picking the key with the highest value from `_signals`. Each entry carries `reason="primary"`, the `via` field set to that dominant key, the fused `_score`, and the full `signals` dict.
- **Expansion entries** — built inside `_build_expansion_context` (around line 605) as graph-neighbour notes are fetched. These carry `reason="expansion"`, `via="graph"`, `edge_type` (the graph edge label, e.g. `"related"`, `"similar_to"`), an optional `tier` (`"strong"` or `""`), and an empty `signals` dict because fusion scores do not apply to graph-pulled notes.

`build_context` now returns a three-tuple `(prompt_str, token_count, trace_list)`. Only notes that actually made it into the prompt appear in the trace — candidates dropped by the token budget are excluded (verified by `test_build_context_trace_empty_when_no_results`).

### WebSocket delivery — chat.py

After Claude finishes streaming, the chat router emits one additional WebSocket event before `done`:

```json
{ "type": "trace", "items": [ ... ] }
```

Event order within a single response is: `text_delta` … `text_delta` → `trace` → `done`. Older clients that do not handle the `trace` type treat it as a no-op because the frontend already ignores unknown event types.

### Frontend — useChat.ts + TraceList.vue

[`frontend/app/composables/useChat.ts`](../../frontend/app/composables/useChat.ts) holds a module-level `_pendingTrace` variable. When the `trace` event arrives, `items` is stored there. When `done` fires and the assistant message is committed to `messages`, `_pendingTrace` is attached as `msg.trace` and then cleared. This means the trace is always co-located with its message in the reactive state; there is no separate trace store.

[`frontend/app/components/ChatPanel.vue`](../../frontend/app/components/ChatPanel.vue) renders `<TraceList :items="msg.trace" />` immediately below the message bubble, but only for assistant messages with a non-empty trace.

[`frontend/app/components/TraceList.vue`](../../frontend/app/components/TraceList.vue) is collapsed by default (`expanded = ref(false)`). The toggle button shows "Used context (N)" with a rotating chevron. When expanded, each row renders:

- A filled dot for primary hits, hollow dot for expansion notes.
- A `NuxtLink` to `/memory?path=<encoded-path>` so the user can jump straight to the source note.
- A label from `describe(item)`: for primary hits, `"{via} {score}"` (e.g. `"cosine 0.81"`); for expansion notes, `"via {edge_type}, {tier}"` (e.g. `"via related, strong"`).

## Key Files

| File | Role |
|------|------|
| [`backend/services/retrieval/pipeline.py`](../../backend/services/retrieval/pipeline.py) | Attaches `_signals` dict to every scored candidate during hybrid fusion |
| [`backend/services/context_builder.py`](../../backend/services/context_builder.py) | Assembles primary and expansion trace entries; returns `(prompt, tokens, trace)` from `build_context` |
| [`frontend/app/composables/useChat.ts`](../../frontend/app/composables/useChat.ts) | Buffers the `trace` WS event in `_pendingTrace` and attaches it to the assistant message on `done` |
| [`frontend/app/components/TraceList.vue`](../../frontend/app/components/TraceList.vue) | Collapsed-by-default panel that renders per-note signal labels and navigation links |
| [`frontend/app/components/ChatPanel.vue`](../../frontend/app/components/ChatPanel.vue) | Mounts `<TraceList>` under each assistant bubble when `msg.trace` is present |
| [`backend/tests/test_context_builder_trace.py`](../../backend/tests/test_context_builder_trace.py) | Unit tests for trace completeness, primary/expansion distinction, empty-result handling, and WS event ordering |

## Gotchas

**Traces are not persisted.** `_pendingTrace` lives only in the `useChat` composable instance. A page refresh drops all traces — this is intentional per the spec. Do not attempt to restore traces from `session_history` events; the backend does not send them there.

**`signals` is empty for expansion notes.** Graph-expansion notes are fetched by edge traversal, not by the hybrid scorer, so they never pass through fusion and carry no `_signals` dict. `TraceList` handles this silently — `describe()` renders the `edge_type` / `tier` path instead.

**`tier` is only meaningful for `suggested_related` edges.** The context builder skips `suggested_related` edges whose tier is not `"strong"` (see `context_builder.py` line 561). For all other edge types (`related`, `similar_to`, etc.) `tier` is recorded as-is from the graph but may be an empty string, which `TraceList` omits from the label.

**`via` on primary entries reflects the dominant signal, not the retrieval method.** A note retrieved by BM25 can show `via="cosine"` if its cosine score happened to be highest after fusion. The `via` field is purely the argmax of `signals`, chosen for display clarity, not a record of which index found the note.

**If retrieval returns nothing, `build_context` returns `(None, 0, [])`.** The chat router must handle the `None` prompt gracefully before emitting a `trace` event — an empty trace list results in the `<TraceList>` component rendering nothing at all (the `v-if="items && items.length > 0"` guard in the template).

**`via` in the `TraceItem` type is dual-purpose.** For expansion notes it is always `"graph"`, while the actual edge label is in `edge_type`. The `describe()` function in `TraceList.vue` displays `edge_type || via` for expansion rows, so `edge_type` takes precedence when present.
