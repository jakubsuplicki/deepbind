---
title: Smart Connect
status: active
type: feature
sources:
  - backend/services/connection_service.py
  - backend/services/connection_events.py
  - backend/services/alias_index.py
  - backend/services/dismissed_suggestions.py
  - backend/routers/connections.py
  - frontend/app/pages/memory.vue
  - frontend/app/components/SmartConnectStatus.vue
  - frontend/app/components/BulkPromoteBanner.vue
  - frontend/app/components/SuggestionsPanel.vue
  - frontend/app/components/settings/SmartConnectSection.vue
depends_on:
  - memory
  - knowledge-graph
  - retrieval
last_reviewed: 2026-05-18
last_updated: 2026-05-18
---

## Summary

Smart Connect is a per-note, ingest-time linking system that runs automatically whenever a note is created or updated. It uses cheap, deterministic signals — BM25, note embeddings, chunk embeddings, and alias matching — to populate a `suggested_related` block in each note's frontmatter, which users then promote or dismiss. It replaces the previous `rebuild_graph()` call on every ingest with an incremental approach that targets only the newly ingested note against the existing corpus, keeping the operation well under 300 ms regardless of vault size.

## How it works

### Signal sources and scoring

`connect_note()` in [connection_service.py](backend/services/connection_service.py) is the single entry point called by the ingest path. It builds a connection query from the note's title, headings, tags, and first 800 characters of body, then fans out to four parallel signal sources:

- **BM25** — full-text search over `notes_fts`, capped at 15 results
- **Note embeddings** — cosine similarity against note-level vectors, top 10
- **Chunk embeddings** — cosine similarity against chunk vectors, top 10, with section attribution for evidence text
- **Alias matching** — n-gram scan (n=1..4) of the note body against the `alias_index` table

Each signal set is max-normalised within its own result set to `[0, 1]`. The combined score formula is:

```
score = (0.30 × bm25) + (0.30 × note_emb) + (0.20 × chunk_emb)
      + (0.10 × entity) + (0.07 × alias) + (0.03 × same_source)
```

When a signal source is unavailable (embeddings disabled, no chunks for a short note), the score is divided by the sum of weights of active signals rather than leaving the dead weight in the denominator. A perfect BM25 hit with no other signals still maxes out at 1.0 in the BM25-only space — this is graceful degradation, not hidden weight inflation.

Scores map to tiers: `strong` (≥ 0.80), `normal` (0.60–0.79), `weak` (0.45–0.59, dropped in `fast` mode). Caps: 5 suggestions total, 2 from the same folder, 1 near-duplicate (≥ 0.92).

### Semantic orphan repair

If a note in `fast` mode produces zero suggestions at `strong` or `normal` tier, `connect_note()` checks whether the note is a semantic orphan (no edges of meaningful types in the graph) and if so re-runs in `aggressive` mode, which also accepts the `weak` tier. This is a self-healing path for notes that genuinely have few neighbours.

### Alias index and guardrails

[alias_index.py](backend/services/alias_index.py) maintains a SQLite table `alias_index(phrase_norm, note_path, kind)` populated from each note's `title`, `aliases`, `headings`, and `weak_aliases` frontmatter fields. On ingest, `scan_body()` generates n-grams from the new note's text and exact-matches them against the index.

Three guardrails prevent noise-driven matches:

1. **Minimum length** — phrases under 4 characters are dropped, with an explicit `_ALIAS_SHORT_ALLOWLIST` (`aws`, `jwt`, `sql`, `css`, etc.) for discriminative acronyms that legitimately fall under the floor.
2. **Stopword rejection** — phrases whose every token is in `_ALIAS_STOPWORDS` are silently discarded (`ai`, `api`, `memory`, `graph`, `model`, `data`, etc.). A phrase with even one non-stopword token passes.
3. **Frequency cap** — a phrase already present in more than `max(10, 5% of total notes)` distinct notes is not indexed for the new note. The count check happens before the insert so a blocked phrase never pollutes the index.

### `weak_aliases` edge classification

Notes may declare a `weak_aliases:` frontmatter list alongside the standard `aliases:`. Entries indexed as `kind='weak_alias'` contribute `0.35` to the alias score rather than `1.0`, and — critically — a `weak_alias` hit alone never emits a suggestion: the scorer in `_merge_candidates()` skips any candidate where the only non-zero signal is a sub-1.0 alias score. A `weak_alias` can raise a candidate's score when another signal has already fired, but it cannot independently reach the `strong` tier.

### Provenance edges

After writing frontmatter, `_emit_provenance_edges()` adds two classes of graph edges without requiring a global rebuild:

