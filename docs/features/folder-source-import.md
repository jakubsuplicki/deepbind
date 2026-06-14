---
title: Folder Source Import
status: review-ready
type: feature
sources:
  - backend/main.py
  - backend/models/database.py
  - backend/routers/source_import.py
  - backend/routers/chat.py
  - backend/services/context_builder.py
  - backend/services/retrieval/pipeline.py
  - backend/services/ingest.py
  - backend/services/system_prompt.py
  - backend/services/structured_ingest.py
  - backend/services/source_import/__init__.py
  - backend/services/source_import/archives.py
  - backend/services/source_import/cancellation.py
  - backend/services/source_import/cloud_placeholders.py
  - backend/services/source_import/dedupe.py
  - backend/services/source_import/extractors.py
  - backend/services/source_import/grants.py
  - backend/services/source_import/limits.py
  - backend/services/source_import/manifest.py
  - backend/services/source_import/models.py
  - backend/services/source_import/removal.py
  - backend/services/source_import/rescan.py
  - backend/services/source_import/scan.py
  - backend/services/source_import/selection.py
  - backend/services/source_import/store.py
  - backend/services/source_import/worker.py
  - backend/tests/test_ingest_service.py
  - backend/tests/test_source_import_extractors.py
  - backend/tests/test_source_import_scan.py
  - desktop/src-tauri/Cargo.toml
  - desktop/src-tauri/tauri.conf.json
  - desktop/src-tauri/src/lib.rs
  - desktop/src-tauri/sample-data/deepfiles-demo-folder/README.md
  - desktop/src-tauri/sample-data-src/handover-pack/data-dictionary.md
  - desktop/scripts/dev.sh
  - frontend/app/composables/useChat.ts
  - frontend/app/composables/useSourceImport.ts
  - frontend/app/composables/useFolderSourceImportDialog.ts
  - frontend/app/pages/main.vue
  - frontend/app/pages/memory.vue
  - frontend/app/components/ImportDialog.vue
  - frontend/app/components/FolderSourceImportDetail.vue
  - frontend/tests/composables/useChat.test.ts
  - frontend/tests/components/ImportDialog.test.ts
  - frontend/tests/pages/memory.test.ts
depends_on: [memory, pdf-section-split, smart-connect, retrieval-trace, desktop-shell-graduation]
last_reviewed: 2026-05-18
last_updated: 2026-06-14
---

# Folder Source Import

## Summary

Folder Source Import is the user-facing import flow for local and synced business folders. It lets a non-developer choose a folder, review a metadata-only inventory, approve exactly what DeepFilesAI may read, and then turn approved files into local Markdown memory with progress, evidence, and recovery controls.

This feature complements the existing single-file upload path documented in [memory.md](memory.md). The durable product rule is still the same: Markdown under `memory/` is the source of truth, while SQLite, embeddings, chunks, graph edges, suggestions, and import manifests are derived or operational layers. The implementation is review-ready; packaged desktop validation should follow the [Folder Source Import Demo Smoke](../runbooks/folder-source-import-demo-smoke.md) runbook.

## Definition of Done

- User can choose a local folder, mounted share, synced cloud folder, ZIP archive, or sample dataset from the desktop app.
- Backend scans only local sources granted by the trusted desktop picker flow.
- Metadata scan reads only names, extensions, sizes, modified times, and folder structure before approval.
- Review screen shows supported, skipped, unsupported, duplicate, warning, and estimated import counts.
- Scan issues can be copied as a relative-path issue report for IT/admin help without exposing the absolute local source root.
- User can exclude folders, file types, and selected files before import.
- Import supports existing memory ingest types plus DOCX, XLSX, PPTX, HTML/HTM, RTF, EML, and ZIP containers.
- Batch and per-file progress are visible in plain language.
- Imported knowledge lands as Markdown under `memory/imports/<source-slug>/`.
- Import manifest supports remove import, explicit re-import/rescan, duplicate handling, and skipped-file review.
- Recent import history lets the user reopen prior source imports and reuse completion, skipped-file, rescan, and remove controls.
- Completion can open the imported notes back in Memory with the imported folder active.
- Removing an import deletes only notes created by that import batch and refreshes derived indexes.
- Suggested questions and "Ask about this import" scope retrieval to the current import batch.
- App shutdown, cancellation, folder-name collisions, and hard limits have explicit user-visible behavior.
- No cloud connector, OAuth flow, telemetry, or network dependency is introduced.
- Tests cover scan consent, import lifecycle, extractor fixtures, archive guards, and frontend review/progress states.
- Manual desktop smoke is tracked in [Folder Source Import Demo Smoke](../runbooks/folder-source-import-demo-smoke.md).

## Non-goals

- No Microsoft Graph, Google Drive API, Dropbox API, OAuth, or cloud-storage connector in this slice.
- No continuous sync watcher. Re-import is explicit.
- No OCR for scanned PDFs or images.
- No audio/video transcription.
- No legacy binary Office parsing for `.doc`, `.xls`, or `.ppt` in the first slice.
- No hidden background import of every file in a selected folder without review.
- No formal compliance claim. The feature supports a local-first privacy posture, not certification.

## Implementation Status

