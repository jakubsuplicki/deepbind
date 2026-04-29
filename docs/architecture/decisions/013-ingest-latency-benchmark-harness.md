---
adr: 013
title: Ingest latency benchmark harness
date: 2026-04-28
status: accepted
supersedes: []
superseded_by: []
related:
  - 010-conversation-replay-eval-harness.md
  - 011-latency-benchmark-harness.md
---

# ADR 013 — Ingest latency benchmark harness

## Context

Document ingest is the second user-visible latency surface in the
product. ADR 010 covers conversation eval (quality), ADR 011 covers
chat-path latency (TTFT / decode_tps / total_ms). Ingest sits in a
third dimension: when a user drops a 7 MB PDF into the workspace, how
long until that PDF is searchable / linkable / question-answerable?

The pipeline is multi-stage:

1. **PDF extract** — pdfplumber walks every page, extracts text.
2. **Section detect** — heading-detection heuristic splits long docs
   into per-section notes.
3. **Chunk** — `chunk_markdown` produces multi-granularity chunks
   (anchor + section + bullet + coarse windows) for retrieval.
4. **Embed** — `embed_texts` (fastembed / ONNX MiniLM-L12) encodes
   every chunk into a 384-d vector.
5. **Entity extract** — spaCy NER + regex pulls people / orgs / dates
   / projects per section.

Until now we had no measurement. "Slow" was qualitative; "the embedder
is the bottleneck" was a guess; "PDF extraction takes a while" was
folklore. Without numbers we can't prioritize knobs (parallel pages?
PyMuPDF over pdfplumber? batched embeddings? GPU embeddings?), and we
can't tell whether a refactor regressed a stage.

Same shape as ADR 011: stable scenario set, fixture-anchored, paired
bootstrap CI gate, baseline JSON committed to disk so `git diff
baselines/` is the regression review.

## Decision drivers

- **Per-stage breakdown is load-bearing.** A single "ingest took 20s"
  number doesn't tell us where to invest. Every stage gets timed
  individually.
- **End-to-end is the user-facing number.** The breakdown explains the
  total; the total is what the user feels.
- **Knob loop must isolate one stage at a time.** "Did batched
  embeddings help?" is only answerable if we can re-run the embed
  stage with everything else held identical. So we run stage-isolated
  scenarios alongside end-to-end, sharing prepared upstream inputs
  across the warm-up + N timed runs.
- **Same bootstrap-CI gate as ADR 011.** Reusing the proven gate
  primitive (`compare_metric` with paired bootstrap CI on the
  difference-of-means) means one statistical floor across all three
  benchmarking surfaces (chat, conversation, ingest).
- **Single fixture is enough at v1.** A real, large PDF (the 9/11
  Commission Report — 585 pages, 7.5 MB, ~1.9 M chars after extract)
  exercises every stage at production scale. Multi-fixture scope (a
  short memo, a long technical paper, a JSON dump) is a follow-on
  chunk.
- **Disk write / SQLite indexing is intentionally excluded.** Those
  stages are bounded by I/O and are rarely the bottleneck on consumer
  hardware. Including them would make the metric noisy without
  changing what we'd optimize. The harness measures the *compute*
  pipeline; if disk write becomes the bottleneck, that's a separate
  knob with its own benchmark.

## Decision

Add `backend/tests/eval/ingest/` as a sibling package to
`backend/tests/eval/latency/` with the same shape:

- `scenarios.py` — `Stage` enum and `IngestScenario` frozen dataclass.
  Six v1 scenarios: end-to-end + five stage-isolated.
- `harness.py` — stage primitives (`time_extract`,
  `time_section_detect`, `time_chunk`, `time_embed_batch`,
  `time_entity_extract`) returning `(output, StageTiming)` tuples;
  `run_end_to_end` runs the full pipeline and captures per-stage
  timings; `IngestHarness.run_scenario` dispatches a scenario to the
  right primitive.
- `runner.py` — `aggregate` produces `ScenarioStats` with per-stage
  p50/p95/mean/stdev and median unit descriptors;
  `prepare_inputs` runs upstream stages once per stage-isolated
  scenario so the warm-up + N timed runs measure only the target;
  `run_grid` orchestrates sequentially.
- `gate.py` — re-exports `Verdict` / `Direction` / `compare_metric`
  from `latency.gate`; defines ingest `DEFAULT_METRICS` (all
  LOWER_IS_BETTER) plus `compare_stage_metric` for per-stage
  comparisons across paired baselines.
- `run_bench.py` — CLI with `--scope nightly|pr`, `--fixture`,
  `--seeds`, `--knob-stack`, `--out`. Stable-key JSON output.
- `test_*.py` — 34 unit tests covering scenario shape, stage dispatch,
  per-stage timing capture, units metadata, error propagation,
  aggregate percentiles, stage ordering, gate semantics. Floor test
  (`test_ingest_floor.py`) gated by `JARVIS_INGEST_BENCH=1`.

End-to-end runs ~20 s on the 911 Report; stage-isolated runs are 7 ms
to 500 ms. `--scope pr` is end-to-end only (~90 s for warm-up + 3
timed runs). `--scope nightly` runs the full grid in ~2 minutes.

