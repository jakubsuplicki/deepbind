# ADR 021 — Sidecar ML warmup at boot, not on first chat turn

**Status:** Accepted
**Date:** 2026-05-08
**Related:** [ADR 003](003-desktop-distribution-tauri-and-sidecars.md), [ADR 009](009-context-overflow-compaction.md), [ADR 015](015-single-target-local-only-stack.md), [ADR 016](016-chat-send-via-tauri-ipc.md), [ADR 018](018-english-only-v1-scope.md)

## Context

The chat hot path consumes four heavy ML artefacts that load lazily on first use:

| Component | Lazy-load cost | Memory | Source |
|---|---|---|---|
| fastembed embedder (`snowflake-arctic-embed-l`) | ~3 s | ~1 GB | [`embedding_service.py`](../../../backend/services/embedding_service.py) |
| fastembed cross-encoder reranker (`bge-reranker-v2-m3`) | ~5–8 s | ~600 MB | [`reranker_service.py`](../../../backend/services/reranker_service.py) |
| spaCy NER pipeline (`xx_ent_wiki_sm`) | ~2 s | ~100 MB | [`entity_extraction.py`](../../../backend/services/entity_extraction.py) |
| HuggingFace `tokenizers` Rust extension + `tokenizer.json` files | ~50–200 ms each | small | [`token_counting.py`](../../../backend/services/token_counting.py) |

Without warmup, those costs land on the **first** call site to touch each component. On a fresh app start, that produces a brutal pattern: turn 1 has empty session history, so the retrieval pipeline at `services/retrieval/pipeline.py` short-circuits — the embedder + reranker never load on turn 1's request path. The first call site that fires is `_save_session_bg` in [`session_service.py`](../../../backend/services/session_service.py), which calls `extract_entities` (loading spaCy). Turn 2 then runs the full retrieval pipeline (lazy-loading embedder + reranker) **and** contends with whatever spaCy state is still loading on the asyncio thread.

### The bug, by the diagnostic numbers

Per-turn instrumentation across builds #9–13 on Apple M5 24 GB:

- Turn 1 of cold session: ~16 s wall clock (Ollama mmap + first prefill, no ML lazy-loads on the request path).
- Turn 2 of cold session: **25–30 s wall clock**. The decomposed instrumentation shows `rust_to_fastapi_ms ≈ 20 000+` with `fastapi_to_lock_ms ≈ 0.1 ms` — i.e. the FastAPI route handler runs immediately *once accepted*, but `accept()` itself is starved for ~20 s. The starvation is the asyncio thread holding the GIL while sync ML work runs (embedder ONNX load + reranker ONNX load + ongoing spaCy import).
- Turn 3+: ~1–2 s, identical to the post-prefix-stable warm baseline.

This is the only cold-start turn that pays the ML lazy-load cost. Subsequent app launches that hit the same warm caches don't see it.

### Prior fixes that didn't close the bug

- **ADR 016** (chat sends via Tauri IPC) addressed a separate WKWebView outbound-throttle issue. It eliminated a different 27 s stall but not this one — the WKWebView fix is on the *send* direction; the lazy-load stall is on the *receive direction's accept queue*.
- **`ws_send_queue.py`** (per-WS outbound queue, 2026-05-07) decoupled `_send_event` from `ws.send_json` back-pressure. It eliminated lock-coupling on idle-WKWebView turns but didn't address the GIL-held lazy-load problem on cold-start turn 2.
- **`asyncio.to_thread` wrapping in `_save_session_bg`** moved spaCy off the event loop. Necessary but not sufficient — spaCy is only one of four lazy-loads, and the embedder + reranker still fire on turn 2's retrieval call site.

The pattern that emerged: every chained fix peeled off one symptom but left the underlying class of bug — *lazy ML loads on the request path* — intact. The right structural fix is to eliminate the class.

## Decision