Step 29a and 29b are implemented as the first reviewable slice: the desktop shell can grant a native folder selection to the backend, the backend rejects untrusted raw scan paths, and the import dialog can show a metadata-only folder inventory with exclude-by-file, exclude-by-type, and exclude-by-folder review controls. The backend now creates a temporary approved-file selection ID from those exclusion rules.

Step 29c now has a first approved-import path. Reusable business document extractors are wired into `fast_ingest`, folder scans count DOCX, XLSX, PPTX, HTML/HTM, RTF, EML, and ZIP-derived child files as supported, and `POST /api/source-import/scans/{scan_id}/start` creates a SQLite-backed import manifest before a background worker reads only the approved files. Imported notes receive safe source-relative provenance, destination folders get a short batch disambiguator, and duplicate files inside the batch are skipped by content hash after approval.

Step 29d now has its first lifecycle controls. Active imports can be cancelled through `POST /api/source-import/imports/{batch_id}/cancel`; the worker lets the current file finish, skips queued files with a clear `cancelled_by_user` reason, and leaves any completed notes visible. On sidecar startup, any batch left in `queued`, `importing`, `cancelling`, or `removing` is marked `interrupted` so the UI can show it as partial instead of pretending it completed. Completed/failed/cancelled/interrupted batches can be removed through `POST /api/source-import/imports/{batch_id}/remove`, which archives only notes recorded in that batch manifest, clears derived rows for those note paths, marks the batch `removed`, and leaves unrelated notes alone. Those same terminal batches can be scanned again through `POST /api/source-import/imports/{batch_id}/rescan`; rescan performs another metadata-only folder scan from the manifest source root, reports new/changed/unchanged/missing files, and caches only new/changed candidates for a separate approved import. The completion surface also calls `GET /api/source-import/imports/{batch_id}/review` when a batch has skipped or failed files, then shows a capped manifest-backed review with relative file paths, reasons, and plain-language next steps.

Step 29e now has a first demo completion moment. `GET /api/source-import/imports/{batch_id}/completion` builds a deterministic manifest-backed summary with imported/skipped/failed/duplicate counts, top imported file types/folders, and suggested questions. `ImportDialog` keeps folder completion open on the Memory page, shows those questions, can open the created notes back in Memory, and routes a clicked question to chat with `import_batch_id`; the chat context builder then restricts retrieval context to notes created by that batch and emits trace rows with `via="import_batch"`.

Step 29f now has a first sample-data, cloud-placeholder, cross-batch dedupe, explicit duplicate policy, and ZIP source/import slice. The desktop bundle includes a fictional `deepfiles-demo-folder` app resource with a proposal, meeting notes, spreadsheet CSV, deck-style saved HTML page, email export, saved vendor page, RTF risk note, and small ZIP handover pack. `source_import_pick_sample_dataset` resolves the bundled resource, asks the sidecar for a normal short-lived local-folder grant, and `ImportDialog` exposes it as "Use sample data" beside the native folder picker. The sample still follows the same scan, review, approve, and import flow; it is never auto-scanned or auto-imported. The scan and worker also detect known online-only cloud placeholder markers from metadata, such as iCloud placeholder filenames, Windows cloud-file attributes, and explicit cloud-provider extended attributes, then skip those files with `online_only_placeholder` instead of trying to read or download them. After approval and hashing, the default worker policy skips files whose SHA-256 content already exists in the same batch or in a successfully imported file from a non-removed prior batch, marking prior-batch matches as `duplicate_content_existing_import`. The start-import request also persists a `duplicate_policy`, and the review UI can intentionally keep duplicate content as separate notes. ZIP files inside a selected folder are treated as guarded containers: the scanner reads archive directory metadata, shows child files in the same review list, and the worker extracts only approved children into temporary files before passing them through the normal ingest path. A standalone ZIP can also be chosen as a `local_archive` source through the desktop picker; its child entries use archive-internal relative paths in review and import.

Step 29g now has the import-scoped retrieval polish pass. Suggested import questions still cannot leak outside the approved batch, but `context_builder` now calls the hybrid retrieval pipeline with a batch note-path allowlist so BM25, chunk/note cosine, graph scoring, and reranking can rank evidence inside that scope. The trace keeps `via="import_batch"` and adds `import_scope` beside the underlying hybrid signals.

Step 29h now has the first import history surface. `GET /api/source-import/imports` accepts a bounded `limit` query, `useSourceImport` exposes `listImports()`, and Folder mode in `ImportDialog` shows recent source imports. Selecting a history row reopens that batch's progress/completion surface, reloads manifest-backed completion and skipped/failed review data when available, and reuses the existing scan-again, remove, cancel, and import-scoped question controls.

The batch detail UI used by active imports and reopened history items is shared through `FolderSourceImportDetail.vue` so progress, completion, open-imported-notes, skipped-file review, rescan/import-changes, cancel, remove, and suggested-question controls stay behaviorally identical across both entry points.

The first explicit source-import limit module is now in place at `backend/services/source_import/limits.py`. Scan preview caps, max scan files, per-file size skips, approved-batch byte caps, archive entry/decompressed-size caps, and current sequential import concurrency are named in one place. Files over the per-file cap show `file_too_large`, scan overflow shows `scan_file_limit`, approved files beyond the batch byte cap are left out with `batch_size_limit`, and archive guard failures keep their archive-specific reasons. The frontend maps common skip and lifecycle reasons such as `archive_encrypted`, `password_protected`, `online_only_placeholder`, `unsupported_file_type`, and `missing_from_source` to readable labels and fix-local hints. Metadata scan rows and rescan rows use the same hint helper as post-import skipped-file review, so repair guidance appears before the user starts importing.

