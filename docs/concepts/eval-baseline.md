---
title: Eval Baseline
status: active
type: concept
sources:
  - backend/tests/eval/runner.py
  - backend/tests/eval/queries.py
  - backend/tests/eval/queries_reference.py
  - backend/tests/eval/setup_reference.py
  - backend/tests/eval/test_eval_baseline.py
  - backend/tests/eval/test_baseline_floor.py
  - backend/tests/eval/test_runner_aggregation.py
  - backend/tests/eval/corpus.py
depends_on:
  - retrieval
  - pdf-section-split
last_reviewed: 2026-04-26
---

# Eval Baseline

## Summary

The eval baseline is a frozen retrieval benchmark: 33 hand-authored queries run against section Markdowns derived from four reference PDFs (HAI AI Index 2025, OWASP LLM Top-10, NIST AI RMF, Survey of Large Language Models), producing Recall@5, MRR, and Precision@K numbers that are committed to git. Every change that touches retrieval, indexing, or context assembly must be measured against those numbers — not eyeballed. Without a frozen baseline, claims like "classification improved recall by 12%" are not falsifiable across commits.

## How it works

### Queries to metrics

[`queries_reference.py`](../../backend/tests/eval/queries_reference.py) defines 33 queries across six buckets: `factual` (8), `cross_doc` (6), `section_typed` (8), `polish` (4), `numerical` (4), and `client_estimate` (3). Each query carries `expected_paths` (the section Markdown files that should appear in top-5 results) and a `min_recall` floor that can be set lower for queries that are intentionally harder (e.g., cross-lingual Polish queries get `min_recall: 0.0`).

[`runner.py`](../../backend/tests/eval/runner.py) drives the evaluation: it calls `retrieval.retrieve` for each query, computes Recall (hit count / expected count), MRR (reciprocal rank of the first expected hit), and Precision@K (hits / retrieved). It also estimates the token budget consumed by the assembled context using the same `len(text) // 4` heuristic that `context_builder` uses, so the field is consistent with what the model actually sees.

The runner produces four output keys:
- `overall` — mean Recall/MRR/Precision across all queries
- `by_type` — the same metrics broken down per bucket, with sorted keys so `git diff` is clean
- `per_query` — a stable-key dict (query text truncated to 60 chars, lowercased, spaces → underscores) used for baseline comparison
- `details` — full per-query result objects including retrieved and expected paths

### Baseline freeze and regression gate

The baseline JSON lives at [`backend/tests/eval/baselines/step-28c.json`](../../backend/tests/eval/baselines/step-28c.json). It was produced by running the harness once against a clean re-ingest of the four PDFs and committing the result. Because `per_query` keys are sorted and the JSON uses stable key order, the diff between two runs is readable.

[`test_baseline_floor.py`](../../backend/tests/eval/test_baseline_floor.py) enforces three assertions:
1. Per-query recall must not drop more than **5%** below the baseline value for any individual query.
2. Overall mean Recall@5 must not drop **at all** (zero tolerance on the aggregate).
3. Mean Recall@5 must remain **≥ 0.55**, the absolute floor set at Step 28c.

The test skips unless `JARVIS_EVAL_FLOOR=1` is set. It also skips if the baseline JSON or reference workspace fixture is absent. This makes it safe in ordinary CI (which lacks the reference corpus) while functioning as a hard pre-merge gate for retrieval changes.

### Reference workspace setup

The four PDFs are not checked in. Instead, [`fixtures/reference_workspace/memory/`](../../backend/tests/eval/fixtures/reference_workspace/) contains the pre-ingested section Markdowns — deterministic outputs of `_emit_pdf_sections` from Step 27 — which are committed and amount to roughly 1–2 MB. [`fixtures/reference_pdfs.json`](../../backend/tests/eval/fixtures/reference_pdfs.json) holds the SHA-256 + download URL for each source PDF so the originals can be verified if needed.

