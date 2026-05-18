---
title: Folder Source Import Demo Smoke
type: runbook
status: active
last_updated: 2026-05-18
related_features:
  - folder-source-import
  - memory
  - retrieval-trace
---

# Folder Source Import Demo Smoke

This runbook verifies the buyer-facing folder/source import demo flow end to end. It is meant for a packaged desktop build or a local desktop dev run, and it checks the trust contract as much as the happy path:

```text
choose source -> metadata scan -> review/exclude -> approve import -> local memory -> scoped question
```

The runbook does not prove formal compliance certification. It verifies the local-first product behavior currently implemented by Folder Source Import.

## Preconditions

- Start from a clean or disposable workspace so imported demo notes are easy to inspect and remove.
- Use the desktop app, not a raw browser-only backend call, because source selection depends on the trusted Tauri picker/grant path.
- Turn off network access for the offline smoke pass after the app has already been installed and any required bundled/local model setup is complete.
- Keep Finder open to the workspace `memory/` folder so Markdown output can be inspected directly.

## 1. Bundled Sample Dataset

Goal: prove a first-time buyer can run the product demo without exposing their own files.

1. Open Memory, then open Import.
2. Select Folder mode.
3. Click `Use sample data`.
4. Confirm the app shows metadata-scan copy before any import starts.
5. Run the scan.
6. Verify the review screen shows a mixed sample folder with supported documents and archive children.
7. Approve the default import.
8. Wait for completion.

Pass criteria:

- The sample dataset is not auto-scanned or auto-imported before user action.
- Completion shows imported/skipped/failed/duplicate/warning counts.
- Suggested questions appear.
- `View imported notes` opens Memory to the generated import folder.
- Generated Markdown lives under `memory/imports/...`.
- Markdown frontmatter uses source-relative provenance and does not include the absolute sample resource path.

## 2. Mixed Local Folder

Goal: prove the realistic local-folder path works across common business files.

Create or choose a test folder containing:

- `.md`, `.txt`, `.pdf`
- `.csv`, `.json`, `.xml`
- `.docx`, `.xlsx`, `.pptx`
- `.html` or `.htm`
- `.rtf`, `.eml`
- `.zip` with at least one supported child file
- at least one unsupported binary

Run the folder picker, scan, review, and import.

Pass criteria:

- The scan is metadata-only before approval.
- Unsupported files appear as skipped/unsupported, not silently ignored.
- ZIP children appear as reviewable child rows and only approved children are extracted.
- Business-format extractor warnings, if present, are visible in the completion surface.
- Imported notes remain searchable in Memory.

## 3. Scan Review And Issue Report

Goal: prove a non-technical user can understand and share repair work.

Use a folder with at least two issue types, such as an unsupported file and an encrypted/password-protected archive.

Pass criteria:

- The review screen shows a needs-attention summary before import.
- Row labels are buyer-readable, not raw reason codes.
- Repair hints explain what to do locally.
- `Copy report` copies grouped reasons, relative affected paths, and hints.
- The copied report does not include the absolute selected source root.

## 4. OneDrive Or Synced-Folder Offline Pass

Goal: prove local synced folders behave like local sources without turning into cloud connector work.

Use a OneDrive, Dropbox, Google Drive for desktop, SharePoint-synced, or similar local sync folder. Include at least one file that is online-only or otherwise not downloaded locally, then disconnect from the network.

Pass criteria:

- The local folder can be selected through the desktop picker.
- Online-only or unreadable local placeholders are skipped with a plain-language reason.
- The app does not try to download placeholder files.
- Already-downloaded files can still be imported while offline.
- No cloud OAuth, cloud API, or connector flow appears.

## 5. Re-Import And Duplicate Handling

Goal: prove the user can safely come back to a previous source.

After a completed import:

1. Add one new file.
2. Modify one previously imported file.
3. Delete or move one previously imported file.
4. Add a duplicate copy of an already imported file.
5. Reopen the import from Recent source imports.
6. Click `Scan again`.
7. Review and approve the import-changes flow.

Pass criteria:

- Rescan reports new, changed, unchanged, and missing files.
- Missing files are reported but do not delete prior Markdown automatically.
- Duplicate content is skipped by default after approval.
- The UI offers the explicit duplicate-content choice when appropriate.
- Changed/new files import into a new batch without overwriting older generated notes.

## 6. Cancellation And Interrupted Batch

Goal: prove long-running imports fail recoverably.

Use a folder large enough to keep import active for several seconds.

1. Start import.
2. Cancel while files are queued.
3. Verify completed files remain visible.
4. Start another import and quit the app mid-import.
5. Reopen the app and open Recent source imports.

Pass criteria:

- Cancellation lets the current file finish or fail cleanly, then skips queued files.
- The batch is marked cancelled or interrupted, not completed.
- The user can remove the partial batch.
- Removing the batch only archives notes created by that batch.

## 7. Removal Safety

Goal: prove trial import is low-risk.

Before removing an import, create an unrelated note in Memory near the import destination or with links to imported notes. Then remove the import batch from the completion/history surface.

Pass criteria:

- The remove action asks for confirmation.
- Notes created by the import move to `.trash/` or are otherwise removed through the normal soft-delete path.
- Unrelated user-created notes remain.
- Derived search, embedding, graph, and suggestion data for removed paths are refreshed or cleared.
- The operational manifest remains as a local audit record without preserving file contents in user-facing notes.

## 8. Same Display Name Collision

Goal: prove two sources with the same friendly name do not overwrite each other.

Import two different folders or archives with the same display name from different parent paths.

Pass criteria:

- Both imports complete.
- Destination folders are disambiguated with a short batch suffix or equivalent safe naming.
- Memory can open notes from both imports.
- No imported Markdown from the first source is overwritten by the second.

## 9. Scoped Question

Goal: prove the demo answer is grounded in the folder the user just imported.

From the completion screen, click a suggested question or ask a question using the import action.

Pass criteria:

- Chat starts with the current `import_batch_id` scope.
- Retrieval traces show `via="import_batch"` or an equivalent import-scope signal.
- Evidence comes from notes created by the selected batch.
- The answer does not quietly pull unrelated older memory unless the user explicitly broadens scope.

## Final Release Gate

The flow is demo-ready when all smoke sections above pass on:

- the bundled sample dataset
- at least one local mixed business folder
- at least one local synced-folder/offline scenario
- a packaged desktop build, not only the dev server

Record any failed section with the app build, workspace path, selected source shape, visible user-facing message, and whether generated Markdown contains only source-relative provenance.
