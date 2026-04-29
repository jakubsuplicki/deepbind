---
title: Ingest latency baseline
type: concept
last_updated: 2026-04-29
related_features:
  - memory
  - latency-benchmark
  - chat
related_adrs:
  - 010
  - 011
  - 013
---

# Ingest latency baseline

Sibling concept to [eval-baseline](eval-baseline.md) (conversation
quality) and [latency-baseline](latency-baseline.md) (chat-path
TTFT/TPS). This one measures the **document-ingest pipeline** — the
time from "user drops a PDF" to "every stage of compute is done."

## Why this exists

Document ingest is the second user-visible latency surface. Until ADR
013 we had no measurement; "the embedder is the bottleneck" was a
guess and "PDF extraction takes a while" was folklore. Without numbers
we can't pick which knob to turn first.

The harness times every pipeline stage individually plus end-to-end
wall clock, against a stable fixture, with a paired bootstrap-CI gate
on the per-stage durations. Same shape as the chat-latency harness —
one statistical floor across all three benchmarking surfaces.

## What's measured

Five pipeline stages plus end-to-end:

| Stage | What runs | Why we time it |
|-------|-----------|----------------|
| `extract` | `pdfplumber` text extraction | Dominant cost on most PDFs |
| `section_detect` | Heading-detection heuristic | Pure-Python; should be cheap |
| `chunk` | `chunk_markdown` multi-granularity chunker | Determines how many chunks the embedder sees |
| `embed_batch` | `embed_texts` (fastembed / ONNX MiniLM) | Per-chunk encode cost; scales with chunk count |
| `entity_extract` | `extract_entities` (spaCy NER + regex) | Runs per-section in production; per-call shape is what matters |
| `end_to_end` | Every stage in pipeline order | What the user feels |

The **compute pipeline** is what's measured. Disk write / SQLite index
/ Smart Connect are excluded — those are I/O-bounded and rarely the
bottleneck on consumer hardware. The harness measures what we can
optimize via code.

## Scenarios

Six v1 scenarios, all against `samples/911Report.pdf`:

- **end-to-end-911Report** — full pipeline; reports per-stage breakdown plus total wall clock
- **extract-911Report** — pdfplumber only
- **section-detect-911Report** — heading detection on pre-extracted text
- **chunk-911Report** — chunk_markdown on pre-extracted text
- **embed-batch-64-911Report** — fastembed encode of 64 chunks (chunks pre-prepared)
- **entity-extract-911Report** — spaCy NER on first 50 KB of pre-extracted text

Stage-isolated scenarios share prepared upstream inputs across the
warm-up + N timed runs so the measurement reflects only the target
stage's variance.

## Running it

```bash
# Full grid (~2 min): end-to-end + every isolated stage, 3 timed runs each
.venv/bin/python -m tests.eval.ingest.run_bench

# PR-mode (~90s): end-to-end only, 3 timed runs
.venv/bin/python -m tests.eval.ingest.run_bench --scope pr

# Knob comparison: tag the run so future readers know what was on
.venv/bin/python -m tests.eval.ingest.run_bench \
    --knob-stack pymupdf_extract \
    --out tests/eval/ingest/baselines/baseline-pymupdf.json
```

Output goes to `backend/tests/eval/ingest/baselines/<machine>-<ts>.json`
by default; that path is the canonical artifact, committed to git.

## Canonical baseline-0 (2026-04-28, pdfplumber)

Apple M5 Pro 24 GB / Ollama 0.18.0 / stock pipeline / `samples/911Report.pdf`
(585 pages, 7.5 MB, 1.9 M chars).

Artifact: `backend/tests/eval/ingest/baselines/apple-m5-20260428T124627Z.json`.

```
scenario                                    total p50    total p95
chunk-911Report                                  66ms         66ms
embed-batch-64-911Report                        496ms        497ms
end-to-end-911Report                          20275ms      20525ms
  └─ extract                                  19686ms      19920ms
  └─ section_detect                              11ms         11ms
  └─ chunk                                       68ms         68ms
  └─ embed_batch                                501ms        517ms
  └─ entity_extract                               7ms          7ms
entity-extract-911Report                          7ms          7ms
extract-911Report                             19878ms      20284ms
section-detect-911Report                         11ms         11ms
```

### What baseline-0 said

- **Extract was 97% of end-to-end.** Every other stage was rounding
  error against pdfplumber. PDF text extraction on a 585-page document
  took ~20 s sequentially.
- **chunk was 66 ms on 5,005 emitted chunks.** ~13 µs per chunk. Not
  a bottleneck.
- **embed-batch-64 was 496 ms.** ~7.7 ms per chunk on MiniLM-L12 on
  CPU. Throughput ~129 chunks/s — surfaced the production embedding
  loop as a future bottleneck.

## Baseline-1 (2026-04-28, pypdfium2 swap, cap-bug version — superseded)

