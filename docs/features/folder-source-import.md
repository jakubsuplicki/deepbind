---
title: Folder Source Import
status: in-progress
type: feature
sources:
  - backend/models/database.py
  - backend/routers/source_import.py
  - backend/services/ingest.py
  - backend/services/structured_ingest.py
  - backend/services/source_import/__init__.py
  - backend/services/source_import/extractors.py
  - backend/services/source_import/grants.py
  - backend/services/source_import/manifest.py
  - backend/services/source_import/models.py
  - backend/services/source_import/scan.py
  - backend/services/source_import/selection.py
  - backend/services/source_import/store.py
  - backend/services/source_import/worker.py
  - backend/tests/test_ingest_service.py
  - backend/tests/test_source_import_scan.py
  - desktop/src-tauri/Cargo.toml
  - desktop/src-tauri/src/lib.rs
  - desktop/scripts/dev.sh
  - frontend/app/composables/useSourceImport.ts
  - frontend/app/components/ImportDialog.vue
  - frontend/tests/components/ImportDialog.test.ts
depends_on: [memory, pdf-section-split, smart-connect, retrieval-trace, desktop-shell-graduation]
last_reviewed: 2026-05-16
last_updated: 2026-05-16
---

# Folder Source Import

## Summary

Folder Source Import is the planned buyer-visible import flow for local and synced business folders. It lets a non-developer choose a folder, review a metadata-only inventory, approve exactly what DeepFilesAI may read, and then turn approved files into local Markdown memory with progress, evidence, and recovery controls.

This feature complements the existing single-file upload path documented in [memory.md](memory.md). The durable product rule is still the same: Markdown under `memory/` is the source of truth, while SQLite, embeddings, chunks, graph edges, suggestions, and import manifests are derived or operational layers.

## Definition of Done

- User can choose a local folder, mounted share, synced cloud folder, ZIP archive, or sample dataset from the desktop app.
- Backend scans only local sources granted by the trusted desktop picker flow.
- Metadata scan reads only names, extensions, sizes, modified times, and folder structure before approval.
- Review screen shows supported, skipped, unsupported, duplicate, warning, and estimated import counts.
- User can exclude folders, file types, and selected files before import.
- Import supports existing memory ingest types plus DOCX, XLSX, PPTX, HTML/HTM, RTF, EML, and ZIP containers.
- Batch and per-file progress are visible in plain language.
- Imported knowledge lands as Markdown under `memory/imports/<source-slug>/`.
- Import manifest supports remove import, explicit re-import/rescan, duplicate handling, and skipped-file review.
- Removing an import deletes only notes created by that import batch and refreshes derived indexes.
- Suggested questions and "Ask about this import" scope retrieval to the current import batch.
- App shutdown, cancellation, folder-name collisions, and hard limits have explicit user-visible behavior.
- No cloud connector, OAuth flow, telemetry, or network dependency is introduced.
- Tests cover scan consent, import lifecycle, extractor fixtures, archive guards, and frontend review/progress states.

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

Step 29c now has a first approved-import path. Reusable business document extractors are wired into `fast_ingest`, folder scans count DOCX, XLSX, PPTX, HTML/HTM, RTF, EML, and ZIP inventory as supported, and `POST /api/source-import/scans/{scan_id}/start` creates a SQLite-backed import manifest before a background worker reads only the approved files. Imported notes receive safe source-relative provenance, destination folders get a short batch disambiguator, and duplicate files inside the batch are skipped by content hash after approval.

Still planned: full ZIP-as-container child extraction, remove-import, explicit re-import/rescan, cancellation/interruption recovery, richer skipped/failed review, sample dataset, and batch-scoped chat/retrieval actions.

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

The scan creates a temporary report from metadata only. It may inspect file names, extensions, sizes, modified times, and folder structure. It must not extract text, hash contents, create Markdown, write embeddings, call a model, or create graph data before user approval.

The report should include:

- source display name and root path
- total files and total size seen
- supported, unsupported, skipped, and warning counts
- counts by extension
- largest files
- folder summary
- proposed destination under `memory/imports/`
- skip reasons for system, temporary, unreadable, oversized, unsupported, encrypted, and placeholder files

Default exclusions should skip system/temp folders such as `.git`, `.svn`, `.hg`, `node_modules`, virtualenvs, caches, build outputs, hidden/system folders, over-limit files, unsupported binaries, and symlink targets outside the selected root. The UI should summarize these as "system and temporary folders" with expandable detail instead of leading with developer jargon.

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

### Batch manifest

Every import should create a local operational manifest keyed by `import_batch_id`. This manifest is app metadata, not the user's knowledge source of truth. It can store the selected source root, scan options, approved file ids, content hashes after approval, generated note paths, skipped files, warnings, and per-file outcomes.

SQLite operational tables are the preferred storage for this manifest. Markdown remains canonical for user knowledge; the manifest exists for lifecycle, progress, dedupe, and recovery. If the manifest is lost, imported Markdown should still be usable after reindex, but remove/re-import history may be unavailable.

