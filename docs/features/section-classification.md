---
title: Section Classification
status: active
type: feature
sources:
  - backend/services/document_classifier.py
  - backend/services/retrieval/intent_parser.py
  - backend/services/retrieval/pipeline.py
  - backend/services/ingest.py
depends_on:
  - retrieval
  - memory
last_reviewed: 2026-04-26
---

# Section Classification

## Summary

Every section note produced by the document split (step 27) is tagged with a `section_type` field (e.g. `risks`, `requirements`, `integrations`) and a confidence score stored directly in frontmatter. The retrieval pipeline reads these tags to boost matching candidates when the user's query signals a typed intent — "what risks does this RFP mention?" — without filtering out untagged or mismatched sections so recall is preserved.

## How It Works

### Stage 1 — Heuristic classifier

[`document_classifier.py`](../../backend/services/document_classifier.py) runs a pure-Python weighted keyword scan over the concatenated section title and body. Each taxonomy type has a keyword table with per-keyword weights (`"shall"` = 0.8, `"ryzyko"` = 0.8, etc.). Raw scores are normalized to [0, 1] against the highest-scoring type in that section, then a heading prior is applied: if the title matches a pattern like `^Ryzyka?$` or `^Requirements?$`, 0.4 is added to that type's score (capped at 1.0). The heuristic accepts a result only when the top score is ≥ 0.6 **and** its margin over second place is ≥ 0.15. If either condition fails, Stage 1 returns `"other"` and defers to Stage 2.

Sections with fewer than 10 words always return `"other"` with confidence 0.0 — too short to trust keyword density.

### Stage 2 — LLM fallback

When Stage 1 returns `"other"` or confidence is below threshold, `classify_section_llm` sends the section heading and the first 500 characters of body to Claude Haiku with a constrained prompt listing the fixed taxonomy labels. The model is asked to respond with exactly one label. The response is normalized and matched against `SECTION_TYPES`; a fuzzy prefix match is used as a fallback if the model returns a near-miss. Confidence is fixed at 0.75 for an exact match, 0.70 for a prefix match, 0.5 for an unrecognized fallback. Any exception returns `("other", 0.0)` silently.

During live ingest (`_emit_document_sections` in [`ingest.py`](../../backend/services/ingest.py)), **only Stage 1 runs**. The async LLM stage is skipped on the hot path to keep ingest fast. The backfill script `backend/scripts/classify_existing_sections.py` handles LLM classification for sections that Stage 1 left as `"other"`.

### Where confidence comes from

The heuristic confidence is the normalized top score after the heading prior is applied — it reflects how dominant the winning type's keyword signal was relative to all other types in the same section. The LLM path returns a fixed confidence tier (0.75/0.70/0.50) because the model output is a point prediction with no probability exposed at the `max_tokens=16` budget.

### Frontmatter written

`_make_section_frontmatter` in [`ingest.py`](../../backend/services/ingest.py) adds two optional fields:

```yaml
section_type: risks
section_type_confidence: 0.84
```

Both fields are omitted entirely if the classifier is unavailable or Stage 1 returns `"other"` during ingest. Non-PDF section notes (markdown, text, JSON splits) go through the same `_emit_document_sections` path and receive the same classification treatment.

### Retrieval consumption

[`intent_parser.py`](../../backend/services/retrieval/intent_parser.py) runs a deterministic regex scan over the query text (`_detect_preferred_section_types`) and populates `QueryIntent.preferred_section_types` — a list that may contain zero, one, or multiple type labels. The patterns cover both English and Polish phrasing: `"what risks"` and `"jakie ryzyka"` both map to `["risks"]`; `"what are the security requirements"` may return both `"security"` and `"requirements"`.

In [`pipeline.py`](../../backend/services/retrieval/pipeline.py), `_compute_post_fusion_boost` adds `BOOST_SECTION_TYPE = 0.10` to a candidate's fused score when its `section_type` is in `preferred_section_types`. The boost is additive with other post-fusion boosts (explicit Jira key match, sprint) and capped at `BOOST_CAP = 0.40`. The section-type boost is **not** gated on the `JARVIS_FEATURE_JIRA_RETRIEVAL` flag — it fires for any query with typed intent regardless of whether Jira retrieval is enabled.

Candidates without a `section_type` field (pre-step-28d ingests, non-split documents) receive no boost and are never excluded.

## Key Files

| File | Role |
|---|---|
| [`backend/services/document_classifier.py`](../../backend/services/document_classifier.py) | Heuristic and LLM classifiers; defines the fixed 12-type taxonomy and keyword/heading-prior tables |
| [`backend/services/ingest.py`](../../backend/services/ingest.py) | Calls Stage 1 during `_emit_document_sections`; writes `section_type` and `section_type_confidence` to each section note's frontmatter |
| [`backend/services/retrieval/intent_parser.py`](../../backend/services/retrieval/intent_parser.py) | Regex patterns that map query phrases (EN + PL) to `preferred_section_types` on `QueryIntent` |
| [`backend/services/retrieval/pipeline.py`](../../backend/services/retrieval/pipeline.py) | Post-fusion boost logic; exports `BOOST_SECTION_TYPE`, `BOOST_CAP`, and `_compute_post_fusion_boost` |

## Gotchas

**PL/EN keyword coverage is uneven by design.** High-signal Polish terms are present for every type (e.g. `"harmonogram"` for timeline, `"wycena"` for pricing), but some lower-weight English terms have no Polish equivalent in the tables (`"sprint"`, `"milestone"`). Sections from heavily Polish-only bodies may score lower on Stage 1 than equivalent English content and fall through to the LLM more often. The heading prior partially compensates — a Polish heading like `"Harmonogram"` adds 0.4 to the timeline score regardless of keyword density.

**The 0.6 / 0.15 thresholds are hard-coded constants** (`_CONFIDENCE_ACCEPT`, `_MARGIN_ACCEPT`) in `document_classifier.py`. A section with two nearly equal signals (e.g. a "Security Requirements" section) will always fall through to Stage 2 because the margin condition fails. That is intentional — ambiguous sections should not be forced into a single type by a narrow heuristic win.

**Non-PDF sources do not receive LLM classification at ingest time.** All format paths (PDF, text, Markdown, JSON) share `_emit_document_sections`, which only runs Stage 1. The LLM fallback must be applied via the backfill script. Sections classified as `"other"` by Stage 1 will have no `section_type` field in frontmatter until the script runs.

**The intent parser can return multiple types for a single query.** A query like `"what are the security requirements?"` may populate `preferred_section_types` with both `"security"` and `"requirements"`. Each matching candidate gets the same flat `BOOST_SECTION_TYPE = 0.10` boost — there is no multiplier for matching both types simultaneously.

**`section_type` is never written to the graph.** Adding it as a node type would recreate the hub-node anti-pattern removed in step 27. The field exists only in Markdown frontmatter and in the SQLite notes index. The graph is not affected by classification.

**LLM response parsing is tolerant but not exhaustive.** If the model returns a value that neither exactly matches nor prefix-matches any `SECTION_TYPES` entry, the result is silently downgraded to `("other", 0.5)`. The `signals` dict from Stage 1 (per-type raw scores) is logged but never written to frontmatter.