Reported end-to-end 950 ms; `embed_batch_size=64` was capping the
end-to-end embedding stage at the first 64 chunks. **Numbers were
wrong**; replaced by baseline-2 once the harness lie was fixed. Kept
in artifact form (`apple-m5-20260428T131344Z.json`) for historical
reference only.

## Baseline-2 (2026-04-28, pypdfium2 swap, honest measurement)

Same hardware, `knob_stack: [pypdfium2_extract,honest_e2e_embed]`,
same fixture. Harness fix: `IngestScenario.embed_batch_size` default
changed from `64` to `0` (= "embed all chunks") so end-to-end matches
production's `embed_note_chunks` behavior.

Artifact: `backend/tests/eval/ingest/baselines/apple-m5-20260428T135114Z.json`.

```
scenario                                    total p50    total p95
chunk-911Report                                  70ms         71ms
embed-batch-64-911Report                        501ms        520ms     (per-batch throughput)
end-to-end-911Report                          43059ms      43149ms     (HONEST end-to-end)
  └─ extract                                    376ms        379ms
  └─ section_detect                              11ms         12ms
  └─ chunk                                       71ms         74ms
  └─ embed_batch                              42588ms      42681ms     (all 5,546 chunks)
  └─ entity_extract                               8ms          8ms
entity-extract-911Report                          7ms          7ms
extract-911Report                               369ms        370ms
section-detect-911Report                         11ms         11ms
```

### Knob-1 result (honest)

The pypdfium2 swap saved ~17 seconds (~30% end-to-end). The cap-bug
"21×" headline was a measurement artifact; what's real:

| Stage | true baseline-0 p50 | baseline-2 p50 | Speedup |
|-------|---------------:|---------------:|--------:|
| extract | 19,686 ms | 376 ms | **55×** (unchanged from cap-bug analysis) |
| end-to-end | ~60,000 ms | 43,059 ms | **~1.4×** (honest) |
| embed_batch (all chunks) | ~40,000 ms | ~42,588 ms | 1.0× (untouched by knob-1) |
| Other stages | combined ~100 ms | ~100 ms | 1.0× (untouched) |

