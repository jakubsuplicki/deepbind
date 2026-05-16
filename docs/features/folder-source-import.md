---
title: Folder Source Import
status: in-progress
type: feature
sources:
  - backend/routers/source_import.py
  - backend/services/source_import/grants.py
  - backend/services/source_import/models.py
  - backend/services/source_import/scan.py
  - backend/services/source_import/store.py
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

Step 29a is implemented as the first vertical slice: the desktop shell can grant a native folder selection to the backend, the backend rejects untrusted raw scan paths, and the import dialog can show a metadata-only folder inventory. Content import, exclusions, expanded document extractors, import manifests, removal, re-import, dedupe, and batch-scoped chat are still planned work.

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

### Batch manifest

Every import should create a local operational manifest keyed by `import_batch_id`. This manifest is app metadata, not the user's knowledge source of truth. It can store the selected source root, scan options, approved file ids, content hashes after approval, generated note paths, skipped files, warnings, and per-file outcomes.

SQLite operational tables are the preferred storage for this manifest. Markdown remains canonical for user knowledge; the manifest exists for lifecycle, progress, dedupe, and recovery. If the manifest is lost, imported Markdown should still be usable after reindex, but remove/re-import history may be unavailable.

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

The current memory ingest path already supports `.md`, `.txt`, `.pdf`, `.csv`, `.xml`, and `.json`. Folder Source Import should add practical business document support:

- `.docx` via `python-docx`, pending license and bundle-size review
- `.xlsx` via `openpyxl`, pending license and bundle-size review
- `.pptx` via `python-pptx`, pending license and bundle-size review
- `.html` and `.htm` via the existing `trafilatura` plus `markdownify` stack
- `.rtf` via `striprtf`, pending license and bundle-size review
- `.eml` via Python's standard `email` package, with attachments recorded as metadata only
- `.zip` as a container source with scan-before-extract, zip-slip protection, decompression limits, file count limits, depth limits, and nested archives disabled by default

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

- `backend/routers/source_import.py` - REST API for trusted source grants, metadata scans, and cached scan reports.
- `backend/services/source_import/grants.py` - Short-lived in-memory source grants created only from the trusted desktop picker path.
- `backend/services/source_import/scan.py` - Metadata-only directory scanner; counts supported/skipped/unsupported files without opening file contents.
- `backend/services/source_import/store.py` - Temporary in-memory scan report cache for the review screen.
- `backend/services/source_import/models.py` - Pydantic request/response models for grants and scan reports.
- `desktop/src-tauri/Cargo.toml` - Adds `getrandom` for strong shell-to-sidecar grant token generation.
- `desktop/src-tauri/src/lib.rs` - `source_import_pick_folder` command: native macOS folder picker plus shell-authenticated grant registration.
- `desktop/scripts/dev.sh` - Shares the dev source-import grant token between the sidecar and Tauri shell.
- `frontend/app/composables/useSourceImport.ts` - Frontend wrapper for desktop folder picking and `/api/source-import/scan`.
- `frontend/app/components/ImportDialog.vue` - Adds the Folder mode and metadata review summary to the existing import dialog.
- `backend/tests/test_source_import_scan.py` - Backend coverage for grant auth, metadata-only scanning, single-use tokens, limits, and symlink skips.
- `frontend/tests/components/ImportDialog.test.ts` - Frontend coverage for the new Folder mode surface.

Planned later files:

- `backend/services/source_import/extractors.py` - Extension-to-Markdown extractor registry.
- `backend/services/source_import/manifest.py` - Import batch manifest, provenance, and lifecycle metadata.
- `backend/services/source_import/dedupe.py` - Approved-file hashing and duplicate/re-import comparison.
- `backend/services/source_import/limits.py` - Scan, file, archive, and concurrency limits.
- `backend/services/source_import/worker.py` - Bulk import queue, progress updates, cancellation, and per-file outcomes.

## API / Interface

Planned REST surface:

```text
POST   /api/source-import/grants
POST   /api/source-import/scan
GET    /api/source-import/scans/{scan_id}
POST   /api/source-import/scans/{scan_id}/start
GET    /api/source-import/imports
GET    /api/source-import/imports/{batch_id}
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

**This doc is in progress.** Step 29a exists, but later slices still own exclusions, content import, lifecycle actions, richer extractors, and batch-scoped chat.

## Related

- [Step 29 - Folder and Source Import Demo Flow](../steps/step-29-folder-source-import-demo-flow.spec.md)
- [Memory System](memory.md)
- [PDF Section Split](pdf-section-split.md)
- [Smart Connect](smart-connect.md)
- [Retrieval Trace UI](retrieval-trace.md)