The metadata review surface also shows a compact "needs attention" summary when a scan has skipped or unsupported files. That summary reports the total files needing review, the supported files still ready for approval, and the top repair hints before the user has to inspect individual rows. The same panel can copy a plain-text issue report with source display name, scan counts, grouped reasons, relative affected paths, and suggested fixes so a non-technical operator can ask an admin to download, unlock, convert, or repair source files without sharing the absolute local source root.

Extractor warnings are now carried through the approved import path. When a best-effort converter has to cap or qualify the extracted content, `fast_ingest` writes those warnings into the generated Markdown, the source-import worker stores them on the manifest file row, completion summaries count imported files with warnings, and the shared folder import detail surface shows that count beside imported/skipped/failed/duplicate totals. When the manifest includes per-file warning detail, the completion surface also lists the affected relative file paths and warning text so a non-technical user can review the imported notes without reading logs.

The bundled sample dataset now has an end-to-end backend smoke test that runs the same grant, metadata scan, approval, import, completion summary, ZIP-child provenance, and import-scoped retrieval path used by a real folder. Completion suggestions also prioritize fix-needed, structured-data, and email prompts ahead of lower-specificity document prompts when the five-question cap is reached.

## How It Works

### Source selection and consent

The frontend should expose a source import entry point for files, folders, Jira exports, URLs, archives, and sample data. Folder selection should use the Tauri desktop shell rather than a browser-only file picker so the user can naturally select OneDrive-synced folders, SharePoint folders synced to disk, Dropbox/Google Drive desktop folders, local project folders, SMB shares, and NAS-mounted folders.

The backend should not accept arbitrary raw paths from the web UI for production scans. The desktop picker should grant a short-lived source token or equivalent local capability, and `POST /api/source-import/scan` should require that grant. This avoids accidentally exposing a broad localhost endpoint that can inspect any path on the machine.

Before any content read, the UI states the contract plainly:

```text
DeepFilesAI will first scan file names, types, sizes, and folders.
File contents are imported only after you approve.
```

### Metadata-only scan

The scan creates a temporary report from metadata only. It may inspect file names, extensions, sizes, modified times, folder structure, and ZIP central-directory metadata for archives found inside the chosen folder or for a standalone ZIP source. It must not extract text, hash contents, create Markdown, write embeddings, call a model, or create graph data before user approval.

The report should include:

- source display name and root path
- total files and total size seen
- supported, unsupported, skipped, and warning counts
- counts by extension
- largest files
- folder summary
- proposed destination under `memory/imports/`
- skip reasons for system, temporary, unreadable, oversized, unsupported, encrypted, and placeholder files

Default exclusions should skip system/temp folders such as `.git`, `.svn`, `.hg`, `node_modules`, virtualenvs, caches, build outputs, hidden/system folders, over-limit files, unsupported binaries, symlink targets outside the selected root, and archive entries rejected by safety guards. The UI should summarize these as "system and temporary folders" or archive-specific skipped reasons with expandable detail instead of leading with developer jargon.

### Review and approval

The review screen is the user's control point. It should show what will be imported, what will be skipped, and why. Users can exclude folders, file types, and selected files, then approve the final set.

The current implementation keeps the full scan index in the backend's temporary scan cache and returns only a capped preview to the UI. When the user changes exclusions, `POST /api/source-import/scans/{scan_id}/selection` applies those rules against the full scan and returns an approved-file summary plus a temporary `selection_id`. That keeps large folder behavior honest: the preview can be truncated without losing the approved-file handoff.

Large scans need virtualized or paged tables. The importer should not render thousands of file rows directly.

### Approved import

After approval, the backend reads only the approved files. It writes generated Markdown under:

```text
memory/imports/<source-slug>/
```

Relative subfolders should be preserved where they help the user understand provenance:

```text
Client A/Proposal.docx
-> memory/imports/client-a/Proposal.md

Client A/Discovery/Notes.docx
-> memory/imports/client-a/Discovery/Notes.md
```

Markdown frontmatter should keep safe provenance:

```yaml
source_kind: local_folder_import
source_filename: Proposal.docx
source_relpath: Discovery/Proposal.docx
import_batch_id: import_...
source_modified_at: ...
source_size: ...
```

Do not write absolute local source paths into Markdown by default. Absolute paths can reveal usernames, company names, mount names, and client structure.

The current worker uses the selected batch manifest to preserve relative subfolders and writes to `memory/imports/<source-slug>-<short-batch-id>/` so two folders with the same friendly name cannot overwrite each other. It passes `source_label` and extra provenance into `fast_ingest`, including the CSV/XML structured ingest branch, so generated Markdown stores source-relative values rather than the absolute local source root.

For ZIP children, the source-relative path includes the archive file as a container segment:

```text
Client A/handover.zip/Docs/brief.md
-> memory/imports/client-a-abc123/handover.zip/Docs/brief.md
```

