---
title: Client Estimator Specialist
status: active
type: feature
sources:
  - backend/tests/test_client_estimator_bootstrap.py
  - backend/tests/test_executor_section_type_passthrough.py
  - backend/services/specialist_service.py
  - backend/services/tools/executor.py
depends_on:
  - specialists
  - section-classification
  - retrieval
last_reviewed: 2026-04-26
---

# Client Estimator Specialist

## Summary

Client Estimator is a built-in specialist that reads client RFPs and discovery materials from workspace memory and produces a structured 11-section Markdown estimate brief — Executive Summary through Recommended Next Step — saved as a real note in `memory/plans/`. It is the first business workflow built on top of section-typed retrieval: by routing its searches through the hybrid retrieval pipeline instead of plain memory search, it preferentially surfaces the workspace sections most relevant to each part of the brief (risks sections for Risks, requirements sections for Functional Scope, and so on).

## How It Works

### Specialist configuration

The specialist is defined inline in [`backend/services/specialist_service.py`](../../backend/services/specialist_service.py) as part of `_BUILTIN_SPECIALISTS`. On workspace bootstrap, `seed_builtin_specialists` writes it to `agents/client-estimator.json` if the file does not already exist. The configuration is idempotent: if the file exists, only missing or empty keys are backfilled — user edits to any field are preserved.

The specialist's tool allowlist is `["search_notes", "read_note", "query_graph", "write_note"]`. It has no sources glob (it searches the whole workspace) and no Jira tools.

### Retrieval strategy

When `specialist_id="client-estimator"` is passed to `execute_tool`, the `search_notes` branch skips `memory_service.list_notes` and calls `services.retrieval.pipeline.retrieve` directly. The pipeline applies the section-type boost mechanism introduced in step 28d, so sections whose `section_type` frontmatter matches the semantic intent of the query rank higher. For all other specialists (or no specialist), the original `memory_service` path is used unchanged — the change is a two-line conditional in [`backend/services/tools/executor.py`](../../backend/services/tools/executor.py).

### Output structure

The system prompt instructs the model to produce exactly this heading sequence:

1. Executive Summary
2. Business Goal (draws from `section_type=business_goals`)
3. Functional Scope (from `requirements`)
4. Technical Scope (from `technical_constraints`)
5. Integrations (from `integrations`)
6. Risks (from `risks`)
7. Assumptions
8. Open Questions (from `open_questions` plus anything unresolvable)
9. Suggested MVP (synthesis, 5 bullets max)
10. Estimate Buckets (S/M/L/XL per work area — no person-days unless the source states them)
11. Recommended Next Step

Every claim must include a `[[wiki-link]]` citation to the source section. If no source covers a heading the model must write `NOT IN SOURCES` — fabrication is prohibited by both the system prompt and an explicit rule. The final note is saved via `write_note` to `memory/plans/<slug>-estimate.md`, which makes it a retrievable memory note and a graph node with edges to every cited section.

### Bootstrap and dismissal

`seed_builtin_specialists` in [`backend/services/specialist_service.py`](../../backend/services/specialist_service.py) handles three cases:

- **Fresh workspace** — file is written from the in-memory definition.
- **File already exists** — only empty or absent keys are backfilled; nothing is overwritten.
- **User previously deleted it** — `delete_specialist` calls `_mark_specialist_dismissed`, which writes `"client-estimator"` to `dismissed_specialists` in `app/config.json`. Seed reads this list and skips re-creation permanently.

## Key Files

- [`backend/services/specialist_service.py`](../../backend/services/specialist_service.py) — Defines `_CLIENT_ESTIMATOR_SYSTEM_PROMPT` and the `client-estimator` entry in `_BUILTIN_SPECIALISTS`; `seed_builtin_specialists` and `_mark_specialist_dismissed` handle bootstrap and dismissal.
- [`backend/services/tools/executor.py`](../../backend/services/tools/executor.py) — Conditional in `execute_tool` that routes `search_notes` to `retrieval.pipeline.retrieve` when `specialist_id == "client-estimator"`.
- [`backend/tests/test_client_estimator_bootstrap.py`](../../backend/tests/test_client_estimator_bootstrap.py) — Covers fresh creation, no-overwrite on existing file, and dismissed-specialist skip.
- [`backend/tests/test_executor_section_type_passthrough.py`](../../backend/tests/test_executor_section_type_passthrough.py) — Verifies the pipeline is called for `client-estimator` and that other specialists are unaffected.

## Gotchas

**Manual activation only.** Client Estimator does not self-activate and is not auto-routed based on message content. The user must select it from the specialist sidebar, the same way as any other specialist. There is no trigger word or intent detection that switches it on automatically.

**Depends on `section_type` frontmatter.** The retrieval boost only works if workspace documents have been split and classified (step 28d). On a workspace where sections carry no `section_type` frontmatter, the pipeline still runs but the boost is a no-op — the specialist falls back to plain BM25/embedding ranking. It will not crash, but brief quality degrades because sections relevant to e.g. Risks may not surface at the top.

**No PDFs ingested means thin or empty output.** Client Estimator has no fallback content source. If the workspace contains no ingested client documents, every section of the brief will read `NOT IN SOURCES`. This is intentional and correct behaviour per the system prompt's anti-fabrication rule — it should not be mistaken for a bug.

**Estimate Buckets are S/M/L/XL only.** The system prompt explicitly prohibits the model from inventing person-day estimates unless the source document itself states them. If a stakeholder expects numeric estimates, they need to provide them in the source materials first.

**Dismissal is permanent until `app/config.json` is edited manually.** Once a user deletes the specialist, `dismissed_specialists` in `app/config.json` prevents any future workspace bootstrap from recreating it. There is no UI to undo a dismissal — the entry must be removed from the JSON file by hand.