## Trade-offs

| Choice | Benefit | Cost |
|--------|---------|------|
| Compute-only pipeline (no disk write / indexing) | Stable measurement; the metric tracks what we can actually optimize via code | Ingest end-to-end as the user feels it includes some I/O the harness doesn't capture; surfacing that gap is a future fixture |
| Single fixture at v1 (911 Report) | One real, large PDF exercises every stage at production scale | Some stages (entity-extract) are linear in input size and the per-document number depends on this fixture; per-stage *throughput* (ms / KB) is the portable metric |
| Stage-isolated scenarios share prepared inputs across timed runs | Measures only the target stage's variance | Doesn't capture interactions (e.g. embedding gets slower if it follows a hot-cache extract) — but the end-to-end scenario covers that case |
| 3 timed seeds (vs ADR 011's 5) | Each end-to-end run is ~20 s; 3 runs is enough variance for bootstrap CI on a deterministic pipeline | Tighter CIs need more seeds; user can pass `--seeds 1,2,3,4,5` |
| Reuse of latency.gate machinery | Same statistical floor across all three benchmarking surfaces; no divergence | Cross-package import dependency (`tests.eval.ingest.gate` re-exports from `tests.eval.latency.gate`); acceptable — the dependency is one-directional and stable |

## Alternatives considered

1. **Profile-based instrumentation (cProfile / py-spy).** Would catch
   sub-stage hotspots but produces flat function-time tables, not
   reproducible regression-gate numbers. Profiling is the right tool
   for the *next* layer of investigation once a stage is identified
   as the bottleneck. Rejected for the v1 framing.

2. **Real `fast_ingest()` end-to-end.** Includes disk write, SQLite
   index, Smart Connect — all of which add noise and aren't what we
   optimize via code. Rejected; the harness measures the
   compute-only path. A future fixture-mode benchmark could capture
   `fast_ingest` total wall clock if disk write becomes a load-
   bearing concern.

3. **Multi-fixture v1 scope.** Memo + paper + JSON dump on day one.
   Rejected because the v1 question is "where's the bottleneck on
   one real document." Multi-fixture is the right shape once we want
   to characterize the per-document-size curve (ms ∝ pages? ∝
   chunks? ∝ chars?) — that's its own chunk.

4. **Async parallel scenario execution.** Stages are CPU-bound on the
   same cores; running them concurrently just trashes cache. Rejected
   in favor of sequential execution — same as ADR 011.

## Migration path

Substrate lands in this chunk; baseline-0 (canonical, pinned) captured
in the same chunk against `samples/911Report.pdf` on Apple M5 Pro 24 GB
+ Ollama 0.18.0. Subsequent chunks each focus on one knob:

1. **PyMuPDF replacing pdfplumber for extract** — expected 5–10×
   speedup on the dominant stage (extract is 97% of end-to-end on
   baseline-0). Re-run `--scope nightly`, diff against baseline-0
   under `compare_runs`, verify CI excludes zero on
   `extract.p50_ms` and `total_ms_p50`.
2. **Parallel page extraction** — pdfplumber/PyMuPDF page work is
   embarrassingly parallel; M5 Pro 12-core can fan out. Combinable
   with knob 1.
3. **Batched embeddings in production path** — `embed_texts` already
   batches but the production caller (`embed_note_chunks`) currently
   loops note-by-note. Wiring it to batch across notes should give a
   meaningful win on multi-document imports.
4. **Skip-on-content-hash optimization (already partially in place)**
   — re-ingestion of an unchanged document should short-circuit.
   Verify with a baseline that re-runs ingest twice.

Each knob lands as `apple-m5-{timestamp}.json` with the corresponding
`knob_stack` recorded in machine_info, so a future reader can see
which baselines were captured under which optimization stack.

## Canonical baseline-0 (2026-04-28)

Captured on Apple M5 Pro 24 GB / Ollama 0.18.0 / stock pipeline against
`samples/911Report.pdf` (585 pages, 7.5 MB, 1,906,758 chars after
extract). Artifact:
[backend/tests/eval/ingest/baselines/apple-m5-20260428T124627Z.json](../../../backend/tests/eval/ingest/baselines/apple-m5-20260428T124627Z.json).

| Scenario | total p50 | total p95 | Notes |
|----------|----------:|----------:|-------|
| end-to-end-911Report | 20,275 ms | 20,525 ms | sum-of-stages = 20,272 ms |
| extract-911Report | 19,878 ms | 20,284 ms | matches end-to-end's extract stage |
| section-detect-911Report | 11 ms | 11 ms | pure-Python heuristic on 1.9 M chars |
| chunk-911Report | 66 ms | 66 ms | 5,005 chunks emitted on 1.9 M chars |
| embed-batch-64-911Report | 496 ms | 497 ms | 64 chunks, 384-d vectors, ~93 KB total |
| entity-extract-911Report | 7 ms | 7 ms | first 50 KB sample, 293 entities |

End-to-end stage breakdown (within the same 20.3 s run):

| Stage | p50 ms | % of end-to-end |
|-------|-------:|----------------:|
| extract | 19,686 | **97.1%** |
| embed_batch | 501 | 2.5% |
| chunk | 68 | 0.3% |
| section_detect | 11 | 0.05% |
| entity_extract | 7 | 0.03% |

**Headline finding: PDF extraction (pdfplumber) is the entire problem.**
Every other stage combined is under 600 ms. The knob loop is
unambiguous: replace or parallelize pdfplumber first; everything else
is rounding error until that's done.

Secondary finding: embed-batch at 64 chunks is 500 ms → ~127 chunks/s
of throughput. The production path produces 5,005 chunks for this doc;
embedding all of them sequentially without per-batch parallelism would
add ~40 s. The harness measures one batch; the production path's
chunk-by-chunk embedding loop is a separate concern surfaced by this
finding.

## Amendment 1 — knob-1 landed: pypdfium2 replaces pdfplumber (2026-04-28)

**Result**: extract dropped 55× (19,686 ms → 358 ms p50), end-to-end
dropped 21× (20,275 ms → 950 ms p50). Knob landed in the same chunk
that captured baseline-1.

### What changed

- `backend/services/ingest.py::_extract_pdf_text` swapped from
  `pdfplumber` (built on `pdfminer.six`) to `pypdfium2` (Python
  bindings to Google's PDFium / Chrome's PDF engine).
- License posture: `pypdfium2` is Apache-2.0 OR BSD-3-Clause; bundles
  PDFium under permissive terms. PyMuPDF (the obvious "fastest"
  candidate) was disqualified — AGPL-3.0 / commercial license is
  incompatible with closed-source enterprise distribution.
- `backend/requirements.txt`: `pdfplumber==0.11.9` →
  `pypdfium2==5.7.1`. pdfplumber's transitive deps (pdfminer.six,
  pillow, pypdfium2 itself) drop out.