The worker extracts the approved archive member to a temporary local file, hashes that temporary file after approval, adds `source_archive_relpath` and `source_archive_member_path` frontmatter, then deletes the temporary extraction when the batch ends.

For a standalone ZIP source, the archive file is the selected source root. Child paths use archive-internal relative paths:

```text
handover.zip -> Docs/brief.md
-> memory/imports/handover-abc123/Docs/brief.md
```

Those notes use `source_kind: local_archive_import`, keep `source_archive_relpath` as the selected archive filename, and still avoid writing the absolute local archive path into Markdown.

### Batch manifest

Every import should create a local operational manifest keyed by `import_batch_id`. This manifest is app metadata, not the user's knowledge source of truth. It can store the selected source root, scan options, approved file ids, content hashes after approval, generated note paths, skipped files, warnings, and per-file outcomes.

SQLite operational tables are the preferred storage for this manifest. Markdown remains canonical for user knowledge; the manifest exists for lifecycle, progress, dedupe, and recovery. If the manifest is lost, imported Markdown should still be usable after reindex, but remove/re-import history may be unavailable.

The first manifest implementation lives in `source_import_batches` and `source_import_files`, created by `models.database.init_database()`. It stores the local source root as operational metadata, the approved relative paths, per-file status/stage/reason, content hashes after approval, generated note paths, and batch-level counts. The frontend polls `GET /api/source-import/imports/{batch_id}` for progress, uses `GET /api/source-import/imports/{batch_id}/review` for a capped skipped/failed file review, and loads `GET /api/source-import/imports?limit=...` to reopen recent source import batches after the dialog has been closed.

The manifest exists so the app can:

- show accurate completion summaries
- remove a whole import safely
- explicitly scan or import the same source again
- detect duplicates by content hash
- show skipped and failed files later
- debug lifecycle issues without writing sensitive absolute paths into note frontmatter

### Remove import

The user should be able to remove an import batch from completion and import history views. Removal should confirm the affected batch, delete or archive only Markdown notes created by that batch, refresh derived SQLite/embedding/chunk/graph/suggestion data for those notes, and leave unrelated user notes untouched.

Removing an import is part of the trust model. It lets a user try a real folder without feeling trapped if they chose the wrong source.

The current implementation exposes removal on the folder-import completion/history surface and through `POST /api/source-import/imports/{batch_id}/remove`, with the batch id repeated in the request body as an explicit confirmation. The removal service reads generated note paths from the manifest, uses the normal soft-delete path to move those notes into `.trash/`, clears note/chunk/node embedding rows plus note-scoped enrichment and dismissal rows for those paths, schedules a graph rebuild, and keeps the operational manifest as a local audit record. It does not remove user-created notes that happen to live near the import destination.

### Re-import and duplicate handling

Continuous sync is deferred, but explicit re-import/rescan is supported for completed, failed, cancelled, and interrupted batches if the manifest still has the local source root.

Re-import should compare source-relative path, size, modified time, and content hash when available:

- unchanged files are skipped
- changed files are imported into a new approved batch in the current slice
- new files are imported
- missing files are reported, not automatically deleted in the first slice
- duplicate files are detected by content hash after approval

Do not hash during metadata scan, because hashing reads file contents. Hashing is allowed during the approved import.

The current rescan service compares source-relative paths and file metadata only. It marks unchanged files as informational, reports missing files without deleting existing Markdown, treats previously failed/cancelled files as importable again, and creates a temporary scan containing only new or changed candidates. The UI shows the "Since last import" counts and uses the same `/selection` and `/start` path before any file contents are read. This means changed files create fresh notes under a new batch-disambiguated destination today; replacing or updating the prior generated note for the same source-relative path remains future policy work.

The current worker performs duplicate handling only after approval, because content hashing reads file contents. With the default `duplicate_policy: skip`, once a file is successfully imported, later approved files in the same batch with the same SHA-256 content hash are marked `skipped` with `duplicate_content` and a `duplicate_of` relative path.

Cross-batch dedupe is also active for successfully imported files in non-removed batches when the duplicate policy is `skip`. `source_import.dedupe` looks up the approved file's content hash in the local manifest and skips the duplicate as `duplicate_content_existing_import`, with `duplicate_of` pointing to the prior source display name and relative path. Removed batches are ignored so a user can safely remove an import and then import the same content again. When the user chooses to import duplicate content as separate notes, the batch manifest stores `duplicate_policy: import` and the worker still hashes after approval but does not skip same-batch or prior-batch content matches.

### Crash recovery and cancellation

Folder import can take long enough that shutdown and cancellation need first-class behavior. Writes should be atomic enough that a crash does not leave malformed Markdown marked as complete. On startup, any batch left in `queued`, `importing`, `cancelling`, or `removing` should be marked interrupted or recoverable rather than silently completed.

Cancellation should stop queued files, let the active file finish or fail cleanly, keep completed notes visible, and mark the batch as partial/cancelled until the user removes or re-imports it.

The current implementation persists cancellation as manifest state rather than only an in-memory task flag. `source_import.cancellation` marks `queued`/`importing` batches as `cancelling`; the worker checks between files, marks unprocessed files skipped, and finishes the batch as `cancelled`. FastAPI startup calls `mark_interrupted_batches()` after database initialization so orphaned active manifests become `interrupted` and removable on the next launch.

### Skipped and failed review