The first manifest implementation lives in `source_import_batches` and `source_import_files`, created by `models.database.init_database()`. It stores the local source root as operational metadata, the approved relative paths, per-file status/stage/reason, content hashes after approval, generated note paths, and batch-level counts. The frontend polls `GET /api/source-import/imports/{batch_id}` for progress.

The manifest exists so the app can:

- show accurate completion summaries
- remove a whole import safely
- explicitly scan or import the same source again
- detect duplicates by content hash
- show skipped and failed files later
- debug lifecycle issues without writing sensitive absolute paths into note frontmatter

### Remove import

The user should be able to remove an import batch from completion and import history views. Removal should confirm the affected batch, delete or archive only Markdown notes created by that batch, refresh derived SQLite/embedding/chunk/graph/suggestion data for those notes, and leave unrelated user notes untouched.

Removing an import is part of the trust model. It lets a buyer try a real folder without feeling trapped if they chose the wrong source.

### Re-import and duplicate handling

Continuous sync is deferred, but explicit re-import/rescan should be supported if the manifest has enough metadata.

Re-import should compare source-relative path, size, modified time, and content hash when available:

- unchanged files are skipped
- changed files update or replace the prior generated note for that source-relative path
- new files are imported
- missing files are reported, not automatically deleted in the first slice
- duplicate files are detected by content hash after approval

Do not hash during metadata scan, because hashing reads file contents. Hashing is allowed during the approved import.

The current worker performs first-slice duplicate handling inside one batch: once an approved file is successfully imported, later approved files with the same SHA-256 content hash are marked `skipped` with `duplicate_content` and a `duplicate_of` relative path. Cross-batch dedupe and explicit "import duplicates anyway" remain future policy work.

### Crash recovery and cancellation

Folder import can take long enough that shutdown and cancellation need first-class behavior. Writes should be atomic enough that a crash does not leave malformed Markdown marked as complete. On startup, any batch left in an importing/removing state should be marked interrupted or recoverable rather than silently completed.

Cancellation should stop queued files, let the active file finish or fail cleanly, keep completed notes visible, and mark the batch as partial/cancelled until the user removes or re-imports it.

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

The first 29c extractor work deliberately avoids adding Office parser dependencies. DOCX, XLSX, and PPTX are ZIP/XML formats, so DeepFilesAI reads their safe text/table/slide parts with the Python standard library plus `defusedxml`. HTML/HTM uses the existing `trafilatura` plus `markdownify` stack. RTF uses a best-effort control-word stripper. EML uses Python's standard `email` package and records attachments as metadata only. ZIP currently imports as a safe archive inventory note; extracting approved archive children is still part of a later archive-specific source workflow.

Extractor quality should be honest. These formats are best-effort conversions, not a guarantee that every vendor-specific document layout converts perfectly. Completion states should distinguish imported successfully, imported with warnings, skipped before extraction, and failed during extraction.

Before adding a new parser dependency, verify license compatibility, transitive notices, bundle-size impact, malformed-file behavior, memory use on large files, and that macros, external links, templates, or embedded objects are ignored rather than executed. The extractor registry should allow an extractor to be disabled if a dependency fails this gate.

### Progress and completion

The progress UI should avoid internal terms. Prefer "Reading files", "Creating memory", "Preparing search", "Finding connections", and "Skipped files" over implementation terms such as extractor, Markdown writer, embedding, graph linking, or ingest failure.

The completion screen is the demo moment. It should summarize imported files, created notes, split documents, skipped/failed files, duplicate handling, and suggested connections. It should offer actions to ask about the import, open imported memory, review suggested connections, view skipped files, scan the source again, or remove the import.

Suggested questions and "Ask about this import" should carry `import_batch_id` into chat/retrieval as a scope hint. A buyer asking about the folder they just imported should get an answer grounded in that folder first, with retrieval traces showing the imported sources used.

Suggested questions should be deterministic in the first slice. Generate them from file types, folder names, document titles, headings, and section classifications rather than requiring an LLM call just to create prompts.

### Path and logging hygiene

The importer should normalize and safely slug Unicode filenames, handle case-insensitive collisions on macOS/Windows, avoid reserved Windows names where practical, and preserve source-relative paths for provenance. It should avoid logging file contents and avoid writing full absolute paths into user-visible errors unless the path is necessary for the user to fix a local permissions issue.

### Sample dataset

The product should include or plan a small fictional sample business folder so demos do not depend on a prospect handing over real files. The sample dataset should include representative files such as a proposal, meeting notes, spreadsheet, deck, email export, saved page, and small archive.

Sample data must be clearly labeled and must not auto-import without user action.

## Key Files