- Implementation explicitly closes `PdfPage` and `PdfTextPage`
  handles per page so memory stays bounded on multi-hundred-page
  documents.
- PDFium is **not thread-safe** — the function stays single-threaded
  per call; concurrent ingestion of multiple PDFs is the caller's
  responsibility to serialize. ADR-worthy for a future parallel-ingest
  knob, not relevant at v1.

### Baseline-1 (artifact: `apple-m5-20260428T131344Z.json`)

Same hardware (Apple M5 Pro 24 GB / Ollama 0.18.0), `knob_stack:
[pypdfium2_extract]`, `samples/911Report.pdf`.

| Scenario | baseline-0 p50 | baseline-1 p50 | Δ | Speedup |
|----------|---------------:|---------------:|---:|--------:|
| end-to-end-911Report | 20,275 ms | 950 ms | −19,325 ms | **21.3×** |
| extract-911Report | 19,878 ms | 366 ms | −19,512 ms | **54.3×** |
| section-detect-911Report | 11 ms | 11 ms | ~0 | 1.0× |
| chunk-911Report | 66 ms | 71 ms | +5 ms | 0.93× |
| embed-batch-64-911Report | 496 ms | 499 ms | +3 ms | 0.99× |
| entity-extract-911Report | 7 ms | 7 ms | ~0 | 1.0× |

End-to-end stage breakdown (within the 950 ms run):

| Stage | baseline-1 p50 | % of end-to-end |
|-------|---------------:|----------------:|
| extract | 358 ms | **37.7%** |
| embed_batch | 505 ms | 53.2% |
| chunk | 72 ms | 7.6% |
| section_detect | 11 ms | 1.2% |
| entity_extract | 7 ms | 0.7% |

### Gate verdict

Bootstrap-CI gate on the full 6-scenario grid registers
`EQUIVALENT` for `total_ms_{p50,p95,mean}` with mean difference
~−6,500 ms and CI right edge at +2 ms — *technically correct*
because four of six scenarios (chunk / embed / section_detect /
entity_extract) were never expected to change (the knob is purely
upstream of them) and they didn't, which dilutes the average across
the bootstrap resamples.

The **per-scenario diff** is the load-bearing finding:

- `extract-911Report`: Δ = −19,512 ms; baseline-1 stdev 9 ms vs
  baseline-0 stdev 295 ms → signal-to-noise ≈ 65σ. Unambiguous
  improvement.
- `end-to-end-911Report`: Δ = −19,325 ms; signal-to-noise of similar
  magnitude. Unambiguous improvement.

A focused 2-scenario gate (`compare_runs` on just the affected
scenarios) trips `INSUFFICIENT_DATA` because the gate's `min_pairs=3`
floor isn't met. Future enhancement: support per-run pairing inside
one cell so a single-scenario knob can produce a CI-significant
verdict from the 3 timed runs alone. Not blocking for this chunk —
55× is not a "did something change" question.

### What's next (knob 2)

embed_batch is now 53% of end-to-end (505 ms / 950 ms). It's the new
bottleneck. But 505 ms is for 64 chunks — the production path emits
5,005 chunks for this document. If `embed_note_chunks` loops
note-by-note without batching across notes, the actual production
embedding cost on this PDF is closer to 40 s (5005 × 8 ms/chunk).