Skipped and failed files are intentionally visible product state. The current review endpoint reads from the batch manifest, groups problem files by reason, caps the returned file list, and marks whether each file is likely retryable or locally fixable. `ImportDialog` shows that review on the completion surface with relative file paths and user-facing next steps such as downloading an online-only file, unlocking a protected document, checking local permissions, or scanning again after a cancellation.

This review covers approved files that were skipped or failed during the import worker, including duplicate-content skips and extractor/read failures. Pre-approval scan exclusions such as unsupported file types and hidden/system folders are visible on the active scan review screen; persisting those scan-only rows as import history is still a later policy choice.

### Completion summary and scoped questions

The first completion summary is deterministic and manifest-backed. It does not call a model to invent prompts; it derives the user-facing summary from `source_import_files` outcomes: successful imports, created notes, duplicate-content skips, skipped/failed counts, top extensions, and top source-relative folders.

Suggested questions are generated from those same manifest facts. General prompts are always present when at least one file imported, and extra prompts appear for skipped/failed files, spreadsheets/structured data, emails, decks, documents, and the highest-volume folder. Within the capped list, fix-needed, structured-data, and email prompts come before lower-specificity document prompts so the completion screen favors demo actions. Clicking a question navigates to chat with the current `import_batch_id`. The same completion surface can also open the created Markdown notes in Memory by normalizing manifest note paths, activating the imported folder, and selecting the first created note.

The chat path treats that batch id as a retrieval scope. `build_context()` asks the manifest for created note paths, calls the hybrid retrieval pipeline with those paths as an allowlist, wraps the selected Markdown notes in normal `<retrieved_note>` evidence tags, and emits retrieval traces marked `via="import_batch"`. The answer stays grounded in the folder the user just approved while still benefiting from BM25, chunk/note cosine, graph scoring, and reranking inside that boundary. If the hybrid pass is unavailable, the builder falls back to deterministic keyword-overlap ranking over the same manifest-created notes.

### Destination naming and collisions

The friendly default path is:

```text
memory/imports/<source-slug>/
```

If that folder already exists for a different import, use a stable disambiguator such as:

```text
memory/imports/<source-slug>-<short-batch-id>/
```

The UI can still show the friendly source name. The filesystem path must avoid overwriting prior imports.

### Limits and back-pressure

Source import must enforce explicit limits so a huge share or hostile archive cannot freeze the app. The implementation should define and test limits for max files per scan, max approved bytes per batch, max per-file bytes, max archive decompressed bytes, max archive file count, max archive depth, max folder depth shown in review, max concurrent extractors, and max concurrent indexing/embedding work spawned by one import.

When a limit is hit, the file or batch should show a visible skipped/limited reason. Silent truncation is not acceptable.

### Document type coverage

The current memory ingest path supports `.md`, `.txt`, `.pdf`, `.csv`, `.xml`, `.json`, `.docx`, `.xlsx`, `.pptx`, `.html`, `.htm`, `.rtf`, `.eml`, and `.zip`.

The first 29c extractor work deliberately avoids adding Office parser dependencies. DOCX, XLSX, and PPTX are ZIP/XML formats, so DeepFilesAI reads their safe text/table/slide parts with the Python standard library plus `defusedxml`. Malformed Office XML is reported as a safe importer error, optional malformed XLSX shared strings become warnings when the sheet can still be read, and encrypted ZIP/Office members are rejected before extraction. HTML/HTM uses the existing `trafilatura` plus `markdownify` stack and falls back to sanitized body conversion with a user-readable warning when main-content extraction is unavailable. RTF uses a best-effort control-word stripper. EML uses Python's standard `email` package and records attachments as metadata only. Single-file ZIP upload still imports as a safe archive inventory note, while folder/source import now expands ZIP children into the review and approval flow before extracting approved children.

Extractor quality should be honest. These formats are best-effort conversions, not a guarantee that every vendor-specific document layout converts perfectly. Completion states should distinguish imported successfully, imported with warnings, skipped before extraction, and failed during extraction.

The current warning path covers extractor-level warnings such as capped spreadsheet previews, column-limited workbook tables, malformed optional XLSX shared strings, blank presentations, empty RTF/email bodies, listed-but-not-imported email attachments, HTML body fallback, archive inventory preview limits, and generated Markdown truncation. Warnings are stored in note frontmatter as `extractor_warnings`, appended to the generated Markdown under `Import warnings`, persisted in `source_import_files.warnings`, and surfaced as `warning_file_count` in the manifest-backed completion summary. It is intentionally a visible quality signal, not a batch failure.

Extractor polish now also preserves Office core-property titles when present, falls back to filenames when metadata is absent, strips script/style/template content from HTML fallback conversion, records email `Cc` headers, keeps attachment names as metadata only, and rejects encrypted archive members with `archive_encrypted` before content extraction. The direct extractor regression suite pins representative DOCX, XLSX, PPTX, HTML, RTF, EML, ZIP, malformed XML, and encrypted archive behavior separately from the broader approved-import lifecycle tests.

Before adding a new parser dependency, verify license compatibility, transitive notices, bundle-size impact, malformed-file behavior, memory use on large files, and that macros, external links, templates, or embedded objects are ignored rather than executed. The extractor registry should allow an extractor to be disabled if a dependency fails this gate.

