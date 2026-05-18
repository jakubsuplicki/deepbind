<template>
  <div class="import-dialog__batch">
    <div class="import-dialog__batch-header">
      <span>{{ statusLabel }}</span>
      <strong>
        {{ batch.imported_file_count }}/{{ batch.total_file_count }}
      </strong>
    </div>
    <div class="import-dialog__batch-bar">
      <span :style="{ width: progressPercent }" />
    </div>
    <div class="import-dialog__batch-meta">
      {{ batch.created_note_count }} notes
      <span v-if="batch.skipped_file_count">
        · {{ batch.skipped_file_count }} skipped
      </span>
      <span v-if="batch.failed_file_count">
        · {{ batch.failed_file_count }} failed
      </span>
    </div>
    <div v-if="batch.current_file" class="import-dialog__source-path">
      {{ batch.current_file }}
    </div>
    <div class="import-dialog__source-path">
      {{ batch.destination_root }}
    </div>
    <div
      v-if="canCancel || canRemove || canRescan"
      class="import-dialog__batch-actions"
    >
      <button
        v-if="canCancel"
        type="button"
        class="import-dialog__stop-btn"
        :disabled="cancelling"
        @click="$emit('cancel')"
      >
        <Icon name="ph:stop-bold" class="icon--sm" />
        {{ cancelling ? 'Cancelling...' : 'Cancel import' }}
      </button>
      <button
        v-if="canRescan"
        type="button"
        class="import-dialog__rescan-btn"
        :disabled="rescanning"
        @click="$emit('rescan')"
      >
        <Icon name="ph:arrows-clockwise-bold" class="icon--sm" />
        {{ rescanning ? 'Scanning...' : 'Scan again' }}
      </button>
      <button
        v-if="canRemove"
        type="button"
        class="import-dialog__remove-btn"
        :disabled="removing"
        @click="$emit('remove')"
      >
        <Icon name="ph:trash-bold" class="icon--sm" />
        {{ removing ? 'Removing...' : 'Remove import' }}
      </button>
    </div>
  </div>

  <div
    v-if="completion || completionLoading"
    class="import-dialog__completion"
  >
    <div class="import-dialog__completion-header">
      <span>Ready to ask</span>
      <strong>
        {{ completion?.created_note_count ?? batch.created_note_count }}
        {{ (completion?.created_note_count ?? batch.created_note_count) === 1 ? 'note' : 'notes' }}
      </strong>
    </div>
    <div v-if="completionLoading" class="import-dialog__progress">
      Preparing import summary...
    </div>
    <template v-else-if="completion">
      <div class="import-dialog__completion-grid">
        <div class="import-dialog__completion-stat">
          <span>Imported</span>
          <strong>{{ completion.imported_file_count }}</strong>
        </div>
        <div class="import-dialog__completion-stat">
          <span>Duplicates</span>
          <strong>{{ completion.duplicate_file_count }}</strong>
        </div>
        <div class="import-dialog__completion-stat">
          <span>Skipped</span>
          <strong>{{ completion.skipped_file_count }}</strong>
        </div>
        <div class="import-dialog__completion-stat">
          <span>Failed</span>
          <strong>{{ completion.failed_file_count }}</strong>
        </div>
      </div>
      <div
        v-if="(completion.warning_file_count ?? 0) > 0"
        class="import-dialog__chips"
      >
        <span class="import-dialog__chip import-dialog__chip--warn">
          Imported with warnings {{ completion.warning_file_count }}
        </span>
      </div>
      <div
        v-if="importedNotePaths.length"
        class="import-dialog__completion-actions"
      >
        <button
          type="button"
          class="import-dialog__view-notes-btn"
          @click="$emit('view-notes', importedNotePaths)"
        >
          <Icon name="ph:books-bold" class="icon--sm" />
          View imported notes
        </button>
      </div>
      <div
        v-if="warningRows.length"
        class="import-dialog__warning-review"
      >
        <div class="import-dialog__warning-title">
          Check imported notes
        </div>
        <ul class="import-dialog__warning-list">
          <li
            v-for="file in warningRows"
            :key="file.file_id"
            class="import-dialog__warning-file"
          >
            <span class="import-dialog__file-name" :title="file.relpath">
              {{ file.relpath }}
            </span>
            <span class="import-dialog__warning-text">
              {{ file.summary }}
            </span>
          </li>
        </ul>
        <p
          v-if="warningFileCount > warningRows.length"
          class="import-dialog__sublabel"
        >
          Showing {{ warningRows.length }} of {{ warningFileCount }} warning files.
        </p>
      </div>
      <div v-if="typeRows.length" class="import-dialog__chips">
        <span
          v-for="row in typeRows"
          :key="row.extension"
          class="import-dialog__chip"
        >
          {{ row.extension }} {{ row.count }}
        </span>
      </div>
      <div
        v-if="completion.can_ask_about_import && questionRows.length"
        class="import-dialog__questions"
      >
        <button
          v-for="question in questionRows"
          :key="question.question"
          type="button"
          class="import-dialog__question-btn"
          @click="$emit('ask-question', question.question)"
        >
          {{ question.question }}
        </button>
      </div>
    </template>
  </div>

  <div
    v-if="problemCount > 0 || reviewLoading"
    class="import-dialog__issues"
  >
    <div class="import-dialog__issues-header">
      <span>Skipped and failed files</span>
      <strong>
        {{ problemCount }}
        {{ problemCount === 1 ? 'file' : 'files' }}
      </strong>
    </div>
    <div class="import-dialog__issues-grid">
      <div class="import-dialog__issues-stat">
        <span>Skipped</span>
        <strong>{{ batch.skipped_file_count }}</strong>
      </div>
      <div class="import-dialog__issues-stat">
        <span>Failed</span>
        <strong>{{ batch.failed_file_count }}</strong>
      </div>
    </div>
    <div v-if="reviewReasonRows.length" class="import-dialog__chips">
      <span
        v-for="row in reviewReasonRows"
        :key="row.reason"
        class="import-dialog__chip import-dialog__chip--warn"
      >
        {{ humanizeSourceImportReason(row.reason) }} {{ row.count }}
      </span>
    </div>
    <div v-if="reviewLoading" class="import-dialog__progress">
      Preparing skipped file review...
    </div>
    <ul v-else-if="reviewRows.length" class="import-dialog__issue-list">
      <li
        v-for="file in reviewRows"
        :key="`${file.status}-${file.file_id}`"
        class="import-dialog__issue-file"
        :class="`import-dialog__issue-file--${file.status}`"
      >
        <div class="import-dialog__issue-main">
          <span class="import-dialog__file-name" :title="file.relpath">
            {{ file.relpath }}
          </span>
          <span class="import-dialog__issue-status">
            {{ humanizeSourceImportReason(file.status) }}
          </span>
        </div>
        <div class="import-dialog__issue-meta">
          {{ formatBytes(file.size) }}
          <span v-if="file.reason">{{ humanizeSourceImportReason(file.reason) }}</span>
        </div>
        <div class="import-dialog__issue-hint">
          {{ folderIssueActionHint(file) }}
        </div>
      </li>
    </ul>
    <p
      v-if="!reviewLoading && reviewTruncated"
      class="import-dialog__sublabel"
    >
      Showing the first {{ reviewRows.length }} files from a larger review.
    </p>
  </div>

  <div v-if="rescan" class="import-dialog__rescan">
    <div class="import-dialog__rescan-header">
      <span>Since last import</span>
      <strong>
        {{ rescan.importable_file_count }}
        {{ rescan.importable_file_count === 1 ? 'file' : 'files' }}
      </strong>
    </div>
    <div class="import-dialog__rescan-grid">
      <div
        v-for="row in rescanStatusRows"
        :key="row.label"
        class="import-dialog__rescan-stat"
      >
        <span>{{ row.label }}</span>
        <strong>{{ row.count }}</strong>
      </div>
    </div>
    <p v-if="rescan.missing_file_count" class="import-dialog__sublabel">
      Missing files are reported only; they are not removed.
    </p>
    <ul v-if="rescan.files.length" class="import-dialog__scan-files">
      <li
        v-for="file in rescan.files"
        :key="`${file.status}-${file.id}`"
        class="import-dialog__scan-file"
        :class="`import-dialog__rescan-file--${file.status}`"
      >
        <div class="import-dialog__scan-file-row">
          <span class="import-dialog__file-name" :title="file.relpath">
            {{ file.relpath }}
          </span>
          <span class="import-dialog__file-meta">
            {{ formatBytes(file.status === 'missing' ? (file.previous_size ?? 0) : file.size) }}
            <span v-if="file.reason">{{ humanizeSourceImportReason(file.reason) }}</span>
            <span v-else>{{ humanizeSourceImportReason(file.status) }}</span>
          </span>
        </div>
        <div
          v-if="folderSourceImportActionHint(file)"
          class="import-dialog__scan-file-hint"
        >
          {{ folderSourceImportActionHint(file) }}
        </div>
      </li>
    </ul>
    <p v-if="rescan.file_list_truncated" class="import-dialog__sublabel">
      Showing the first {{ rescan.files.length }} files from a larger rescan.
    </p>
    <div v-if="rescan.importable_file_count > 0" class="import-dialog__batch-actions">
      <button
        type="button"
        class="import-dialog__change-btn"
        :disabled="rescanImportStarting"
        @click="$emit('start-rescan-import')"
      >
        <Icon name="ph:upload-simple-bold" class="icon--sm" />
        {{ rescanImportStarting ? 'Importing...' : 'Import changes' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  folderIssueActionHint,
  folderSourceImportActionHint,
  humanizeSourceImportReason,
} from '~/composables/useFolderSourceImportDialog'
import type {
  SourceImportBatchSummary,
  SourceImportCompletionSummary,
  SourceImportFileReviewItem,
  SourceImportRescanReport,
  SourceImportSuggestedQuestion,
} from '~/composables/useSourceImport'

const props = defineProps<{
  batch: SourceImportBatchSummary
  statusLabel: string
  progressPercent: string
  canCancel: boolean
  canRemove: boolean
  canRescan: boolean
  cancelling: boolean
  rescanning: boolean
  removing: boolean
  completion: SourceImportCompletionSummary | null
  completionLoading: boolean
  typeRows: Array<{ extension: string; count: number }>
  questionRows: SourceImportSuggestedQuestion[]
  problemCount: number
  reviewLoading: boolean
  reviewReasonRows: Array<{ reason: string; count: number }>
  reviewRows: SourceImportFileReviewItem[]
  reviewTruncated: boolean
  rescan: SourceImportRescanReport | null
  rescanStatusRows: Array<{ label: string; count: number }>
  rescanImportStarting: boolean
}>()

defineEmits<{
  cancel: []
  rescan: []
  remove: []
  'ask-question': [question: string]
  'start-rescan-import': []
  'view-notes': [notePaths: string[]]
}>()

const importedNotePaths = computed(() => {
  const paths = props.batch.files.flatMap(file => file.note_paths ?? [])
  return Array.from(new Set(paths)).filter(path => path.length > 0)
})

const warningFiles = computed(() =>
  props.batch.files.filter(file => (file.warnings ?? []).length > 0)
)

const warningFileCount = computed(() =>
  props.completion?.warning_file_count ?? props.batch.warning_file_count ?? warningFiles.value.length
)

const warningRows = computed(() =>
  warningFiles.value.slice(0, 4).map(file => ({
    file_id: file.file_id,
    relpath: file.relpath,
    summary: (file.warnings ?? []).slice(0, 2).join('; '),
  }))
)

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  const precision = value >= 10 || unit === 0 ? 0 : 1
  return `${value.toFixed(precision)} ${units[unit]}`
}
</script>