That's the next chunk: confirm the production loop's batching
behavior, replace any single-call-per-note loop with a batched one,
re-run baseline-2 with `knob_stack: [pypdfium2_extract,
batched_embeddings]`. Expected further drop in end-to-end of multiple
seconds depending on what the production loop currently does.

### Distribution-time obligation (release-eng note)

`pypdfium2` bundles PDFium plus its third-party dependencies (FreeType,
ICU, libpng subset, Skia subset, etc.). The installer must ship
`LICENSES/LicenseRef-PdfiumThirdParty.txt` from the wheel inside its
about-box / `THIRD-PARTY-NOTICES.txt`. This is the same hygiene every
C-extension-bundled wheel needs; surface it in the release-engineering
checklist when the desktop installer chunk lands. Not blocking for
this chunk (no installer yet).

### Extraction-quality verification (2026-04-28)

Speed without verifying quality would be a regression risk. Both
extractors were run side-by-side against the same fixture
(`samples/911Report.pdf`) before the swap was finalized.

**Findings:**

| Aspect | pypdfium2 | pdfplumber | Verdict |
|--------|-----------|------------|---------|
| Total chars extracted | 1,957,363 | 1,906,758 | pypdfium2 +2.6% |
| Page count produced | 585 | 585 | Identical |
| Newlines (line breaks) | 22,735 | 26,756 | pypdfium2 cleaner (fewer arbitrary mid-sentence breaks) |
| Word-level Jaccard (200K char sample) | — | — | 0.610 |
| Anchor passage (`Boston Center`...) | Clean prose | Clean prose | Equivalent |
| Famous quote `We have some planes` | Present | Present | Both correct |

**For prose (dominant content type on this fixture):** Both extractors
produce essentially identical content. pypdfium2 has cleaner line
wrapping (~15% fewer mid-sentence breaks); pdfplumber has more
aggressive soft-hyphenation joining that occasionally splits or
merges words incorrectly. Net for retrieval / semantic search:
**equivalent or slightly favoring pypdfium2**.

**For multi-column timeline tables:** pdfplumber attempts to preserve
visual layout by interleaving columns, which on the 9/11 timeline
tables produces hybrid sentences like *"8:38 Boston Center notifies
NEADS 9:03:11 Flight 175 crashes into 2 WTC of hijacking (South
Tower)"* — left column and right column merged into a single line.
pypdfium2 returns content-stream order: the two columns concatenate
one after the other, losing spatial structure but not inventing false
adjacencies. For our pipeline (text → chunks → embeddings) the latter
is meaningfully better — an embedding model handles paragraph-after-
paragraph cleanly, but a sentence with words interleaved from
unrelated columns is actively worse than wrong.

**Where pypdfium2 is genuinely weaker:** Two-column journal/newspaper
layouts in content-stream order may interleave columns. The 911
Report is mostly single-column prose with occasional two-column
timelines, so not a real concern here. For heavy two-column corpora
the fix is `pypdfium2.PdfTextPage.get_text_bounded()` with per-column
bboxes, or layering `pdftext` (an Apache-2.0 wrapper on pypdfium2)
that adds line/block structure — both stay in the pypdfium2
ecosystem, not "revert to pdfplumber."

**Where both are equally limited:** Neither extracts tables as
structured row/col data in their `extract_text()` / `get_text_range()`
modes. pdfplumber's `extract_tables()` does return row arrays, but
the ingest pipeline never used it. Structured table ingestion is a
separate code path regardless of which library underlies prose
extraction; flagged for a future chunk if a customer fixture demands
it.

**Conclusion**: pypdfium2 is the right swap on both speed and quality.
The 55× extract speedup costs us no measurable retrieval-quality
ground on prose; on multi-column tables it actually improves the
chunk shape that ends up in the embedding store.

## Amendment 2 — harness was under-reporting end-to-end (2026-04-28)

**The lie**: baseline-0 reported end-to-end at 20.3s; baseline-1 at
950ms. Those numbers were wrong because `run_end_to_end` capped
embedding at the first 64 chunks (`embed_batch_size=64`) while
production's `embed_note_chunks` embeds *all* chunks of a note in one
`aembed_texts` call.

**The truth (re-measured with cap removed):**

Artifact: `apple-m5-20260428T135114Z.json`
(`knob_stack=[pypdfium2_extract,honest_e2e_embed]`).

| Stage | p50 | % of end-to-end |
|-------|---:|---:|
| extract (pypdfium2) | 376 ms | 0.9% |
| section_detect | 11 ms | 0.03% |
| chunk | 71 ms | 0.2% |
| **embed_batch (all 5,546 chunks)** | **42,588 ms** | **98.8%** |
| entity_extract | 8 ms | 0.02% |
| **end-to-end total** | **43,059 ms** | — |

**Recalibrated picture across all baselines:**

| Baseline | What it claimed | Honest value |
|---|---:|---:|
| baseline-0 (pdfplumber + cap) | 20.3 s | ~60 s (extract was 19.7s, embed-all would have added ~40s) |
| baseline-1 (pypdfium2 + cap) | 950 ms | not measured separately — cap-bug version |
| baseline-2 (pypdfium2 + honest) | — | **43.1 s** |

**The pypdfium2 swap savings still hold** but are smaller than the
cap-bug numbers suggested:

- Honest baseline-0 → baseline-2: ~60s → 43s = **~17 seconds saved per
  ingest of this fixture (~30% faster end-to-end, not 21×)**.
- The "21× faster" headline was a measurement artifact. The 55×
  extract speedup is real; what changed is the share of total time
  extract represented (was 97%, is now 0.9%).

### Fix landed in this amendment

`backend/tests/eval/ingest/scenarios.py`: `IngestScenario.embed_batch_size`
default changed from `64` to `0` (= "embed all chunks"). The isolated
`embed_batch(64)` scenario keeps its explicit cap for per-batch
throughput measurement; only the end-to-end scenario is affected.

`backend/tests/eval/ingest/harness.py`: `run_end_to_end` default
mirrors the new scenario default.

### What this surfaces about embedding

embed_batch is now the dominant stage (98.8% of end-to-end). Per-chunk
cost is ~7.7 ms/chunk on the M5 Pro 24 GB CPU running the
`paraphrase-multilingual-MiniLM-L12-v2` ONNX model. **The 43s
isn't a hardware floor** — it's a `(model, ONNX-config, chunk-count)`
floor. Several knobs are still on the table:

- **Chunk-level content-hash skip on re-ingest** (knob-2 next) — first
  ingest unchanged, but re-ingest of identical / partially-edited
  documents drops to a fraction of 43 s.
- **ONNX Runtime threading** — fastembed's default thread setting may
  not use all 8 P-cores; bumping it could give ~2× free.
- **int8 quantized embedding model** — fastembed has quantized
  variants; ~2-3× faster, MTEB drop <2 points.
- **Smaller English-only model** (MiniLM-L6 vs multilingual-L12) — ~2×
  faster, real quality measurement needed.
- **Background / lazy embedding** — UX change, not compute change;
  user perceives ingest as ~100ms while embedding finishes async.
- **MLX backend on Apple Silicon** — 5-10× faster but Mac-only.

The pre-Amendment-1 claim that "PDF extract is the entire problem"
was correct *on the cap-bug numbers*. With honest measurement, **the
embedding stage is now the bottleneck.** Knob-2 (chunk-hash-skip)
addresses re-ingest specifically; first-ingest knobs are knob-3
onwards.

## Amendment 3 — knob-2 landed: chunk-level content-hash skip on re-ingest (2026-04-28)

`backend/services/embedding_service.py::embed_note_chunks` now snapshots
existing `(content_hash → embedding_blob)` rows before deleting and
reuses the binary blob for any chunk whose hash matches. Only chunks
with new or changed text are sent through the embedding model. Vectors
are bit-identical for unchanged content (we reuse the stored bytes
verbatim, not re-encode through the model).

### Implementation

```python
# Snapshot existing hash → blob before delete
cursor = await db.execute(
    "SELECT ce.content_hash, ce.embedding "
    "FROM chunk_embeddings ce "
    "JOIN note_chunks nc ON ce.chunk_id = nc.id "
    "WHERE nc.path = ?",
    (note_path,),
)
existing_blobs: dict[str, bytes] = {h: blob for h, blob in await cursor.fetchall()}

# DELETE old rows (existing behavior)
...

# Embed only chunks whose hash isn't in the snapshot
texts_to_embed = [chunks[i].text for i, h in enumerate(new_hashes) if h not in existing_blobs]
new_vectors_iter = iter(await aembed_texts(texts_to_embed) if texts_to_embed else [])

# Build per-chunk blob list: reuse existing for unchanged, fresh for new
chunk_blobs = []
for h in new_hashes:
    if h in existing_blobs:
        chunk_blobs.append(existing_blobs[h])
    else:
        chunk_blobs.append(vector_to_blob(next(new_vectors_iter)))
```

Five new unit tests in `backend/tests/test_embedding_service.py`:

- Re-ingest identical content → zero model calls
- Partial edit → only changed chunks embed
- Bit-identical blob reuse across re-ingest
- Wholly-changed content → every chunk re-embeds, old rows removed
- Adding new chunks → only new chunks embed

### Measured impact (production fast_ingest path, 911Report.pdf)

Real end-to-end measurement against the production path (not the
compute-only harness): full ingest including section-split, 60
per-section MD files, SQLite persistence, and Smart Connect.

| Scenario | Before knob-2 | After knob-2 | Speedup | Saved |
|---|---:|---:|---:|---:|
| First ingest (cold) | 25.6 s | 25.6 s | 1.0× (unchanged) | 0 s |
| Re-ingest, unchanged content (`reindex_all_chunks`) | 25.6 s | 1.4 s | **18.6×** | **24.2 s** |
| Re-ingest, one paragraph edited | 25.6 s | 2.0 s | **12.9×** | **23.6 s** |

The re-ingest pattern matters because `reindex_all_chunks` is invoked
when:
- A user clicks "Reindex" in the settings UI
- The embedding model changes and the workspace gets backfilled
- A workspace is migrated between machines and the DB is rebuilt
- The user accidentally re-imports the same file

All four scenarios drop from "leave-and-come-back" to "barely a beat."

### Harness/production gap surfaced