- `backend/models/database.py` - Creates the SQLite operational manifest tables used by source import batches and per-file outcomes.
- `backend/routers/source_import.py` - REST API for trusted source grants, metadata scans, cached scan reports, review selections, import start, import listing, and batch status.
- `backend/services/ingest.py` - Existing single-file ingest path now delegates business document formats to the reusable extractor registry and accepts source-relative provenance for approved folder imports.
- `backend/services/structured_ingest.py` - CSV/XML ingest path; applies folder-import provenance to generated structured-data notes when called through `fast_ingest`.
- `backend/services/source_import/__init__.py` - Source-import package note that tracks the current slice boundaries.
- `backend/services/source_import/extractors.py` - Best-effort business document extractor registry for DOCX, XLSX, PPTX, HTML/HTM, RTF, EML, and safe ZIP inventory notes.
- `backend/services/source_import/grants.py` - Short-lived in-memory source grants created only from the trusted desktop picker path.
- `backend/services/source_import/manifest.py` - SQLite-backed import batch manifest, progress summary, per-file status, generated note path, and content-hash storage.
- `backend/services/source_import/scan.py` - Metadata-only directory scanner; counts supported/skipped/unsupported files without opening file contents.
- `backend/services/source_import/selection.py` - Applies review exclusions to the full cached scan and creates the approved-file handoff record.
- `backend/services/source_import/store.py` - Temporary in-memory scan and selection cache for the review screen.
- `backend/services/source_import/models.py` - Pydantic request/response models for grants, scans, review selections, and import batch summaries.
- `backend/services/source_import/worker.py` - Background approved-file importer; hashes only after approval, skips same-batch duplicates, writes safe provenance, and updates the manifest.
- `desktop/src-tauri/Cargo.toml` - Adds `getrandom` for strong shell-to-sidecar grant token generation.
- `desktop/src-tauri/src/lib.rs` - `source_import_pick_folder` command: native macOS folder picker plus shell-authenticated grant registration.
- `desktop/scripts/dev.sh` - Shares the dev source-import grant token between the sidecar and Tauri shell.
- `frontend/app/composables/useSourceImport.ts` - Frontend wrapper for desktop folder picking, scan, review selection creation, import start, and import status polling.
- `frontend/app/components/ImportDialog.vue` - Adds Folder mode, metadata review summary, exclusion controls, approved import start, and batch progress/completion UI to the existing import dialog.
- `backend/tests/test_ingest_service.py` - Backend coverage for business document extraction into Markdown.
- `backend/tests/test_source_import_scan.py` - Backend coverage for grant auth, metadata-only scanning, single-use tokens, limits, symlink skips, full-scan review selection, business document support counts, approved import start, duplicate skip, and safe provenance.
- `frontend/tests/components/ImportDialog.test.ts` - Frontend coverage for Folder mode, review exclusion updates, and approved import start.

Planned later files:

- `backend/services/source_import/dedupe.py` - Approved-file hashing and duplicate/re-import comparison.
- `backend/services/source_import/limits.py` - Scan, file, archive, and concurrency limits.

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
```

Planned lifecycle surface:

```text
POST   /api/source-import/imports/{batch_id}/rescan
POST   /api/source-import/imports/{batch_id}/cancel
DELETE /api/source-import/imports/{batch_id}
```

`POST /api/source-import/scan` should receive a trusted picker grant/source token plus scan options, not an arbitrary raw path from the browser UI.

Batch states:

```text
scanning -> ready_for_review -> importing -> completed
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

**Cancelled and interrupted imports are normal states.** A laptop can sleep, the app can close, and users can cancel. These states should be recoverable or removable, not treated as exceptional corruption.

**Cloud-sync folders are local paths, not connectors.** OneDrive, SharePoint, Dropbox, and Google Drive desktop sync are supported only insofar as they expose local files. Online-only placeholders should be skipped with a clear reason; the app should not download them in this slice.

**Removal is not just file deletion.** Deleting generated Markdown is not enough. SQLite rows, embeddings, chunks, graph edges, and suggestions derived from those notes must be removed or refreshed.

**Duplicate behavior must be explicit.** Same content in multiple folders is common in business file shares. Default to content-hash dedupe after approval, and make any "import duplicates anyway" behavior an explicit user choice.

**Folder names collide.** Two clients can both have a `Documents` folder. Destination paths need a safe disambiguator while the UI keeps the friendly name.

**Limits are product behavior.** Max-file, max-size, archive, and concurrency limits should show clear skip reasons. They are not just defensive constants buried in code.

**ZIP is a source, not just a file.** Archives need the same scan, review, approval, and guardrail model as folders. Never extract archive entries blindly.

**Current ZIP support is inventory-only.** Single-file ZIP ingest creates a safe listing note and does not extract child files. Full archive-as-source import still needs its own scan, review, temporary extraction, and archive-child manifest behavior.

**This doc is in progress.** Step 29a and 29b exist, and 29c now has reusable extractors plus the first approved folder import worker. Later slices still own full archive extraction, remove/re-import/cancel lifecycle actions, richer extractor quality, sample data, and batch-scoped chat.

## Related

- [Step 29 - Folder and Source Import Demo Flow](../steps/step-29-folder-source-import-demo-flow.spec.md)
- [Memory System](memory.md)
- [PDF Section Split](pdf-section-split.md)
- [Smart Connect](smart-connect.md)
- [Retrieval Trace UI](retrieval-trace.md)