Run a synthetic warmup of every heavy ML component at sidecar boot, in a background thread, before the first user message can be dispatched.

Concretely, [`backend/services/warmup_service.py`](../../../backend/services/warmup_service.py):

- Spawns a single daemon `threading.Thread` (`name="jarvis-warmup"`) from the FastAPI lifespan, after `start_workers()`. The lifespan does **not** await it; the `yield` proceeds immediately. Startup remains non-blocking.
- The thread sequentially exercises each component on dummy input:
  1. **tokenizer** — walks `_BUNDLED_TOKENIZER_IDS`, calling `get_tokenizer(id)` once per id. Primes the `tokenizers` Rust extension import + caches every catalog tokenizer so chat-model switches mid-session are instant.
  2. **NER** — `extract_entities("Warmup note: John Smith met with ACME Corp on 2026-01-15.")`. Triggers the full spaCy pipeline (tokenizer + tagger + NER), not just the import.
  3. **embedder** — `embed_query("warmup")`. Exercises both the ONNX model load and Arctic-Embed-L's query-prefix path.
  4. **reranker** — `rerank("warmup query", ["dummy document one", "dummy document two"])`. Triggers the cross-encoder forward pass alongside the ONNX load.
- Status is exposed via `GET /api/health/warm`. Returns `{ready, started, completed, started_at, completed_at, components: {name: {state, duration_ms, error}}}`. Always 200; readiness is a payload field, not an HTTP status, so the route is cheap to hit while loading. The frontend [`useWarmup.ts`](../../../frontend/app/composables/useWarmup.ts) singleton polls with backoff (`250, 250, 500, 500, 1000, 1000, 2000` ms then 2 s steady-state) and stops on `ready: true`.
- Sequential ordering chosen so components most likely to be needed by an early chat dispatch finish first (tokenizer is fast; NER fires on `_save_session_bg`; embedder + reranker fire on the retrieval pipeline). Parallel ONNX loads contend for the same CPU + memory bandwidth so they aren't actually faster on M-series hardware — sequential is predictable and only ~10–15 s total.
- Per-component states progress `pending` → `running` → `ready` / `failed` / `skipped`. `skipped` is **not an error**: `JARVIS_DISABLE_RERANKER=1` or a stripped `tokenizers` package returns `skipped`, which the chat path's documented fallbacks already handle. `failed` is also non-fatal: the chat path will still try to load the component on demand and degrade per its own contract (e.g. reranker → fusion-only retrieval).
- The orchestrator wraps each warmer in its own try/except so a rogue future warmer can't abort the loop and leave later components stuck in `pending` forever. `start()` is idempotent: the second call is a no-op.

### Frontend surface

[`ChatPanel.vue`](../../../frontend/app/components/ChatPanel.vue) renders a small instrument-readout pill above the messages area when `started && !ready`:

```
● PREPARING MODELS  | tokenizer · NER · embedder · reranker
```

Per-component states are styled (running = phosphor amber, ready/skipped = struck-through, failed = neon orange). The pill transitions out via `<Transition name="warmup-pill">` the instant warmup completes. It never blocks input — if the user dispatches a chat turn while warmup is still running, the chat path takes the lazy-load hit on whatever isn't yet loaded, which now degrades to "the warmup we already showed you, but on the chat path" rather than a silent 25-30 s stall.

In practice warmup completes in ~10–15 s on M5 24 GB; the user typing their first message takes longer than that on average, so the chat path almost always finds the components already hot.

## Trade-offs