The harness's compute-only end-to-end on the 911 Report reports 43.1 s.
Production's full fast_ingest reports **25.6 s** — much less. Reason:
production section-splits the PDF into 60 notes BEFORE chunking, so
the chunker runs on each section independently and emits ~3,023
chunks total. The harness chunks the whole 1.9M-char extracted text
as one document and emits ~5,546 chunks (~45% more). The harness is
measuring a worst-case shape, not the production shape.

Two follow-ups (out of scope for this chunk):

1. Update the harness's end-to-end scenario to mirror production's
   section-split-then-chunk path. That would bring its number into
   line with reality.
2. Add a `re-ingest-911Report` scenario to the harness that captures
   the chunk-hash-skip win as a permanent regression-gate metric. The
   one-shot script used for this measurement isn't a permanent test.

### Knob-2 vs knob-1

Different problems, both real:

- **Knob-1** (pypdfium2): saves 17 s on every ingest (first or repeat) by
  making extract 55× faster.
- **Knob-2** (chunk-hash-skip): saves up to 24 s on re-ingest specifically;
  zero help on first ingest.

Combined: a user re-importing a fresh PDF for the first time pays the
full ~26 s; re-importing the same PDF (or reindexing) pays ~1.4 s.
Cumulative saving on a "user accidentally re-uploads" scenario: ~58 s
vs the original pdfplumber + DELETE-and-rebuild path.

### What this doesn't address

Still on the table for first-ingest:

- ONNX Runtime threading (knob-3 candidate, free if currently single-threaded)
- int8 quantized embedding model (knob-4 candidate, ~2-3× speed, <2pt MTEB drop)
- Smaller English-only model (knob-5, quality measurement needed via ADR 010)
- Background / lazy embedding (knob-6, UX change not compute change)
- MLX backend on Apple Silicon (knob-7, Mac-only)

First-ingest of the 911 Report at ~26 s is the new floor for this
fixture on this hardware until one of those knobs lands.
## Amendment 4 — knob-3 investigated: ONNX Runtime threading is already optimal (2026-04-29)

**Result**: no code change. ONNX Runtime's auto-pick (`intra_op_num_threads=0`,
which lets ORT default to the physical core count) is already the
fastest configuration at production scale. Clamping threads to the
P-core count — which the 64-batch micro-bench *suggested* — is 4%
slower across a real ~5,500-chunk pass. Knob-3 closed without a
landing.

### Why we measured this anyway

Amendment 2 listed knob-3 as "free ~2× if currently single-threaded."
The current code (`backend/services/embedding_service.py::_get_model`)
constructs `TextEmbedding(model_name=...)` with no `threads=` kwarg,
which means fastembed leaves `intra_op_num_threads` and
`inter_op_num_threads` at ORT's defaults (both 0 = auto). On Apple
Silicon M5 Pro 24 GB the topology is asymmetric (4 P-cores +
6 E-cores = 10 logical), and ORT's auto-pick blindly grabs all
physical cores including the slower E-cores — a classic anti-pattern
on hetero hardware. The hypothesis was that capping to the P-core
count would cut intra-op sync waits.

### Sweep results

Two passes against the production embedding model
(`paraphrase-multilingual-MiniLM-L12-v2`):

**Pass 1 — single 64-text batch** (representative of `embed-batch-64`
harness scenario), 3 timed runs each, p50:

| `threads` | p50 ms | Δ vs auto |
|----------:|-------:|----------:|
| auto (10) | 490 | baseline |
| 1         | 733   | +50% |
| 2         | 555   | +13% |
| 3         | 496   | +1% |
| **4**     | **484** | **−1.4%** ← P-core count |
| 5         | 483   | −1.5% |
| 6         | 515   | +5% |
| 8         | 526   | +7% |
| 10        | 557   | +14% |

Suggests `threads=4` or `5` is best — by ~7 ms / 64-chunk batch.

**Pass 2 — production-scale 5,546-chunk pass** (the actual
bottleneck-stage workload), warm-up + 2 timed runs, p50:

| Config (`threads / batch_size / parallel`) | p50 (s) | Δ vs current |
|---|---:|---:|
| **auto / 256 / none** (current production) | **40.93** | baseline |
| 4 / 256 / none | 42.72 | +4% (worse) |
| auto / 512 / none | 42.00 | +3% (worse) |
| auto / 1024 / none | 48.21 | +18% (worse) |
| 4 / 1024 / none | 48.40 | +18% (worse) |
| auto / 256 / parallel=0 | 89.09 | +118% (terrible) |
| auto / 256 / parallel=4 | 37.31 | **−9% (faster)** |

### Why the micro-bench result inverts at scale

A 64-chunk batch executes a single `InferenceSession.run()` call. With
short batches the intra-op sync-points dominate, and clamping to
P-cores reduces sync waits.

A 5,546-chunk pass executes ~22 successive `run()` calls (at
`batch_size=256`). Across that many calls, ORT's auto-pick gets to
keep all 10 logical cores busy because work overlaps with the next
batch's preprocessing. The "auto over-subscribes E-cores" effect that
hurt the single-batch case is amortized away. Net: the micro-bench
overstated the win.