- `derived_from` — `note → source:<sha1(source)[:12]>` when `fm["source"]` is set (populated by URL/PDF/Jira ingest)
- `same_batch` — `note → batch:<id>` when `fm["batch_id"]` is set by structured ingest flows

These are provenance signals, not semantic ones. They are excluded from `find_semantic_orphans()` by default so a note imported from a URL but never genuinely related to other notes still registers as a semantic orphan.

### Dismissal persistence

[dismissed_suggestions.py](backend/services/dismissed_suggestions.py) stores `(note_path, target_path)` pairs the user has explicitly rejected. Before scoring, `generate_suggestions()` removes dismissed targets from every signal dict, so they never score, never cap a slot, and never reappear. The store is operational state (not canonical knowledge) — wiping it only causes dismissed suggestions to reappear once, not data loss.

### Quality loop and event log

[connection_events.py](backend/services/connection_events.py) records analytics-only events in `connection_events(event_type, note_path, target_path, confidence, methods_json, tier, smart_connect_version, created_at)`. Events are written on promote, dismiss, and during backfill (deduped per `(note_path, target_path, version)` per day to avoid log bloat). This table is the source for acceptance rate and per-method effectiveness in `GET /api/connections/stats`. It is strictly append-only analytics — the `dismissed_suggestions` table remains the authoritative dedup guard for the pipeline.

Each `SuggestedLink` carries a `score_breakdown: {method: contribution}` dict when two or more signals fired. Values are final weighted contributions after normalisation and must sum to `confidence ± 0.001`. When only one method fires the field is omitted.

### Backfill

`POST /api/connections/backfill` runs `connect_note()` over the whole vault (or only semantic orphans). It streams newline-delimited JSON progress via `Content-Type: text/event-stream` from a POST endpoint. A per-note skip check avoids reprocessing notes already at `CURRENT_SMART_CONNECT_VERSION` with existing suggestions that are not orphans; `force=true` overrides this.

The `connect_note()` function splits into `generate_suggestions()` (pure read, safe to call in dry-run) and `apply_suggestions()` (writes frontmatter, graph, edges). `dry_run=True` returns candidates without touching any persistent state.

Versioning is stored per-note in frontmatter as `smart_connect.version`. Bumping `CURRENT_SMART_CONNECT_VERSION` in [connection_service.py](backend/services/connection_service.py) causes the next backfill to revisit all notes at a lower version without requiring `force=True`.

### Ingest path replacement

`fast_ingest()` no longer calls `rebuild_graph()`; instead it calls `connect_note()` which internally calls `graph_service.ingest_note()` for an incremental graph update. The full `rebuild_graph()` is still invoked by batch structured ingest (after the whole batch), "Reindex all" actions, and algorithm version bumps.

## Key files

- [connection_service.py](backend/services/connection_service.py) — orchestrates per-note candidate generation, scoring, cap enforcement, frontmatter writes, and graph edge emission; exposes `connect_note()`, `generate_suggestions()`, and `apply_suggestions()`
- [connection_events.py](backend/services/connection_events.py) — append-only analytics event log for promote/dismiss/backfill events; feeds `GET /stats` acceptance-rate and method-breakdown aggregations
- [alias_index.py](backend/services/alias_index.py) — SQLite-backed phrase index with NFKD normalisation, guardrail filtering (min-length, stopwords, frequency cap), and n-gram body scan
- [dismissed_suggestions.py](backend/services/dismissed_suggestions.py) — persists user-dismissed `(note_path, target_path)` pairs so they are excluded from all future scoring passes
- [routers/connections.py](backend/routers/connections.py) — FastAPI router for all `/api/connections/` endpoints including backfill SSE stream, stats, bulk-promote, and coverage
- [memory.vue](frontend/app/pages/memory.vue) — renders the workspace bulk-review banner and opens the first pending suggestion note from `coverage.pending_note_paths`
- [SmartConnectStatus.vue](frontend/app/components/SmartConnectStatus.vue) — polling status badge that shows active/idle/warn state, inline progress bar for background section linking, and auto-opens a completion popover
- [BulkPromoteBanner.vue](frontend/app/components/BulkPromoteBanner.vue) — workspace-level "Link all" / "Review" banner driven by `pending_strong_suggestions` and `pending_strong_notes` from the coverage endpoint; requires explicit confirmation before calling `POST /promote-bulk`
- [SuggestionsPanel.vue](frontend/app/components/SuggestionsPanel.vue) — per-note suggestion list with Keep / Dismiss actions, "Keep all (N)" bulk flow for 2–5 strong suggestions, and "Why?" score breakdown tooltip
- [SmartConnectSection.vue](frontend/app/components/settings/SmartConnectSection.vue) — Settings panel for triggering backfill; consumes the SSE stream via `fetch()` + `ReadableStream` and renders a live progress bar