| Choice | Benefit | Cost |
|---|---|---|
| Daemon thread, not asyncio task | Sync ML loads can't starve the event loop, no matter what they do internally. | Thread can't be `await`-ed; status is exposed via a Lock-guarded snapshot + a `threading.Event` for `wait_for_ready`. |
| Sequential warmup | Predictable; smallest-and-most-likely-needed components finish first. | ~10–15 s total instead of ~5–8 s if parallelised. Acceptable because warmup runs in the background while the user reaches the chat surface and starts typing. |
| Synthetic dummy input vs. real warmup queries | Zero risk of polluting user data. Trivially deterministic. | The first *real* call still pays a tiny "warm but not for *this* input" cost — but that's millisecond-scale, not the seconds-scale lazy-load. |
| Status endpoint as part of `/api/health/*` family | Reuses the existing `useApi` plumbing and CORS posture. Frontend already polls health surfaces. | Health endpoints are exempt from the entitlement gate (per ADR 019); future readers might assume warmup status is also unauthenticated by design — it is, but the reasoning is "it's diagnostic, not authoritative" not "it's safety-critical." |
| Failures non-fatal | Chat path still works (each component has its own degradation contract). | A latently-broken bundle can ship — e.g. corrupt fastembed cache — and only surface as "retrieval is fusion-only" rather than a hard error at boot. Mitigated by per-component error reporting in the status endpoint and existing build-time integrity checks in `desktop/sidecar/jarvis-sidecar.spec`. |

## Alternatives Considered

- **Move all `import` statements to module-level so they fire at sidecar startup automatically.** Rejected: this triggers the `import` cost but not the *first inference* cost, which is the bigger chunk for ONNX models. Both fastembed `TextEmbedding` and `TextCrossEncoder` allocate the ONNX session lazily on first `.embed(...)` / `.rerank(...)` call, not at construction. Module-level imports also break PyInstaller bundle analysis in some edge cases (the `.spec` file relies on `services` modules being importable on demand).
- **Block the FastAPI lifespan until warmup completes.** Rejected: would push perceived sidecar boot from ~2 s to ~15 s, which is felt as the entire app feeling unresponsive at launch. The current shape lets the UI mount and show the chat surface immediately; the warmup pill is a small honest indicator while the underlying caches finish loading.
- **Warm only what the *next* chat dispatch will need.** Rejected: requires guessing what the user will do (dispatch chat? do retrieval? trigger entity extraction via note save?). Cheaper to warm everything at boot than to be wrong.
- **Push the warmup into the `desktop/scripts/build-bundled-app.sh` build step (e.g. pre-cached ONNX session state).** Rejected: ONNX Runtime's session state isn't safe to serialize across processes; bundle size would balloon; and Apple Silicon ANE/Metal kernel-cache files are version-pinned to the runtime build. Net negative.
- **Auto-trigger warmup via a hidden first-load HTTP request from the frontend.** Rejected: only fires when the frontend connects, which is racy on slow startups. Lifespan-triggered guarantees warmup begins as early as the sidecar can run code at all.

## Migration Path

Lands as one self-contained chunk:

1. New service file [`backend/services/warmup_service.py`](../../../backend/services/warmup_service.py) + 10 unit tests in [`backend/tests/test_warmup_service.py`](../../../backend/tests/test_warmup_service.py).
2. New endpoint `GET /api/health/warm` in [`backend/main.py`](../../../backend/main.py).
3. Lifespan wires `warmup_service.start()` after `start_workers()`.
4. New composable [`frontend/app/composables/useWarmup.ts`](../../../frontend/app/composables/useWarmup.ts).
5. Polling started from [`frontend/app/layouts/default.vue::onMounted`](../../../frontend/app/layouts/default.vue).
6. Pill rendered from [`frontend/app/components/ChatPanel.vue`](../../../frontend/app/components/ChatPanel.vue).

No prior surface changes meaning. No prior contract is broken.

## Verification

- **Backend tests:** 1772 passed (was 1762; +10 from `test_warmup_service.py`).
- **Frontend build:** `nuxi build` clean (vue-tsc + nitro prerender both pass).
- **Pending live verification:** rebuild the bundled `.app` and confirm on M5 24 GB that turn 2 of a cold session lands at ~1–2 s wall clock — closing the 25–30 s stall observed across builds #9–13.