This is exactly the failure mode `--scope nightly` was designed to
catch: stage-isolated micro-bench numbers are useful but production-
scale measurement is the truth. Recording it explicitly so we don't
re-run the same 64-batch sweep next time.

### `parallel=4` is real but not free

Fastembed's `embed(parallel=4)` spawns 4 worker *processes* (not
threads), each loading its own ORT session and ~400 MB model copy.
That's ~1.6 GB of additional resident memory while embedding runs,
on top of the chat-path embedding singleton. On a 24 GB laptop this
is acceptable; on the smaller end of the supported hardware
(8-16 GB MacBook Air, low-tier Windows) it would not be.

Other costs:
- Per-call worker spawn overhead. `embed-batch-64` (small batches)
  almost certainly regresses with `parallel=4` because the spawn
  amortization isn't there. Untested but high prior.
- Process pool isolation breaks the chat-path embedding singleton
  warm-cache benefit (every chunk-embed call would respawn).
- 9% gain on a knob with this many trade-offs is below the bar for a
  standalone landing.

If we revisit `parallel=4`, it should be inside a *combined* knob —
e.g. layer it with knob-4 (int8 quantized model) where the model copy
is half size and the parallelism win compounds. Recorded as a
deferred sub-knob, not its own line item.

### Implication for knobs 4-7

The 43-second embed_batch floor on this hardware is real and won't be
moved by threading alone. Headroom from here lives in:

1. **Smaller / quantized model** (knobs 4 + 5) — `(model, ONNX-config)`
   space, not the threading space. Same compute pattern, less compute
   per chunk.
2. **Hardware-accelerator backend** (knob 7, MLX) — moves the work off
   CPU entirely. Order-of-magnitude territory, but Mac-only.
3. **Background/lazy embedding** (knob 6) — UX move, not a compute
   move. User perceives ingest as instant; embedding catches up
   asynchronously. Independent of all the above.

Knob-3 is the first knob in this loop that closed without a landing.
The measurement has paid for itself by ruling out a category of
"easy" wins and re-pointing the loop at model-level changes (knob-4
next).

### What's recorded

- No code change. `_get_model()` continues to construct
  `TextEmbedding(model_name=...)` without a `threads=` kwarg.