[`setup_reference.py`](../../backend/tests/eval/setup_reference.py) has three modes: build a workspace from the fixture Markdowns (default, always works), verify SHA-256 of locally downloaded PDFs against the manifest (`--verify-shas`), and record new SHAs into the manifest (`--update-shas`). When building the workspace it copies the Markdown tree, initialises the SQLite index from scratch via `init_database`, and re-indexes every file through `index_note_file` — confirming that the fixture is the canonical source, not the database.

## Key files

| File | Role |
|------|------|
| [`backend/tests/eval/runner.py`](../../backend/tests/eval/runner.py) | Runs queries against `retrieval.retrieve`, computes all three metrics, emits stable-key JSON output |
| [`backend/tests/eval/queries_reference.py`](../../backend/tests/eval/queries_reference.py) | 33 reference queries with expected paths, per-query `min_recall` floors, and bucket labels |
| [`backend/tests/eval/queries.py`](../../backend/tests/eval/queries.py) | Synthetic queries for the hand-written corpus (keyword/semantic/relational/temporal — not the regression gate) |
| [`backend/tests/eval/test_baseline_floor.py`](../../backend/tests/eval/test_baseline_floor.py) | Opt-in pytest that enforces no-regression against `baselines/step-28c.json`; skips silently when env var is absent |
| [`backend/tests/eval/test_eval_baseline.py`](../../backend/tests/eval/test_eval_baseline.py) | Always-on tests that validate the synthetic corpus structure and runner output shape |
| [`backend/tests/eval/test_runner_aggregation.py`](../../backend/tests/eval/test_runner_aggregation.py) | Unit tests for `by_type` aggregation, empty buckets, and stable key order |
| [`backend/tests/eval/setup_reference.py`](../../backend/tests/eval/setup_reference.py) | Bootstrap script to build the reference workspace from fixture Markdowns or verify PDF SHAs |
| [`backend/tests/eval/corpus.py`](../../backend/tests/eval/corpus.py) | Synthetic in-memory corpus (hand-written notes) used by the always-on tests only |
| `backend/tests/eval/baselines/step-28c.json` | Committed baseline numbers; git diff of this file shows retrieval changes across PRs |
| `backend/tests/eval/fixtures/reference_workspace/` | Pre-ingested section Markdowns (~1–2 MB); the source of truth for all reference eval runs |
| `backend/tests/eval/fixtures/reference_pdfs.json` | SHA-256 + URL manifest for the four source PDFs (PDFs themselves are not in the repo) |

## Gotchas

**Two corpora, two purposes.** [`corpus.py`](../../backend/tests/eval/corpus.py) and [`queries.py`](../../backend/tests/eval/queries.py) are a synthetic hand-written set (personal notes about Portugal, health, a website redesign). They exist to give the always-on unit tests a fast, self-contained corpus. They do not gate retrieval quality. The regression gate uses only `queries_reference.py` against the reference workspace.

**Updating the baseline is a deliberate act.** The baseline JSON is meant to move forward, never backward. After a change that genuinely improves retrieval (e.g., Step 28d classification), re-run `run_eval.py`, review the diff table, and commit the new JSON. Do not update the baseline to make a failing test pass.

**The fixture Markdowns, not the PDFs, are the reproducible unit.** The PDFs are large, copyrighted, and not in the repo. The committed Markdowns are the output of a specific Step 27 ingest and represent a fixed point in the pipeline. If section splitting logic changes, the fixtures need to be regenerated and re-committed.

**`min_recall: 0.0` does not mean the query is ignored.** Queries with a zero floor (e.g., the Polish investment query) still appear in per-query output and contribute to the overall mean. They are marked zero because they are known-hard; a future step may raise the floor once cross-lingual retrieval improves.

**The floor test is not a CI unit test.** Running it requires the reference workspace fixture to be present and `JARVIS_EVAL_FLOOR=1` to be set. It is expected to pass immediately after the baseline is recorded, and to be run locally before merging any retrieval-affecting change.