## API / Interface

```
POST /api/connections/run/{note_path}        — re-run connect_note for an existing note
POST /api/connections/dismiss               — persist a dismissal; body: {note_path, target_path}
POST /api/connections/promote               — move suggestion into related:; body: {note_path, target_path}
POST /api/connections/backfill              — SSE stream, body: BackfillRequest
POST /api/connections/promote-bulk          — workspace bulk-promote; body: {min_confidence, scope, dry_run}
GET  /api/connections/orphans               — list semantic orphan notes
GET  /api/connections/stats                 — workspace quality metrics (acceptance rate, method breakdown)
GET  /api/connections/coverage              — lightweight coverage snapshot for sidebar badges
```

Key Python signatures from [connection_service.py](backend/services/connection_service.py):

```python
async def connect_note(
    note_path: str,
    workspace_path: Optional[Path] = None,
    mode: str = "fast",          # "fast" | "aggressive"
    *,
    dry_run: bool = False,
    min_confidence: Optional[float] = None,
    force: bool = False,
) -> ConnectionResult: ...

async def generate_suggestions(
    note_path: str,
    workspace_path: Optional[Path] = None,
    mode: str = "fast",
) -> _SuggestContext: ...        # read-only; never mutates disk or DB

async def apply_suggestions(
    ctx: _SuggestContext,
    min_confidence: Optional[float] = None,
    *,
    force: bool = False,
) -> ConnectionResult: ...
```

`ConnectionResult` carries `suggested: list[SuggestedLink]`, `strong_count`, `aliases_matched`, `graph_edges_added`, and `unchanged` (True when suggestions were byte-equivalent to existing frontmatter and the write was skipped).

## Gotchas

**`suggested_related` is never auto-written to `related`.** The `related:` frontmatter key is reserved for user-confirmed links (weight 0.9 in the graph). The pipeline only writes `suggested_related`. Promoting via the UI or API is the only path to `related:`.

**Alias guardrails run before the DB insert.** The frequency cap check uses the count of distinct notes already holding a phrase. Because the upsert deletes the note's existing rows first, `already_indexed` is always `False` at check time — a phrase blocked by the cap simply does not appear in the index for that note. This means a phrase that was acceptable when a note was first indexed may be silently dropped on re-index if the vault has grown.

**`weak_alias` alone never produces a suggestion.** The check in `_merge_candidates()` is: if the only non-zero signal is `al_score > 0.0 and al_score < 1.0` (weak alias), the candidate is skipped entirely. A `weak_alias` must be accompanied by at least one of `bm25`, `note_emb`, or `chunk_emb` to influence the final score.

**Provenance edges (`derived_from`, `same_batch`, `suggested_related`) are excluded from semantic orphan detection.** A note imported from a URL has a `derived_from` edge to its source node, but that does not rescue it from orphan status. Only edges that represent genuine semantic relationships — `related`, `mentions`, `mentions_org`, `alias_match`, etc. — count. The constant `SUGGESTED_RELATED_MAX_WEIGHT = 0.35` in `graph_service/queries.py` also caps how much unconfirmed suggestions can influence retrieval ranking.

**Backfill streams via POST, not EventSource.** `POST /api/connections/backfill` returns `text/event-stream` but native `EventSource` only supports GET. The frontend in [SmartConnectSection.vue](frontend/app/components/settings/SmartConnectSection.vue) (and any future consumer) must use `fetch()` + `ReadableStream` to parse the newline-delimited JSON lines.

**Backfill idempotency.** Without `force=True`, a note is skipped if `smart_connect.version >= CURRENT_SMART_CONNECT_VERSION` and `suggested_related` exists and the note is not a semantic orphan. The in-memory fingerprint check in `_finalise()` provides a second guard: even when a note is not skipped by version, if the new suggestions are byte-equivalent to the existing frontmatter the file is not rewritten and `ConnectionResult.unchanged` is set to `True`. This prevents Obsidian/git noise on repeated "Run on all notes".

**`score_breakdown` is omitted for single-signal suggestions.** When only one method fires, the breakdown dict is not written to frontmatter (it would just repeat the total). The "Why?" tooltip in [SuggestionsPanel.vue](frontend/app/components/SuggestionsPanel.vue) is conditionally rendered on the presence of `score_breakdown`.

**`method_breakdown` in stats comes from frontmatter, not `alias_index`.** `GET /api/connections/stats` scans `suggested_related[].methods` across every note file to count which signals drove suggestions. The `alias_index.*` block in the response is index health data only and is intentionally not mixed with method effectiveness counts.