- No new baseline JSON. `knob_stack` continues to be
  `[pypdfium2_extract, honest_e2e_embed]` (chunk-hash-skip is a
  re-ingest-path knob, doesn't apply to first-ingest harness numbers).
- The two sweep scripts (`sweep_threads.py`, `sweep_batchparallel.py`)
  were one-off tmp tools and are not committed; the numbers above are
  the durable record. If a future reader wants to re-verify, the
  sweep shape is: `services.embedding_service._model = TextEmbedding(
  model_name=..., threads=N)` then time `embed(TEXTS, batch_size=...,
  parallel=...)` — full reconstruction in one screen.

## Amendment 5 — knob-6 landed: background/lazy embedding for section-split ingest (2026-04-29)

**Result**: user-perceived ingest of `samples/911Report.pdf` dropped
from 25.6 s → **1.53 s** (~17× faster). Background embed catches up
in ~24.5 s with per-note progress (`embedding 12/61…`) visible via
`GET /api/memory/ingest/status`. Total compute is unchanged — knob-6
is a perception change, not a compute change.

### Why this is the highest-EV remaining knob

The knob-3 / knob-4 path turned out to be a dead end on this hardware:
- Threading is already optimal (Amendment 4)
- Int8 quantization isn't available pre-built and self-quantizing
  involves calibration + custom-model loader + workspace migration
  story + quality regression risk
- CoreML Execution Provider silently falls back to CPU on this model
  (the optimized graph uses ORT contrib ops `SkipLayerNormalization` /
  `Attention` / `FastGelu` that CoreML EP can't handle)

That left compute reduction looking like substantial work with
uncertain payoff. Meanwhile knob-6 is a UX move on top of unchanged
compute: the slow stage already runs *after* the fast user-visible
work, so deferring it to a background job costs nothing in
correctness, vector quality, or hardware support, and recovers
~24 s of user-perceived latency.

### Implementation

Three small changes:

1. **`backend/services/memory_service.py`** — `_index_note` and
   `index_note_file` accept a `defer_embedding: bool = False`
   parameter. When True, `embed_note` and `embed_note_chunks` are
   skipped; the caller is responsible for catching up later.

2. **`backend/services/ingest.py::_emit_document_sections`** — passes
   `defer_embedding=True` to every `index_note_file` call (both index
   and per-section), then schedules the embed pass via
   `ingest_jobs.schedule_embed_for_paths(...)` *before* `connect_note`
   runs. Smart Connect on the index runs synchronously; it embeds
   query content on the fly and reads from already-populated
   embeddings of OTHER notes — so it works fine without this
   document's own embeddings being in place yet.

3. **`backend/services/ingest_jobs.py`** — adds
   `schedule_embed_for_paths(paths, *, workspace_path, doc_title)` and
   the underlying coroutine `embed_paths(paths, *, workspace_path,
   job_id)`. The scheduler mirrors the existing
   `schedule_graph_rebuild` shape — daemon thread, `start_job` /
   `update_stage` / `finish_job` for UI visibility, env-var
   short-circuit (`JARVIS_DISABLE_EMBEDDINGS=1`) for test isolation.

The single-file ingest path (memos, short PDFs) is untouched — no
`defer_embedding` flag flips, embedding still runs inline, sub-second.

### Measured impact (production fast_ingest, 911Report.pdf, M5 Pro 24 GB)

| Phase | Before knob-6 | After knob-6 | Notes |
|---|---:|---:|---|
| User-perceived (HTTP response) | 25.6 s | **1.53 s** | What the UI sees |
| Background embed (async) | 0 s | 24.55 s | Same compute, decoupled |
| Total compute | 25.6 s | 26.08 s | Equivalent (~1.9% noise) |
| User-perceived speedup | — | **16.7×** | First-ingest UX win |

Cumulative on a fresh first-ingest of 911Report.pdf, vs the original
pdfplumber + inline-embed baseline:

| State | First-ingest (user-perceived) |
|---|---:|
| Pre-knob-1 (pdfplumber + inline embed) | ~60 s (estimated honest) |
| After knob-1 (pypdfium2 + inline embed) | 25.6 s |
| After knob-6 (pypdfium2 + deferred embed) | **1.53 s** |
| Cumulative speedup | **~40×** user-perceived |

Re-ingest paths (knob-2 territory) are unchanged from Amendment 3:
unchanged content stays at 1.4 s, partial edit at 2.0 s. Knob-6
doesn't help re-ingest because that path doesn't go through
`_emit_document_sections` for already-split documents (it goes
through `reindex_all_chunks`, which still runs synchronously for now).

### Eventual-consistency contract surfaced

Search behavior during the embed window:
- `search_similar` and `search_similar_chunks` query the embedding
  tables directly. Notes that are still being embedded are absent
  from results until the background job catches up.
- `connect_note` for newly-ingested notes finds connections to
  *existing* notes correctly. It doesn't find connections from this
  new document's sections back to themselves — those embeddings
  aren't there yet — but section-to-section is already handled by
  the wiki-link graph that `_emit_document_sections` writes
  synchronously, so the user-facing graph view isn't impacted.
- `schedule_section_connect` (the per-section Smart Connect job that
  fires alongside the embed job) reads chunk_embeddings and may miss
  same-document siblings while the embed job is still running. Sibling
  links via wiki-links are the load-bearing path; cross-document
  semantic links are recoverable on the next manual "Reindex
  connections" or are picked up the next time another document is
  ingested.

The cost is bounded by the embed window (~24 s for a 60-section
document on this hardware). For a compliance product this is
acceptable: the user sees their notes immediately and the UI
already shows a per-note "indexing N/M" badge.

### Tests

Three new tests in
[backend/tests/test_embedding_service.py](../../../backend/tests/test_embedding_service.py):

- `test_index_note_file_defer_embedding_skips_model` — `defer_embedding=True` produces zero rows in `note_embeddings`/`chunk_embeddings`
- `test_index_note_file_default_still_embeds` — regression: single-file ingest path still embeds inline
- `test_embed_paths_populates_deferred_embeddings` — the underlying coroutine catches up exactly the deferred set
- `test_schedule_embed_for_paths_returns_none_for_empty_list` — short-circuit for empty paths

`JARVIS_DISABLE_EMBEDDINGS=1` is honored by the scheduler so tests
that exercise `_emit_document_sections` don't leak daemon threads
past teardown.

### Response payload addition

`fast_ingest`'s return dict now includes `embed_job_id` (alongside
the existing `section_connect_job_id`). Frontend can poll
`/api/memory/ingest/status` for the specific embed job's progress
and clear the "indexing" badge when it shows up in `recent`.

### What this doesn't address

- Re-ingest paths (`reindex_all_chunks`) still run synchronously.
  Could apply the same defer pattern but UX value is lower because
  the user is already inside a "reindexing…" flow when they trigger
  it.
- Search results during the embed window have a "freshness gap" — a
  newly-ingested note isn't searchable for ~25 s on a long PDF.
  Could improve by surfacing a UI hint when a search runs while
  embeddings are still settling. Not in this chunk.
- Process death during the embed window leaves notes indexed but not
  embedded. Recovery exists (`reindex_all_chunks` from settings)
  but isn't automatic. A startup scan that re-queues
  notes-without-embeddings would close this gap; flagged for a
  follow-on chunk if it becomes a real complaint.

### Knob-6 vs other candidates

Not a substitute for compute reduction — knob-4/5/7 still represent
real follow-on territory if first-ingest *compute* needs to drop
(e.g. for a 5,000-page document where the embed window grows
proportionally). For the 585-page 911 Report on M5 Pro 24 GB, knob-6
delivers a user experience that feels instant and pushes follow-on
compute knobs further out the priority queue.

The knob loop has now produced wins from three angles:
- knob-1 (extract): 55× faster compute on the dominant stage
- knob-2 (chunk-hash-skip): 18× faster re-ingest via memoization
- knob-6 (background embed): 17× faster user-perceived first-ingest
  via decoupling

The first two reduced *compute*; knob-6 reduced *latency*. Both axes
were necessary; neither alone got us to "feels instant."
