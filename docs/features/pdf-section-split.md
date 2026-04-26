---
title: PDF Section Split
status: active
type: feature
sources:
  - backend/services/ingest.py
  - backend/services/graph_service/entity_edges.py
  - backend/services/graph_service/concepts.py
  - backend/tests/test_pdf_section_split.py
  - backend/tests/test_document_section_split.py
  - backend/tests/test_ingest_section_graph.py
  - backend/tests/test_entity_caps_scaling.py
  - backend/tests/test_concepts.py
depends_on:
  - memory
  - knowledge-graph
last_reviewed: 2026-04-26
---

# PDF Section Split

## Summary

When a PDF (or other long document) is ingested, this feature splits it into one index note plus N per-section notes rather than a single monolithic file. Each section note carries `parent`, `section_index`, and `source_type: section/pdf` in its frontmatter; the index note carries `document_type: pdf-document`. The motivation is graph density: a single-note PDF produces one hub with satellite entities and almost no bridges to other documents. A split PDF produces a cluster of linked notes, and entities shared across sections — the same author or organisation appearing in the introduction and the methods — collapse to shared graph nodes, creating real inter-document edges.

## How It Works

### Split trigger

`fast_ingest` in [`backend/services/ingest.py`](../../backend/services/ingest.py) checks two thresholds before deciding to split:

- Extracted text length ≥ `SECTION_SPLIT_MIN_CHARS` (30,000 characters, roughly 10–15 dense pages).
- At least `SECTION_SPLIT_MIN_HEADINGS` (4) top-level sections detected.

Both conditions must hold. A short PDF, or a long PDF with no heading structure, takes the existing single-file path unchanged.

The same machinery is extended to other formats (step 27d): `.txt` uses the same heuristic heading detector as PDF; `.md` uses ATX heading detection (`#`, `##`) that is aware of fenced code blocks; `.json` splits on top-level keys (dict) or on 50-item chunks (array); `.xml` is handled by `structured_ingest`.

### Heading detection

`_detect_pdf_sections` splits on lines and applies three heuristics in priority order:

1. **Strict numbered headings** (`1 Introduction`, `2.3 Methods`) — accepted without requiring blank lines around them, because pdfplumber output rarely preserves blank lines between paragraphs. The regex requires a 1–2 digit prefix and a Title-cased short title with no trailing period.
2. **All-caps headings** (`ABSTRACT`, `RELATED WORK`) — accepted only when surrounded by blank lines (or file boundaries).
3. **Title-case single lines** (≤ 12 words, no trailing period, not starting with a function word) — same blank-line requirement.

TOC entries are filtered by a trailing page-number pattern (`1.1 Publications 29`). When the same heading text appears more than once (running headers, TOC echo), only the last occurrence is kept so the real section body is not stolen by the TOC line. If `SECTION_SPLIT_MIN_HEADINGS` or more strict numbered headings are found, looser matches (all-caps, title-case) are dropped entirely to avoid false positives in body text.

A hard cap of `SECTION_SPLIT_MAX_SECTIONS = 60` prevents runaway false-positive explosions on pathological input.

### Output layout

For `hai-ai-index-report-2025.pdf` dropped into the `knowledge/` folder:

```
memory/knowledge/hai-ai-index-report-2025/
  index.md
  01-front-matter.md
  02-introduction.md
  03-research-and-development.md
  ...
```

The folder name is the slugified PDF stem. Section files are zero-padded and prefixed with their position. `_emit_document_sections` (the unified writer for all formats, with `_emit_pdf_sections` as a thin backwards-compatible wrapper) creates the directory via `_unique_dir`, writes each section file, writes the index, calls `index_note_file` on every file, and then calls `graph_service.ingest_note` on each section so graph nodes are created immediately — not deferred until a manual rebuild.

### Frontmatter written

**Index note (`index.md`)**:
```yaml
title: <PDF stem, humanised>
date: <today>
source: <original PDF path>
source_type: index/pdf
document_type: pdf-document
```
Body: a numbered list of `[[wiki-links]]` to each section. The bidirectional link resolver in `graph_service/builder.py` automatically generates reverse edges from sections back to the index.

**Each section note**:
```yaml
title: <section heading verbatim>
date: <today>
source: <original PDF path>
parent: knowledge/hai-ai-index-report-2025/index.md
section_index: 3
source_type: section/pdf
```
Optional fields `section_type` and `section_type_confidence` are added when the document classifier (step 28d) is available.

### Length-scaled entity caps (step 27b)

The graph service in [`backend/services/graph_service/entity_edges.py`](../../backend/services/graph_service/entity_edges.py) previously used fixed caps (50 persons, 50 orgs, 25 projects, 25 places per note). These were sized for hand-written notes under 2 KB; for a 15 KB section of an academic paper they silently drop the long tail of mentioned entities — the exact entities that bridge sections.