### Progress and completion

The progress UI should avoid internal terms. Prefer "Reading files", "Creating memory", "Preparing search", "Finding connections", and "Skipped files" over implementation terms such as extractor, Markdown writer, embedding, graph linking, or ingest failure.

The completion screen is the demo moment. It should summarize imported files, created notes, split documents, skipped/failed files, duplicate handling, and suggested connections. It should offer actions to ask about the import, open imported memory, review suggested connections, view skipped files, scan the source again, or remove the import.

Suggested questions and "Ask about this import" should carry `import_batch_id` into chat/retrieval as a scope hint. A user asking about the folder they just imported should get an answer grounded in that folder first, with retrieval traces showing the imported sources used.

Suggested questions should be deterministic in the first slice. Generate them from file types, folder names, document titles, headings, and section classifications rather than requiring an LLM call just to create prompts.

### Path and logging hygiene

The importer should normalize and safely slug Unicode filenames, handle case-insensitive collisions on macOS/Windows, avoid reserved Windows names where practical, and preserve source-relative paths for provenance. It should avoid logging file contents and avoid writing full absolute paths into user-visible errors unless the path is necessary for the user to fix a local permissions issue.

### Sample dataset

The product should include or plan a small fictional sample business folder so demos do not depend on a prospect handing over real files. The sample dataset should include representative files such as a proposal, meeting notes, spreadsheet, deck, email export, saved page, and small archive.

The current sample lives under `desktop/src-tauri/sample-data/deepfiles-demo-folder/` and is bundled through `tauri.conf.json` as an app resource, not inside the Python sidecar. The Rust shell resolves that resource path and creates the same backend source grant used by the native folder picker. Sample data is clearly labeled, fictional, and must not auto-import without user action. Backend smoke coverage imports this folder end to end and asserts that generated Markdown keeps source-relative provenance, ZIP children record archive metadata, completion questions include data/email prompts, and import-scoped retrieval traces stay inside the batch.

## Key Files