Quality verified independently — pypdfium2 is at minimum equivalent
to pdfplumber on prose and meaningfully better on multi-column
timelines (no false-adjacency interleaving). Full quality comparison
in [ADR 013 §"Extraction-quality verification"](../architecture/decisions/013-ingest-latency-benchmark-harness.md#extraction-quality-verification-2026-04-28).

**embed_batch is now 98.8% of end-to-end.** It is the bottleneck. The
knob loop targets it from multiple angles.

### Knob loop

| # | Knob | Targets | Status | Effect |
|---|------|---------|--------|-------|
| 1 | **pypdfium2 replaces pdfplumber** | extract | ✅ landed (2026-04-28) | extract 55× / end-to-end ~30% |
| 2 | **Chunk-level content-hash skip** | re-ingest path | ✅ landed (2026-04-28) | Re-ingest unchanged 25.6s → 1.4s (18.6×); partial edit 25.6s → 2s (12.9×); first-ingest unchanged |
| 3 | ONNX Runtime threading | first-ingest embed | ❌ closed (2026-04-29) | No-op at production scale; auto already optimal. `parallel=4` worth +9% but +1.6 GB RAM (deferred sub-knob) |
| 4 | int8 quantized embedding model | first-ingest embed | future | ~2-3× embed; <2 pt MTEB regression |
| 5 | Smaller English-only model (MiniLM-L6) | first-ingest embed | future | ~2× embed; quality measurement via ADR 010 harness |
| 6 | **Background / lazy embedding** | first-ingest UX | ✅ landed (2026-04-29) | First-ingest 911Report 25.6 s → **1.53 s** (16.7×); compute unchanged, decoupled into `ingest_jobs.embed_paths` background job |
| 7 | MLX backend (Apple Silicon NPU) | first-ingest embed | future tier 2 | ~5-10× on Mac; Mac-only path |
| 8 | Parallel page extraction | extract | future, optional | PDFium isn't thread-safe; needs `multiprocessing.Pool` |

### Knob-6 landed (2026-04-29) — background embedding decouples user latency from compute

`_emit_document_sections` now writes per-section MD files and indexes
them with `defer_embedding=True`, then fires
`ingest_jobs.schedule_embed_for_paths(...)` as a daemon-threaded
background job before returning. The synchronous portion (extract +
section-split + write MDs + index rows + Smart Connect on the index)
finishes in ~1.5 s on the 911 Report; the embed pass catches up
asynchronously in ~24 s with per-note progress (`embedding 12/61…`)
visible via `GET /api/memory/ingest/status`.

| Phase | Before knob-6 | After knob-6 |
|---|---:|---:|
| User-perceived (HTTP response) | 25.6 s | **1.53 s** |
| Background embed (async) | 0 s | 24.55 s |
| Total compute | 25.6 s | 26.08 s |

Cumulative on first-ingest of 911Report.pdf, vs the original
pdfplumber + inline-embed baseline:

| State | First-ingest (user-perceived) |
|---|---:|
| Pre-knob-1 (pdfplumber + inline embed) | ~60 s (estimated honest) |
| After knob-1 (pypdfium2 + inline embed) | 25.6 s |
| After knob-6 (deferred embed) | **1.53 s** |
| Cumulative | **~40× user-perceived speedup** |

Eventual-consistency cost: notes are not searchable until the
background embed job catches up (~24 s on a 60-section document).
The UI's existing per-note "indexing N/M" badge surfaces this
window; sibling-section links go through wiki-links (written
synchronously) so the graph view is unaffected. Full discussion in
[ADR 013 Amendment 5](../architecture/decisions/013-ingest-latency-benchmark-harness.md#amendment-5--knob-6-landed-backgroundlazy-embedding-for-section-split-ingest-2026-04-29).

### Knob-3 closed (2026-04-29) — threading is already optimal

Measured: ORT's auto-pick `intra_op_num_threads=0` (= physical cores)
is the fastest on a real 5,546-chunk pass at 40.9 s. Clamping threads
to the M5 Pro's 4 P-cores — what the 64-batch micro-bench suggested —
slows production-scale ingest by 4%.

The 64-batch sweep favored `threads=4` by ~1.4%. The 5,546-chunk pass
inverted that result because at scale the work spans ~22 successive
ORT calls and the auto-pick gets to overlap them across all 10 cores.
Single-batch sync penalties amortize away.

`parallel=4` (fastembed multiprocessing) was the only positive signal
at +9%, but at the cost of +1.6 GB resident memory (4 model copies)
and almost-certain regression on small docs. Recorded as a sub-knob
to layer with int8 (knob-4), not as its own landing.

Net: no code change. Headroom from here is in `(model, ONNX-config)`
or hardware-accelerator space, not threading. See
[ADR 013 Amendment 4](../architecture/decisions/013-ingest-latency-benchmark-harness.md#amendment-4--knob-3-investigated-onnx-runtime-threading-is-already-optimal-2026-04-29).

### Production vs harness numbers

The harness measures a **compute-only end-to-end** that does NOT
mirror production's section-split-first behavior. Production's
`fast_ingest` splits a long PDF into per-section MD files BEFORE
chunking, so the chunker runs on each section's body and emits ~3,023
chunks total. The harness chunks the whole 1.9 M-char extracted text
as one document and emits ~5,546 chunks (~45% more).

| Path | Chunks emitted | end-to-end (911Report) |
|------|---------------:|-----------------------:|
| Harness compute-only end-to-end | 5,546 | 43.1 s |
| Production `fast_ingest` (section-split) | 3,023 | 25.6 s |

Both numbers are real. The harness number is "worst case if we ever
disable section-split"; the production number is "what the user
actually waits." Knob iterations should track *both* — the harness
gives stable per-stage diffs, the production measurement gives
realistic UX impact.

### Knob-2 production measurements (2026-04-28)

Real fast_ingest path measurements against `samples/911Report.pdf` on
M5 Pro 24 GB:

| Scenario | Time | vs first ingest |
|----------|----:|----------------:|
| First ingest (cold) | 25.6 s | 1.0× |
| Re-ingest, unchanged content | 1.4 s | **18.6× faster** |
| Re-ingest, one paragraph edited | 2.0 s | **12.9× faster** |

This unblocks the "I accidentally re-imported the same file," "I
clicked Reindex," and "I edited a typo" UX paths.

Each knob produces its own baseline. `compare_runs(pairs)` from
`tests.eval.ingest.gate` produces one verdict per metric (paired
bootstrap CI on the difference) — same statistical floor as
`tests.eval.latency.gate.compare_runs` and the conversation-eval gate.

## Floor test

`backend/tests/eval/ingest/test_ingest_floor.py` is opt-in
(`JARVIS_INGEST_BENCH=1`). Validates the canonical baseline file has
sensible shape; full regression-gate (compare run-to-baseline-0 under
bootstrap CI) lands with the first knob chunk that produces
baseline-1.

## What this doesn't measure

- **Disk write / SQLite indexing.** Out of scope; rarely the
  bottleneck. If it becomes one, that's a separate fixture.
- **Smart Connect / cross-document linking.** Runs after ingest; its
  own latency surface, not part of the ingest harness.
- **Re-ingestion.** The content-hash short-circuit isn't exercised by
  the current scenarios. A "re-ingest unchanged document" scenario is
  a follow-on if we want to verify the cache path.
- **Multi-fixture sweep.** The 911 Report is one document; per-stage
  *throughput* (ms / KB or ms / page) is the portable metric. A short
  memo and a long technical paper would round out the per-document-
  size curve.