<style scoped>
.import-dialog__source-path {
  margin-top: 0.2rem;
  font-size: 0.74rem;
  opacity: 0.65;
  overflow-wrap: anywhere;
}
.import-dialog__batch {
  padding: 0.7rem;
  border: 1px solid rgba(96, 165, 250, 0.38);
  border-radius: 6px;
  background: rgba(96, 165, 250, 0.045);
}
.import-dialog__batch-header {
  display: flex;
  justify-content: space-between;
  gap: 0.75rem;
  font-size: 0.82rem;
}
.import-dialog__batch-header strong {
  flex: 0 0 auto;
}
.import-dialog__batch-bar {
  height: 6px;
  margin-top: 0.5rem;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.12);
}
.import-dialog__batch-bar span {
  display: block;
  height: 100%;
  min-width: 4px;
  background: #60a5fa;
  transition: width 0.2s ease;
}
.import-dialog__batch-meta {
  margin-top: 0.45rem;
  font-size: 0.74rem;
  opacity: 0.72;
}
.import-dialog__batch-actions {
  margin-top: 0.65rem;
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
}
.import-dialog__stop-btn,
.import-dialog__rescan-btn,
.import-dialog__change-btn,
.import-dialog__remove-btn {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.35rem 0.65rem;
  border: 1px solid rgba(239, 68, 68, 0.5);
  border-radius: 4px;
  background: rgba(239, 68, 68, 0.08);
  color: #fca5a5;
  cursor: pointer;
  font: inherit;
  font-size: 0.76rem;
}
.import-dialog__stop-btn {
  border-color: rgba(245, 158, 11, 0.5);
  background: rgba(245, 158, 11, 0.08);
  color: #fbbf24;
}
.import-dialog__rescan-btn {
  border-color: rgba(96, 165, 250, 0.5);
  background: rgba(96, 165, 250, 0.08);
  color: #93c5fd;
}
.import-dialog__change-btn {
  border-color: rgba(34, 197, 94, 0.5);
  background: rgba(34, 197, 94, 0.08);
  color: #86efac;
}
.import-dialog__stop-btn:disabled,
.import-dialog__rescan-btn:disabled,
.import-dialog__change-btn:disabled,
.import-dialog__remove-btn:disabled {
  opacity: 0.55;
  cursor: wait;
}
.import-dialog__completion {
  padding: 0.7rem;
  border: 1px solid rgba(34, 197, 94, 0.32);
  border-radius: 6px;
  background: rgba(34, 197, 94, 0.035);
}
.import-dialog__completion-header {
  display: flex;
  justify-content: space-between;
  gap: 0.75rem;
  font-size: 0.82rem;
}
.import-dialog__completion-header strong {
  flex: 0 0 auto;
}
.import-dialog__completion-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.4rem;
  margin-top: 0.55rem;
  margin-bottom: 0.55rem;
}
.import-dialog__completion-stat {
  padding: 0.45rem;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.025);
}
.import-dialog__completion-stat span {
  display: block;
  font-size: 0.64rem;
  opacity: 0.62;
}
.import-dialog__completion-stat strong {
  display: block;
  margin-top: 0.12rem;
  font-size: 0.88rem;
}
.import-dialog__completion-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 0.55rem;
}
.import-dialog__view-notes-btn {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.38rem 0.65rem;
  border: 1px solid rgba(34, 197, 94, 0.42);
  border-radius: 4px;
  background: rgba(34, 197, 94, 0.08);
  color: #86efac;
  cursor: pointer;
  font: inherit;
  font-size: 0.76rem;
}
.import-dialog__questions {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-top: 0.65rem;
}
.import-dialog__question-btn {
  width: 100%;
  min-height: 2rem;
  padding: 0.45rem 0.55rem;
  border: 1px solid rgba(34, 197, 94, 0.34);
  border-radius: 4px;
  background: rgba(34, 197, 94, 0.07);
  color: inherit;
  cursor: pointer;
  font: inherit;
  font-size: 0.76rem;
  line-height: 1.25;
  text-align: left;
}
.import-dialog__warning-review {
  margin-top: 0.6rem;
  padding: 0.55rem 0.6rem;
  border: 1px solid rgba(245, 158, 11, 0.28);
  border-radius: 4px;
  background: rgba(245, 158, 11, 0.035);
}
.import-dialog__warning-title {
  font-size: 0.76rem;
  color: #fbbf24;
}
.import-dialog__warning-list {
  list-style: none;
  margin: 0.4rem 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.32rem;
}
.import-dialog__warning-file {
  display: grid;
  grid-template-columns: minmax(0, 0.42fr) minmax(0, 0.58fr);
  gap: 0.5rem;
  align-items: baseline;
  font-size: 0.74rem;
}
.import-dialog__warning-text {
  min-width: 0;
  opacity: 0.78;
  overflow-wrap: anywhere;
}
.import-dialog__issues {
  padding: 0.7rem;
  border: 1px solid rgba(245, 158, 11, 0.35);
  border-radius: 6px;
  background: rgba(245, 158, 11, 0.04);
}
.import-dialog__issues-header {
  display: flex;
  justify-content: space-between;
  gap: 0.75rem;
  font-size: 0.82rem;
}
.import-dialog__issues-header strong {
  flex: 0 0 auto;
}
.import-dialog__issues-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.4rem;
  margin-top: 0.55rem;
  margin-bottom: 0.5rem;
}
.import-dialog__issues-stat {
  padding: 0.45rem;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.025);
}
.import-dialog__issues-stat span {
  display: block;
  font-size: 0.64rem;
  opacity: 0.62;
}
.import-dialog__issues-stat strong {
  display: block;
  margin-top: 0.12rem;
  font-size: 0.88rem;
}
.import-dialog__issue-list {
  list-style: none;
  margin: 0.65rem 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  max-height: 230px;
  overflow-y: auto;
}
.import-dialog__issue-file {
  padding: 0.45rem 0.55rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.02);
}
.import-dialog__issue-file--failed {
  border-left: 3px solid #ef4444;
}
.import-dialog__issue-file--skipped {
  border-left: 3px solid #f59e0b;
}
.import-dialog__issue-main {
  display: flex;
  justify-content: space-between;
  gap: 0.5rem;
  font-size: 0.8rem;
}
.import-dialog__issue-status {
  flex: 0 0 auto;
  font-size: 0.68rem;
  opacity: 0.68;
  text-transform: uppercase;
}
.import-dialog__issue-meta {
  display: flex;
  gap: 0.45rem;
  margin-top: 0.18rem;
  font-size: 0.72rem;
  opacity: 0.68;
}
.import-dialog__issue-hint {
  margin-top: 0.22rem;
  font-size: 0.74rem;
  opacity: 0.82;
}
.import-dialog__rescan {
  padding: 0.7rem;
  border: 1px solid rgba(34, 197, 94, 0.32);
  border-radius: 6px;
  background: rgba(34, 197, 94, 0.035);
}
.import-dialog__rescan-header {
  display: flex;
  justify-content: space-between;
  gap: 0.75rem;
  font-size: 0.82rem;
}
.import-dialog__rescan-header strong {
  flex: 0 0 auto;
}
.import-dialog__rescan-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.4rem;
  margin-top: 0.55rem;
}
.import-dialog__rescan-stat {
  padding: 0.45rem;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.025);
}
.import-dialog__rescan-stat span {
  display: block;
  font-size: 0.64rem;
  opacity: 0.62;
}
.import-dialog__rescan-stat strong {
  display: block;
  margin-top: 0.12rem;
  font-size: 0.88rem;
}
.import-dialog__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
}
.import-dialog__chip {
  padding: 0.2rem 0.45rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.025);
  font-size: 0.72rem;
}
.import-dialog__chip--warn {
  border-color: rgba(245, 158, 11, 0.45);
  color: #fbbf24;
}
.import-dialog__scan-files {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  max-height: 210px;
  overflow-y: auto;
}
.import-dialog__scan-file {
  padding: 0.4rem 0.55rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.02);
  font-size: 0.8rem;
}
.import-dialog__scan-file-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}
.import-dialog__scan-file-hint {
  margin-top: 0.22rem;
  font-size: 0.73rem;
  line-height: 1.28;
  opacity: 0.72;
}
.import-dialog__rescan-file--new {
  border-left: 3px solid #22c55e;
}
.import-dialog__rescan-file--changed {
  border-left: 3px solid #60a5fa;
}
.import-dialog__rescan-file--missing {
  border-left: 3px solid #ef4444;
}
.import-dialog__rescan-file--unsupported {
  border-left: 3px solid #f59e0b;
}
.import-dialog__rescan-file--skipped,
.import-dialog__rescan-file--unchanged {
  border-left: 3px solid #64748b;
}
.import-dialog__file-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}
.import-dialog__file-meta {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.75rem;
  opacity: 0.75;
  white-space: nowrap;
}
.import-dialog__sublabel {
  font-size: 0.75rem;
  opacity: 0.6;
  margin: 0.35rem 0 0;
}
.import-dialog__progress {
  margin-top: 0.75rem;
  opacity: 0.7;
}
</style>