- `backend/main.py` - Marks orphaned active source-import batches interrupted during sidecar startup.
- `backend/models/database.py` - Creates and migrates the SQLite operational manifest tables used by source import batches, per-file outcomes, and extractor warnings.
- `backend/routers/source_import.py` - REST API for trusted source grants, metadata scans, cached scan reports, review selections, import start, bounded import listing, batch status, completion summaries, skipped/failed review, rescan, cancellation, and removal.
- `backend/services/ingest.py` - Existing single-file ingest path now delegates business document formats to the reusable extractor registry, accepts source-relative provenance for approved folder imports, and returns extractor warnings from best-effort conversions.
- `backend/services/structured_ingest.py` - CSV/XML ingest path; applies folder-import provenance to generated structured-data notes when called through `fast_ingest`.
- `backend/routers/chat.py` - Accepts `import_batch_id` in chat payloads so questions launched from an import completion can stay scoped to that batch.
- `backend/services/context_builder.py` - Builds import-scoped evidence from manifest-created note paths when chat receives an import batch scope.
- `backend/services/retrieval/pipeline.py` - Supports path-allowlisted hybrid retrieval so import-scoped questions can use BM25, cosine, graph, and rerank signals without leaving the approved batch.
- `backend/services/system_prompt.py` - Threads the optional import batch scope through context assembly while keeping retrieval outside the stable system-prompt prefix.
- `backend/services/source_import/__init__.py` - Source-import package note that tracks the current slice boundaries.
- `backend/services/source_import/archives.py` - ZIP central-directory scanning, path traversal/limit/encryption guards, archive child relpath handling, and temporary approved-child extraction for folder-contained and standalone archive sources.
- `backend/services/source_import/cancellation.py` - Import lifecycle service for requesting cancellation of active batches.
- `backend/services/source_import/cloud_placeholders.py` - Conservative metadata-only cloud placeholder detection and read-error classification for OneDrive/iCloud/Dropbox/Google Drive style synced folders.
- `backend/services/source_import/dedupe.py` - Cross-batch content-hash duplicate lookup for previously imported, non-removed batches.
- `backend/services/source_import/extractors.py` - Best-effort business document extractor registry for DOCX, XLSX, PPTX, HTML/HTM, RTF, EML, and safe ZIP inventory notes.
- `backend/services/source_import/grants.py` - Short-lived in-memory source grants created only from trusted desktop picker paths for local folders and standalone ZIP archives.
- `backend/services/source_import/limits.py` - Named scan, preview, per-file, approved-batch, archive, and current concurrency limits used by the source-import scanner, selection builder, archive guard, and worker.
- `backend/services/source_import/manifest.py` - SQLite-backed import batch manifest, progress summary, completion summary, cancellation state, interrupted-batch recovery, per-file status, extractor-warning storage, skipped/failed review reports, generated note path, and content-hash storage.
- `backend/services/source_import/removal.py` - Import lifecycle cleanup; archives only manifest-created notes and clears derived rows for those note paths.
- `backend/services/source_import/rescan.py` - Metadata-only re-scan comparison for previous import batches; reports new/changed/unchanged/missing files and caches only new/changed candidates for approval.
- `backend/services/source_import/scan.py` - Metadata-only folder/archive scanner; counts supported/skipped/unsupported files without opening file contents.
- `backend/services/source_import/selection.py` - Applies review exclusions to the full cached scan and creates the approved-file handoff record.
- `backend/services/source_import/store.py` - Temporary in-memory scan and selection cache for the review screen.
- `backend/services/source_import/models.py` - Pydantic request/response models for grants, scans, review selections, duplicate policy, import batch summaries, completion summaries, skipped/failed review reports, and rescan reports.
- `backend/services/source_import/worker.py` - Background approved-file importer; hashes only after approval, applies the selected duplicate policy, honors cancellation between files, writes safe provenance, carries extractor warnings into the manifest, and updates progress.
- `desktop/src-tauri/Cargo.toml` - Adds `getrandom` for strong shell-to-sidecar grant token generation.
- `desktop/src-tauri/tauri.conf.json` - Bundles the fictional sample business folder as an app resource separate from the sidecar binary.
- `desktop/src-tauri/src/lib.rs` - `source_import_pick_folder`, `source_import_pick_archive`, and `source_import_pick_sample_dataset` commands: native macOS folder/archive picker or bundled sample-data resolver plus shell-authenticated grant registration.
- `desktop/src-tauri/sample-data/deepfiles-demo-folder/` - Fictional demo folder used to show source import without a prospect handing over real files.
- `desktop/src-tauri/sample-data-src/handover-pack/` - Maintainer source files for the small sample ZIP inventory package.
- `desktop/scripts/dev.sh` - Shares the dev source-import grant token between the sidecar and Tauri shell.
- `frontend/app/composables/useChat.ts` - Sends `import_batch_id` when the user asks a suggested import question from completion.
- `frontend/app/composables/useSourceImport.ts` - Frontend wrapper for desktop folder picking, sample-data picking, scan, review selection creation, import start with duplicate policy, import history listing, import status polling, completion summary, skipped/failed review, cancellation, rescan, and import removal.
- `frontend/app/composables/useFolderSourceImportDialog.ts` - Dialog-local state machine for folder/archive/sample import review, recent import history, progress polling, completion summaries, skipped-file hints, cancellation, removal, and rescan/import-changes actions.
- `frontend/app/pages/main.vue` - Reads `import_batch_id` and `q` query params to start an import-scoped chat turn from the completion surface.
- `frontend/app/pages/memory.vue` - Keeps the folder import dialog open after terminal folder batches so the user can see the completion summary and suggested questions, then can jump back to the imported notes.
- `frontend/app/components/ImportDialog.vue` - Renders the existing file/Jira import modal plus Folder mode; folder/archive/sample import behavior and recent source import history are delegated to `useFolderSourceImportDialog`.
- `frontend/app/components/FolderSourceImportDetail.vue` - Shared folder import batch detail surface for active imports and reopened history items, including progress, completion, imported-with-warnings counts, open-imported-notes, skipped-file review, suggested questions, scan-again/import-changes, cancel, and remove controls.
- `backend/tests/test_ingest_service.py` - Backend coverage for business document extraction into Markdown through `fast_ingest`.
- `backend/tests/test_source_import_extractors.py` - Direct extractor coverage for Office metadata titles, workbook row/column preview warnings, malformed optional shared strings, malformed required Office XML, blank decks, sanitized HTML fallback, RTF control-word decoding, email Cc/attachment metadata, encrypted archive rejection, and ZIP inventory preview limits.
- `backend/tests/test_source_import_scan.py` - Backend coverage for grant auth, metadata-only scanning, single-use tokens, scan/file/batch limits, archive encryption skips, symlink skips, full-scan review selection, business document support counts, bundled sample dataset scanability and end-to-end import, approved import start, duplicate skip/import policy, safe provenance, completion summaries, import-scoped chat context, skipped/failed review, cancellation, interrupted recovery, remove-import cleanup, and rescan/import-changes behavior.
- `frontend/tests/components/ImportDialog.test.ts` - Frontend coverage for Folder mode, sample-data selection, review exclusion updates, approved import start, duplicate-content choice, completion summary display, open-imported-notes event wiring, skipped/failed review display, recent import history, active-import cancellation, completed-import removal, and scan-again/import-changes controls.
- `frontend/tests/pages/memory.test.ts` - Frontend page coverage for opening imported notes from the folder completion surface.
- `frontend/tests/composables/useChat.test.ts` - Pins that chat payloads carry `import_batch_id` when a suggested import question is launched.

## API / Interface

Implemented REST surface:

```text
POST   /api/source-import/grants
POST   /api/source-import/scan
GET    /api/source-import/scans/{scan_id}
POST   /api/source-import/scans/{scan_id}/selection
POST   /api/source-import/scans/{scan_id}/start
GET    /api/source-import/imports
GET    /api/source-import/imports/{batch_id}
GET    /api/source-import/imports/{batch_id}/completion
GET    /api/source-import/imports/{batch_id}/review
POST   /api/source-import/imports/{batch_id}/rescan
POST   /api/source-import/imports/{batch_id}/cancel
POST   /api/source-import/imports/{batch_id}/remove
```

`POST /api/source-import/scan` should receive a trusted picker grant/source token plus scan options, not an arbitrary raw path from the browser UI.

Batch states:

```text
scanning -> ready_for_review -> importing -> cancelling
                                           -> completed
                              -> cancelled
                              -> interrupted
                              -> removing
                              -> removed
                              -> failed
```