`compute_caps(body_len)` scales linearly from base caps at 2 KB to hard caps (200/200/100/100) at 40 KB, clamped at both ends. `compute_co_mention_cap` applies the same linear scale, going from 100 to 400 co-mention pairs. The function signature of `apply_extracted_entities()` does not change; caps are computed once at its entry point from `len(body)`.

### Concept pass improvements (step 27c)

`graph_service/concepts.py` runs TF-IDF across all notes to produce cross-document `concept:` nodes. With a single monolithic PDF note, TF-IDF sees one document and produces nothing useful. After the split, it sees N section notes from the same paper, giving it enough signal to identify terms that recur across sections and bridge them to other documents.

Step 27c tightens the concept pass for mixed Polish/English corpora:

- **Hyphenation repair**: `_repair_hyphenation` joins `imple-\nmentation` before tokenisation.
- **Polish suffix folding**: `_fold_pl` strips common inflectional suffixes from tokens that contain Polish diacritics, collapsing `modele`/`modeli`/`modelach` to a single stem. English tokens are not touched.
- **Expanded stopwords**: citation artefacts (`et`, `al`, `arxiv`, `doi`, `fig`) and connectives (`however`, `therefore`) are added to both the English and Polish stopword sets.
- **Adjacency-required bigrams**: a bigram is only kept if it appears adjacently two or more times in the same note, filtering random adjacency from running text.

## Key Files

- [`backend/services/ingest.py`](../../backend/services/ingest.py) — all split logic: `_detect_pdf_sections`, `_detect_markdown_sections`, `_detect_json_sections`, `_emit_document_sections`, and the branch in `fast_ingest` that routes to them.
- [`backend/services/graph_service/entity_edges.py`](../../backend/services/graph_service/entity_edges.py) — `compute_caps` and `compute_co_mention_cap` for length-scaled entity extraction.
- [`backend/services/graph_service/concepts.py`](../../backend/services/graph_service/concepts.py) — TF-IDF concept pass with hyphenation repair, Polish folding, and adjacency-required bigrams.
- [`backend/tests/test_pdf_section_split.py`](../../backend/tests/test_pdf_section_split.py) — unit tests for heading detection and section emission, including idempotency and pdfplumber-style continuous text.
- [`backend/tests/test_document_section_split.py`](../../backend/tests/test_document_section_split.py) — end-to-end ingest tests for JSON, Markdown, TXT, and XML formats.
- [`backend/tests/test_ingest_section_graph.py`](../../backend/tests/test_ingest_section_graph.py) — verifies that every section note, not just the index, is present in the graph immediately after ingest (step 28b regression guard).
- [`backend/tests/test_entity_caps_scaling.py`](../../backend/tests/test_entity_caps_scaling.py) — monotonicity and clamping tests for `compute_caps`.
- [`backend/tests/test_concepts.py`](../../backend/tests/test_concepts.py) — tests for hyphenation repair, Polish folding, stopword filtering, and bigram adjacency requirement.

## Gotchas

**Small PDFs are not split.** A PDF under 30,000 extracted characters, or one whose text does not contain at least 4 detectable top-level headings, goes through the existing single-file path unchanged. No folder is created, no frontmatter with `parent`/`section_index` is written. Code that consumes these frontmatter fields must not assume they are present on every note.

**Non-PDF formats.** The split is format-agnostic at the emission layer (`_emit_document_sections`). The `doc_type` parameter drives only the `document_type` and `source_type` values in frontmatter (`pdf-document`, `json-document`, etc.). Consumers that key on `document_type: pdf-document` will miss split Markdown or JSON documents; prefer checking `source_type` prefix `index/` or `section/` when the logic applies to all split documents.

**Idempotency on re-ingest.** Re-ingesting the same PDF does not overwrite an existing split. `_unique_dir` appends a numeric suffix (`report-1/`, `report-2/`), matching the existing behaviour for single-file re-ingest. There is no deduplication based on content — if the user re-ingests the same file, they get a second copy. The user must delete the original folder manually if they want a clean replacement.

**TOC false positives.** Numbered lines that look like headings but are table-of-contents entries are filtered by a trailing page-number pattern. However, this is a heuristic; non-standard TOC formatting in some PDFs may still leak through. The hard cap of 60 sections prevents runaway output in the worst case.

**Graph registration is synchronous at ingest time.** Each section is registered via `graph_service.ingest_note` during `_emit_document_sections`. A failure in any individual section logs a warning but does not abort the overall ingest. If the graph call silently fails for a section, that section will be missing from graph-expansion queries until the next full graph rebuild.

**Smart Connect runs on the index only.** `connect_note` is called once on the index note. Per-section cross-document Smart Connect is scheduled as a background job (`schedule_section_connect`) so the ingest response stays fast. Until that job completes, sections will have graph edges from their entity/concept extraction but not from the LLM-based Smart Connect similarity pass.