Per-file stages:

```text
queued -> reading -> creating_memory -> preparing_search -> finding_connections -> done
                                                                     -> skipped
                                                                     -> failed
```

Backend internals may keep more technical stage names, but the frontend should present plain-language labels.

## Gotchas

**Metadata scan cannot hash files.** Hashing reads contents, so it belongs after approval. Use metadata for pre-approval inventory and content hash for approved dedupe/re-import.

**The UI preview is not the approved set.** Scan responses cap visible files for performance. The temporary backend scan record is the source for review selections so a truncated preview does not silently drop files from the approved set.

**Raw local paths are too powerful for the scan API.** Even in a local-only app, a localhost endpoint that scans arbitrary paths is risky. Production scans should be tied to a trusted desktop picker grant.

**Operational manifests may be more sensitive than notes.** The manifest can store absolute source roots for lifecycle actions, but Markdown notes should only store safe relative provenance by default.

**Import-scoped questions need import-scoped retrieval.** Without `import_batch_id` scope, the demo can accidentally answer from older unrelated memory, making the product look less trustworthy.

**Import-scoped retrieval uses an allowlist, not a folder guess.** Suggested questions read only note paths recorded in the import manifest, then run hybrid scoring inside that set. This prevents older unrelated memory from appearing just because it shares a folder name or keyword.

**Cancelled and interrupted imports are normal states.** A laptop can sleep, the app can close, and users can cancel. These states are removable, and cancelled batches keep already-created notes visible until the user removes the import.

**Rescan is not sync.** `POST /api/source-import/imports/{batch_id}/rescan` is a user-triggered metadata comparison. It does not watch folders, delete missing files, hash contents, or replace older generated notes automatically.

**Skipped review is manifest-backed.** `GET /api/source-import/imports/{batch_id}/review` reports skipped/failed outcomes from approved import files. It does not yet persist every unsupported or scan-skipped pre-approval row as batch history.

**Cloud-sync folders are local paths, not connectors.** OneDrive, SharePoint, Dropbox, and Google Drive desktop sync are supported only insofar as they expose local files. The scanner and worker now perform conservative metadata-only placeholder detection for `.icloud` placeholder filenames, Windows cloud-file attributes, and explicit cloud-provider xattrs, then report `online_only_placeholder` with a fixable review hint. Detection is best-effort and local-only; the app does not call cloud APIs or hydrate files.

**Removal is not just file deletion.** Deleting generated Markdown is not enough. SQLite rows, embeddings, chunks, graph edges, and suggestions derived from those notes must be removed or refreshed.

**Duplicate behavior must be explicit.** Same content in multiple folders is common in business file shares. The current default is content-hash dedupe after approval inside the batch and across non-removed prior batches, while the review UI exposes an explicit "import duplicate content as separate notes" choice for batches where repeated copies matter.

**Folder names collide.** Two clients can both have a `Documents` folder. Destination paths need a safe disambiguator while the UI keeps the friendly name.

**Limits are product behavior.** Max-file, max-size, archive, approved-batch, and current concurrency limits live in `backend/services/source_import/limits.py` and show clear skip reasons such as `scan_file_limit`, `file_too_large`, `batch_size_limit`, `archive_entry_limit`, and `archive_size_limit`.

**ZIP is a source, not just a file.** Archives need the same scan, review, approval, and guardrail model as folders. Never extract archive entries blindly.

**ZIP support has three paths.** Single-file ZIP ingest creates a safe listing note and does not extract child files. Folder/source import expands ZIP files found inside the selected folder into guarded child rows, then extracts only approved children to temporary files. Standalone ZIP source import uses the same guarded child review/import path without adding the archive filename as a destination folder segment. Encrypted archive members are rejected from central-directory metadata as `archive_encrypted`; nested archive policy is still later work.

**Implementation is review-ready, with manual desktop smoke pending.** Step 29a and 29b exist, 29c has reusable extractors plus the first approved folder import worker, 29d covers removal, cancellation, interrupted-batch recovery, skipped/failed review, and explicit scan-again/import-changes, 29e has deterministic completion summaries, import-scoped suggested questions, and open-imported-notes handoff back to Memory, 29f has the first bundled fictional sample dataset, conservative online-only placeholder handling, cross-batch content dedupe, explicit duplicate-content choice, ZIP child import from selected folders, standalone ZIP source picking, and explicit nested-archive skips, 29g applies hybrid scoring inside the import-batch allowlist, 29h adds recent source import history, the first named source-import limits are enforced with visible reasons, extractor warnings surface in Markdown, manifest rows, completion summaries, and per-file completion details, direct extractor tests pin representative business-format quality, encrypted archives are rejected before extraction, scan attention can be copied as a relative-path issue report, and the bundled sample folder has end-to-end backend smoke coverage. Later slices still own dependency-backed parser evaluation, OCR/scanned documents, legacy Office formats, and deeper corpus-specific malformed-file hardening.

## Related

- [Folder Source Import Demo Smoke](../runbooks/folder-source-import-demo-smoke.md)
- [Memory System](memory.md)
- [PDF Section Split](pdf-section-split.md)
- [Smart Connect](smart-connect.md)
- [Retrieval Trace UI](retrieval-trace.md)
