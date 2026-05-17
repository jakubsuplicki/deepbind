<template>
  <div class="import-dialog" v-if="visible">
    <div class="import-dialog__backdrop" @click="$emit('close')" />
    <div class="import-dialog__panel">
      <h2 class="import-dialog__title">Import</h2>

      <!-- Mode switch -->
      <div class="import-dialog__modes">
        <button
          type="button"
          class="import-dialog__mode-btn"
          :class="{ 'import-dialog__mode-btn--active': mode === 'generic' }"
          @click="setMode('generic')"
        >
          Files
        </button>
        <button
          type="button"
          class="import-dialog__mode-btn"
          :class="{ 'import-dialog__mode-btn--active': mode === 'folder' }"
          @click="setMode('folder')"
        >
          Folder
        </button>
        <button
          type="button"
          class="import-dialog__mode-btn"
          :class="{ 'import-dialog__mode-btn--active': mode === 'jira' }"
          @click="setMode('jira')"
        >
          Jira
        </button>
      </div>

      <p class="import-dialog__hint">
        <template v-if="mode === 'generic'">
          Saves the file as a note in your memory. Works for Markdown, text, PDF, CSV/XML.
          Embeddings + FTS are generated automatically.
        </template>
        <template v-else-if="mode === 'folder'">
          DeepFilesAI will first scan file names, types, sizes, and folders.
          File contents are imported only after you approve.
        </template>
        <template v-else>
          Full Jira pipeline: per-issue notes under <code>memory/jira/{PROJECT}/</code>,
          structured DB (issues, links, history), embeddings, and graph relations.
          Accepts Jira XML or CSV exports.
        </template>
      </p>

      <div v-if="mode === 'folder'" class="import-dialog__source">
        <div class="import-dialog__source-actions">
          <button
            type="button"
            class="import-dialog__browse-btn"
            :disabled="folderPicking || folderSamplePicking || folderScanning"
            @click="chooseFolderSource"
          >
            <Icon name="ph:folder-open-bold" class="icon--sm" />
            {{ folderPicking ? 'Opening...' : 'Choose folder' }}
          </button>
          <button
            type="button"
            class="import-dialog__sample-btn"
            :disabled="folderPicking || folderSamplePicking || folderScanning"
            @click="chooseSampleDataset"
          >
            <Icon name="ph:sparkle-bold" class="icon--sm" />
            {{ folderSamplePicking ? 'Preparing...' : 'Use sample data' }}
          </button>
        </div>

        <div v-if="folderGrant" class="import-dialog__source-card">
          <div>
            <div class="import-dialog__source-name">{{ folderGrant.display_name }}</div>
            <div class="import-dialog__source-path">{{ folderGrant.root_path }}</div>
          </div>
          <span class="import-dialog__source-badge">metadata only</span>
        </div>

        <label class="import-dialog__toggle">
          <input
            v-model="includeHiddenInFolderScan"
            type="checkbox"
            :disabled="folderScanning || !!folderScan"
          />
          <span>Include hidden files and folders</span>
        </label>

        <div v-if="folderScanning" class="import-dialog__progress">
          Scanning file names, types, sizes, and folders...
        </div>

        <div v-if="folderScan" class="import-dialog__scan">
          <div class="import-dialog__scan-grid">
            <div class="import-dialog__scan-stat">
              <span>Supported</span>
              <strong>{{ folderScan.supported_file_count }}</strong>
            </div>
            <div class="import-dialog__scan-stat">
              <span>Unsupported</span>
              <strong>{{ folderScan.unsupported_file_count }}</strong>
            </div>
            <div class="import-dialog__scan-stat">
              <span>Skipped</span>
              <strong>{{ folderScan.skipped_file_count }}</strong>
            </div>
            <div class="import-dialog__scan-stat">
              <span>Total size</span>
              <strong>{{ formatBytes(folderScan.total_size_seen) }}</strong>
            </div>
          </div>

          <div v-if="folderSelection" class="import-dialog__approval">
            <div class="import-dialog__approval-stat">
              <span>Selected for import</span>
              <strong>
                {{ folderSelection.approved_file_count }}
                <small>{{ formatBytes(folderSelection.approved_total_size) }}</small>
              </strong>
            </div>
            <div class="import-dialog__approval-stat">
              <span>Excluded by review</span>
              <strong>
                {{ folderSelection.excluded_file_count }}
                <small>{{ formatBytes(folderSelection.excluded_total_size) }}</small>
              </strong>
            </div>
            <p class="import-dialog__sublabel">
              File contents stay unread until the approved import step.
            </p>
          </div>
          <div v-else-if="folderSelectionLoading" class="import-dialog__progress">
            Updating review...
          </div>

          <div v-if="folderImport" class="import-dialog__batch">
            <div class="import-dialog__batch-header">
              <span>{{ folderImportStatusLabel }}</span>
              <strong>
                {{ folderImport.imported_file_count }}/{{ folderImport.total_file_count }}
              </strong>
            </div>
            <div class="import-dialog__batch-bar">
              <span :style="{ width: folderImportProgressPercent }" />
            </div>
            <div class="import-dialog__batch-meta">
              {{ folderImport.created_note_count }} notes
              <span v-if="folderImport.skipped_file_count">
                · {{ folderImport.skipped_file_count }} skipped
              </span>
              <span v-if="folderImport.failed_file_count">
                · {{ folderImport.failed_file_count }} failed
              </span>
            </div>
            <div v-if="folderImport.current_file" class="import-dialog__source-path">
              {{ folderImport.current_file }}
            </div>
            <div class="import-dialog__source-path">
              {{ folderImport.destination_root }}
            </div>
            <div
              v-if="folderImportCanCancel || folderImportCanRemove || folderImportCanRescan"
              class="import-dialog__batch-actions"
            >
              <button
                v-if="folderImportCanCancel"
                type="button"
                class="import-dialog__stop-btn"
                :disabled="folderImportCancelling"
                @click="cancelFolderImport"
              >
                <Icon name="ph:stop-bold" class="icon--sm" />
                {{ folderImportCancelling ? 'Cancelling...' : 'Cancel import' }}
              </button>
              <button
                v-if="folderImportCanRescan"
                type="button"
                class="import-dialog__rescan-btn"
                :disabled="folderRescanning"
                @click="rescanFolderImport"
              >
                <Icon name="ph:arrows-clockwise-bold" class="icon--sm" />
                {{ folderRescanning ? 'Scanning...' : 'Scan again' }}
              </button>
              <button
                v-if="folderImportCanRemove"
                type="button"
                class="import-dialog__remove-btn"
                :disabled="folderImportRemoving"
                @click="requestRemoveFolderImport"
              >
                <Icon name="ph:trash-bold" class="icon--sm" />
                {{ folderImportRemoving ? 'Removing...' : 'Remove import' }}
              </button>
            </div>
          </div>

          <div
            v-if="folderImportCompletion || folderImportCompletionLoading"
            class="import-dialog__completion"
          >
            <div class="import-dialog__completion-header">
              <span>Ready to ask</span>
              <strong>
                {{ folderImportCompletion?.created_note_count ?? folderImport?.created_note_count ?? 0 }}
                {{ (folderImportCompletion?.created_note_count ?? folderImport?.created_note_count ?? 0) === 1 ? 'note' : 'notes' }}
              </strong>
            </div>
            <div v-if="folderImportCompletionLoading" class="import-dialog__progress">
              Preparing import summary...
            </div>
            <template v-else-if="folderImportCompletion">
              <div class="import-dialog__completion-grid">
                <div class="import-dialog__completion-stat">
                  <span>Imported</span>
                  <strong>{{ folderImportCompletion.imported_file_count }}</strong>
                </div>
                <div class="import-dialog__completion-stat">
                  <span>Duplicates</span>
                  <strong>{{ folderImportCompletion.duplicate_file_count }}</strong>
                </div>
                <div class="import-dialog__completion-stat">
                  <span>Skipped</span>
                  <strong>{{ folderImportCompletion.skipped_file_count }}</strong>
                </div>
                <div class="import-dialog__completion-stat">
                  <span>Failed</span>
                  <strong>{{ folderImportCompletion.failed_file_count }}</strong>
                </div>
              </div>
              <div v-if="folderImportTypeRows.length" class="import-dialog__chips">
                <span
                  v-for="row in folderImportTypeRows"
                  :key="row.extension"
                  class="import-dialog__chip"
                >
                  {{ row.extension }} {{ row.count }}
                </span>
              </div>
              <div
                v-if="folderImportCompletion.can_ask_about_import && folderImportQuestionRows.length"
                class="import-dialog__questions"
              >
                <button
                  v-for="question in folderImportQuestionRows"
                  :key="question.question"
                  type="button"
                  class="import-dialog__question-btn"
                  @click="askFolderImportQuestion(question.question)"
                >
                  {{ question.question }}
                </button>
              </div>
            </template>
          </div>

          <div
            v-if="folderImportProblemCount > 0 || folderImportReviewLoading"
            class="import-dialog__issues"
          >
            <div class="import-dialog__issues-header">
              <span>Skipped and failed files</span>
              <strong>
                {{ folderImportProblemCount }}
                {{ folderImportProblemCount === 1 ? 'file' : 'files' }}
              </strong>
            </div>
            <div class="import-dialog__issues-grid">
              <div class="import-dialog__issues-stat">
                <span>Skipped</span>
                <strong>{{ folderImport?.skipped_file_count ?? 0 }}</strong>
              </div>
              <div class="import-dialog__issues-stat">
                <span>Failed</span>
                <strong>{{ folderImport?.failed_file_count ?? 0 }}</strong>
              </div>
            </div>
            <div v-if="folderImportReviewReasonRows.length" class="import-dialog__chips">
              <span
                v-for="row in folderImportReviewReasonRows"
                :key="row.reason"
                class="import-dialog__chip import-dialog__chip--warn"
              >
                {{ humanizeReason(row.reason) }} {{ row.count }}
              </span>
            </div>
            <div v-if="folderImportReviewLoading" class="import-dialog__progress">
              Preparing skipped file review...
            </div>
            <ul v-else-if="folderImportReviewRows.length" class="import-dialog__issue-list">
              <li
                v-for="file in folderImportReviewRows"
                :key="`${file.status}-${file.file_id}`"
                class="import-dialog__issue-file"
                :class="`import-dialog__issue-file--${file.status}`"
              >
                <div class="import-dialog__issue-main">
                  <span class="import-dialog__file-name" :title="file.relpath">
                    {{ file.relpath }}
                  </span>
                  <span class="import-dialog__issue-status">
                    {{ humanizeReason(file.status) }}
                  </span>
                </div>
                <div class="import-dialog__issue-meta">
                  {{ formatBytes(file.size) }}
                  <span v-if="file.reason">{{ humanizeReason(file.reason) }}</span>
                </div>
                <div class="import-dialog__issue-hint">
                  {{ folderIssueActionHint(file) }}
                </div>
              </li>
            </ul>
            <p
              v-if="!folderImportReviewLoading && folderImportReviewTruncated"
              class="import-dialog__sublabel"
            >
              Showing the first {{ folderImportReviewRows.length }} files from a larger review.
            </p>
          </div>

          <div v-if="folderRescan" class="import-dialog__rescan">
            <div class="import-dialog__rescan-header">
              <span>Since last import</span>
              <strong>
                {{ folderRescan.importable_file_count }}
                {{ folderRescan.importable_file_count === 1 ? 'file' : 'files' }}
              </strong>
            </div>
            <div class="import-dialog__rescan-grid">
              <div
                v-for="row in folderRescanStatusRows"
                :key="row.label"
                class="import-dialog__rescan-stat"
              >
                <span>{{ row.label }}</span>
                <strong>{{ row.count }}</strong>
              </div>
            </div>
            <p v-if="folderRescan.missing_file_count" class="import-dialog__sublabel">
              Missing files are reported only; they are not removed.
            </p>
            <ul v-if="folderRescan.files.length" class="import-dialog__scan-files">
              <li
                v-for="file in folderRescan.files"
                :key="`${file.status}-${file.id}`"
                class="import-dialog__scan-file"
                :class="`import-dialog__rescan-file--${file.status}`"
              >
                <span class="import-dialog__file-name" :title="file.relpath">
                  {{ file.relpath }}
                </span>
                <span class="import-dialog__file-meta">
                  {{ formatBytes(file.status === 'missing' ? (file.previous_size ?? 0) : file.size) }}
                  <span v-if="file.reason">{{ humanizeReason(file.reason) }}</span>
                  <span v-else>{{ humanizeReason(file.status) }}</span>
                </span>
              </li>
            </ul>
            <p v-if="folderRescan.file_list_truncated" class="import-dialog__sublabel">
              Showing the first {{ folderRescan.files.length }} files from a larger rescan.
            </p>
            <div v-if="folderRescan.importable_file_count > 0" class="import-dialog__batch-actions">
              <button
                type="button"
                class="import-dialog__change-btn"
                :disabled="folderRescanImportStarting"
                @click="startFolderRescanImport"
              >
                <Icon name="ph:upload-simple-bold" class="icon--sm" />
                {{ folderRescanImportStarting ? 'Importing...' : 'Import changes' }}
              </button>
            </div>
          </div>

          <div class="import-dialog__scan-section">
            <div class="import-dialog__scan-heading">Destination</div>
            <div class="import-dialog__source-path">{{ folderScan.proposed_destination_root }}</div>
          </div>

          <div v-if="extensionRows.length" class="import-dialog__scan-section">
            <div class="import-dialog__scan-heading">File types</div>
            <div class="import-dialog__chips">
              <button
                v-for="row in extensionRows"
                :key="row.extension"
                type="button"
                class="import-dialog__chip-btn"
                :class="{ 'import-dialog__chip-btn--excluded': isExcludedExtension(row.extension) }"
                :disabled="folderReviewLocked"
                @click="toggleExtensionExclusion(row.extension)"
              >
                {{ row.extension }} {{ row.count }}
              </button>
            </div>
          </div>

          <div v-if="folderRows.length" class="import-dialog__scan-section">
            <div class="import-dialog__scan-heading">Folders</div>
            <div class="import-dialog__chips">
              <button
                v-for="row in folderRows"
                :key="row.relpath"
                type="button"
                class="import-dialog__chip-btn"
                :class="{ 'import-dialog__chip-btn--excluded': isExcludedFolder(row.relpath) }"
                :disabled="folderReviewLocked"
                @click="toggleFolderExclusion(row.relpath)"
              >
                {{ row.relpath }} {{ row.file_count }}
              </button>
            </div>
          </div>

          <div v-if="skipRows.length" class="import-dialog__scan-section">
            <div class="import-dialog__scan-heading">Skipped</div>
            <div class="import-dialog__chips">
              <span
                v-for="row in skipRows"
                :key="row.reason"
                class="import-dialog__chip import-dialog__chip--warn"
              >
                {{ humanizeReason(row.reason) }} {{ row.count }}
              </span>
            </div>
          </div>

          <ul class="import-dialog__scan-files">
            <li
              v-for="file in folderScan.files"
              :key="file.id"
              class="import-dialog__scan-file"
              :class="`import-dialog__scan-file--${file.status}`"
            >
              <label
                v-if="file.status === 'supported'"
                class="import-dialog__file-toggle"
                :title="isExcludedFile(file.id) ? 'Excluded' : 'Included'"
              >
                <input
                  type="checkbox"
                  :checked="!isExcludedFile(file.id)"
                  :disabled="folderReviewLocked"
                  @change="toggleFileExclusion(file.id)"
                />
              </label>
              <span class="import-dialog__file-name" :title="file.relpath">{{ file.relpath }}</span>
              <span class="import-dialog__file-meta">
                {{ formatBytes(file.size) }}
                <span v-if="file.reason">{{ humanizeReason(file.reason) }}</span>
                <span v-else>{{ file.status }}</span>
              </span>
            </li>
          </ul>

          <p v-if="folderScan.file_list_truncated" class="import-dialog__sublabel">
            Showing the first {{ folderScan.files.length }} files from a larger scan.
          </p>
        </div>
      </div>

      <div
        v-else
        class="import-dialog__dropzone"
        @dragover.prevent
        @drop.prevent="handleDrop"
      >
        <p v-if="mode === 'generic'">Drag &amp; drop one or more files here, or</p>
        <p v-else>Drag &amp; drop a file here or</p>
        <input
          ref="fileInput"
          type="file"
          :accept="acceptAttr"
          :multiple="mode === 'generic'"
          class="import-dialog__file-input"
          @change="handleFileSelect"
        />
        <button class="import-dialog__browse-btn" @click="($refs.fileInput as HTMLInputElement).click()">
          Browse
        </button>
      </div>

      <div v-if="selectedFiles.length === 1 && selectedFiles[0]" class="import-dialog__selected">
        <p>{{ selectedFiles[0].name }} ({{ Math.round(selectedFiles[0].size / 1024) }}KB)</p>
      </div>
      <ul v-else-if="selectedFiles.length > 1" class="import-dialog__file-list">
        <li
          v-for="(f, idx) in selectedFiles"
          :key="`${f.name}-${idx}`"
          class="import-dialog__file-row"
          :class="fileStatuses[idx] ? `import-dialog__file-row--${fileStatuses[idx].state}` : ''"
        >
          <span class="import-dialog__file-name" :title="f.name">{{ f.name }}</span>
          <span class="import-dialog__file-meta">
            {{ Math.round(f.size / 1024) }}KB
            <span v-if="fileStatuses[idx]?.state === 'pending'" class="import-dialog__file-status">queued</span>
            <span v-else-if="fileStatuses[idx]?.state === 'uploading'" class="import-dialog__file-status">…</span>
            <span
              v-else-if="fileStatuses[idx]?.state === 'ok'"
              class="import-dialog__file-status import-dialog__file-status--ok"
            >
              <Icon name="ph:check-bold" class="icon--sm" />
            </span>
            <span
              v-else-if="fileStatuses[idx]?.state === 'error'"
              class="import-dialog__file-status import-dialog__file-status--error"
              :title="fileStatuses[idx]?.error"
            >
              <Icon name="ph:x-bold" class="icon--sm" />
            </span>
            <button
              v-if="!uploading"
              type="button"
              class="import-dialog__file-remove"
              @click="removeFile(idx)"
              aria-label="Remove file"
            >×</button>
          </span>
        </li>
      </ul>

      <!-- Generic options -->
      <div v-if="mode === 'generic'" class="import-dialog__options">
        <label class="import-dialog__label">Target folder</label>
        <select v-model="targetFolder" class="import-dialog__select">
          <option value="knowledge">knowledge</option>
          <option value="inbox">inbox</option>
          <option value="projects">projects</option>
          <option value="areas">areas</option>
        </select>
      </div>

      <!-- Jira options -->
      <div v-else-if="mode === 'jira'" class="import-dialog__options">
        <label class="import-dialog__label">Project filter (optional)</label>
        <input
          v-model="projectFilter"
          type="text"
          class="import-dialog__input"
          placeholder="e.g. PROJ,OPS  (comma-separated; empty = import all)"
        />
        <p class="import-dialog__sublabel">
          Only issues whose project key matches will be imported. Leave empty for full export.
        </p>
      </div>

      <div v-if="uploading" class="import-dialog__progress">
        <template v-if="selectedFiles.length > 1">
          Importing {{ uploadProgress.done }} of {{ uploadProgress.total }}…
        </template>
        <template v-else>
          Importing{{ mode === 'jira' ? ' (parsing + indexing + embedding)' : '' }}...
        </template>
      </div>

      <div v-if="error" class="import-dialog__error">{{ error }}</div>
      <div v-if="success" class="import-dialog__success">{{ success }}</div>

      <!-- Recent Jira imports -->
      <div v-if="mode === 'jira'" class="import-dialog__recent">
        <div class="import-dialog__recent-header">
          <h3 class="import-dialog__recent-title">Recent Jira imports</h3>
          <button
            type="button"
            class="import-dialog__refresh-btn"
            :disabled="recentLoading"
            @click="loadRecentImports"
          >
            {{ recentLoading ? '...' : 'Refresh' }}
          </button>
        </div>
        <div v-if="recentError" class="import-dialog__error">{{ recentError }}</div>
        <ul v-if="recentImports.length" class="import-dialog__recent-list">
          <li
            v-for="row in recentImports"
            :key="row.id"
            class="import-dialog__recent-row"
            :class="`import-dialog__recent-row--${row.status}`"
          >
            <div class="import-dialog__recent-line">
              <span class="import-dialog__recent-name" :title="row.filename">
                {{ row.filename }}
              </span>
              <span class="import-dialog__recent-status">{{ row.status }}</span>
            </div>
            <div class="import-dialog__recent-meta">
              {{ formatDate(row.started_at) }}
              · {{ row.issue_count }} issues
              ({{ row.inserted }} new, {{ row.updated }} upd)
              <span v-if="row.project_keys?.length">
                · {{ row.project_keys.join(', ') }}
              </span>
            </div>
            <div v-if="row.error" class="import-dialog__recent-error">
              {{ row.error }}
            </div>
          </li>
        </ul>
        <p v-else-if="!recentLoading && !recentError" class="import-dialog__recent-empty">
          No imports yet.
        </p>
      </div>

      <div class="import-dialog__actions">
        <button class="import-dialog__cancel-btn" @click="$emit('close')">Cancel</button>
        <button
          class="import-dialog__import-btn"
          :disabled="primaryDisabled"
          @click="handlePrimaryAction"
        >
          {{ primaryLabel }}
        </button>
      </div>

      <ConfirmDialog
        :visible="folderRemoveConfirmOpen"
        :loading="folderImportRemoving"
        title="Remove import?"
        :message="folderRemoveConfirmMessage"
        confirm-label="Remove"
        @confirm="confirmRemoveFolderImport"
        @cancel="folderRemoveConfirmOpen = false"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useIngestStatus } from '~/composables/useIngestStatus'
import { useSourceImport } from '~/composables/useSourceImport'
import type {
  SourceGrantResponse,
  SourceImportBatchSummary,
  SourceImportCompletionSummary,
  SourceImportFileReviewItem,
  SourceImportFileReviewReport,
  SourceImportSuggestedQuestion,
  SourceImportRescanReport,
  SourceScanReport,
  SourceSelectionSummary,
} from '~/composables/useSourceImport'

const ingest = useIngestStatus()
const sourceImport = useSourceImport()

defineProps<{
  visible: boolean
}>()

const emit = defineEmits<{
  close: []
  imported: [result: Record<string, unknown>]
}>()

type Mode = 'generic' | 'folder' | 'jira'

interface JiraImportRow {
  id: number
  filename: string
  format: string
  project_keys: string[]
  issue_count: number
  inserted: number
  updated: number
  skipped: number
  bytes_processed: number
  duration_ms: number
  status: string
  error?: string | null
  started_at: string
  finished_at?: string | null
}

const mode = ref<Mode>('generic')
const selectedFiles = ref<File[]>([])
const targetFolder = ref('knowledge')
const projectFilter = ref('')
const uploading = ref(false)
const error = ref('')
const success = ref('')
const folderPicking = ref(false)
const folderSamplePicking = ref(false)
const folderScanning = ref(false)
const folderGrant = ref<SourceGrantResponse | null>(null)
const folderScan = ref<SourceScanReport | null>(null)
const folderSelection = ref<SourceSelectionSummary | null>(null)
const folderSelectionLoading = ref(false)
const folderImport = ref<SourceImportBatchSummary | null>(null)
const folderImportStarting = ref(false)
const folderImportCancelling = ref(false)
const folderImportRemoving = ref(false)
const folderRemoveConfirmOpen = ref(false)
const folderImportCompletion = ref<SourceImportCompletionSummary | null>(null)
const folderImportCompletionLoading = ref(false)
const folderImportReview = ref<SourceImportFileReviewReport | null>(null)
const folderImportReviewLoading = ref(false)
const folderRescan = ref<SourceImportRescanReport | null>(null)
const folderRescanning = ref(false)
const folderRescanImportStarting = ref(false)
const includeHiddenInFolderScan = ref(false)
const excludedFileIds = ref<string[]>([])
const excludedExtensions = ref<string[]>([])
const excludedFolders = ref<string[]>([])
let folderImportPollTimer: ReturnType<typeof setTimeout> | null = null

type FileState = 'pending' | 'uploading' | 'ok' | 'error'
interface FileStatus { state: FileState; error?: string }
const fileStatuses = ref<FileStatus[]>([])
const uploadProgress = ref<{ done: number; total: number }>({ done: 0, total: 0 })

const recentImports = ref<JiraImportRow[]>([])
const recentLoading = ref(false)
const recentError = ref('')

const acceptAttr = computed(() =>
  mode.value === 'jira'
    ? '.xml,.csv'
    : '.md,.txt,.pdf,.csv,.xml,.json,.docx,.xlsx,.pptx,.html,.htm,.rtf,.eml,.zip'
)

const extensionRows = computed(() => {
  if (!folderScan.value) return []
  return Object.entries(folderScan.value.counts_by_extension)
    .map(([extension, count]) => ({ extension, count }))
    .sort((a, b) => b.count - a.count || a.extension.localeCompare(b.extension))
})

const skipRows = computed(() => {
  if (!folderScan.value) return []
  return Object.entries(folderScan.value.skipped_by_reason)
    .map(([reason, count]) => ({ reason, count }))
    .sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason))
})

const folderRows = computed(() => {
  if (!folderScan.value) return []
  return folderScan.value.folder_summary
    .filter(row => row.relpath !== '.')
    .slice(0, 12)
})

const folderImportActive = computed(() =>
  folderImportStarting.value ||
  folderImportCancelling.value ||
  folderImportRemoving.value ||
  folderRescanImportStarting.value ||
  folderImport.value?.state === 'queued' ||
  folderImport.value?.state === 'importing' ||
  folderImport.value?.state === 'cancelling' ||
  folderImport.value?.state === 'removing'
)

const folderImportTerminal = computed(() =>
  !!folderImport.value &&
  !['queued', 'importing', 'cancelling', 'removing'].includes(folderImport.value.state)
)

const folderImportCanCancel = computed(() =>
  !!folderImport.value &&
  ['queued', 'importing'].includes(folderImport.value.state)
)

const folderImportCanRemove = computed(() =>
  !!folderImport.value &&
  ['completed', 'failed', 'cancelled', 'interrupted'].includes(folderImport.value.state) &&
  !folderImportActive.value
)

const folderImportCanRescan = computed(() =>
  !!folderImport.value &&
  ['completed', 'failed', 'cancelled', 'interrupted'].includes(folderImport.value.state) &&
  !folderRescanning.value &&
  !folderImportActive.value &&
  !folderImportRemoving.value
)

const folderImportStatusLabel = computed(() => {
  if (folderImportRemoving.value || folderImport.value?.state === 'removing') {
    return 'Removing import'
  }
  if (folderImportCancelling.value || folderImport.value?.state === 'cancelling') {
    return 'Cancelling import'
  }
  if (folderImportActive.value) return 'Creating memory'
  return humanizeReason(folderImport.value?.state ?? '')
})

const folderRescanStatusRows = computed(() => {
  if (!folderRescan.value) return []
  return [
    { label: 'New', count: folderRescan.value.new_file_count },
    { label: 'Changed', count: folderRescan.value.changed_file_count },
    { label: 'Unchanged', count: folderRescan.value.unchanged_file_count },
    { label: 'Missing', count: folderRescan.value.missing_file_count },
  ]
})

const folderImportProblemCount = computed(() =>
  (folderImport.value?.skipped_file_count ?? 0) +
  (folderImport.value?.failed_file_count ?? 0)
)

const folderImportReviewReasonRows = computed(() => {
  if (!folderImportReview.value) return []
  return Object.entries(folderImportReview.value.reason_counts)
    .map(([reason, count]) => ({ reason, count }))
    .sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason))
    .slice(0, 6)
})

const folderImportReviewRows = computed<SourceImportFileReviewItem[]>(() => {
  if (folderImportReview.value?.files.length) return folderImportReview.value.files
  if (!folderImport.value?.files.length) return []
  return folderImport.value.files
    .filter(file => file.status === 'skipped' || file.status === 'failed')
    .slice(0, 8)
    .map(file => ({
      ...file,
      status: file.status as 'skipped' | 'failed',
      can_retry: folderIssueCanRetry(file),
      can_fix_locally: folderIssueCanFixLocally(file),
    }))
})

const folderImportReviewTruncated = computed(() => {
  if (folderImportReview.value) return folderImportReview.value.file_list_truncated
  return folderImportReviewRows.value.length < folderImportProblemCount.value
})

const folderImportTypeRows = computed(() => {
  if (!folderImportCompletion.value) return []
  return Object.entries(folderImportCompletion.value.imported_extension_counts)
    .map(([extension, count]) => ({ extension, count }))
    .slice(0, 5)
})

const folderImportQuestionRows = computed<SourceImportSuggestedQuestion[]>(() =>
  folderImportCompletion.value?.suggested_questions ?? []
)

const folderRemoveConfirmMessage = computed(() => {
  const count = folderImport.value?.created_note_count ?? 0
  const noun = count === 1 ? 'note' : 'notes'
  return `${count} created ${noun} will be moved out of memory. Unrelated notes will stay.`
})

const folderImportProgressPercent = computed(() => {
  if (!folderImport.value || folderImport.value.total_file_count <= 0) return '0%'
  const done =
    folderImport.value.imported_file_count +
    folderImport.value.skipped_file_count +
    folderImport.value.failed_file_count
  return `${Math.min(100, Math.round((done / folderImport.value.total_file_count) * 100))}%`
})

const folderReviewLocked = computed(() =>
  folderSelectionLoading.value ||
  folderRescanning.value ||
  folderImportActive.value ||
  folderImportTerminal.value
)

const primaryDisabled = computed(() => {
  if (mode.value === 'folder') {
    if (
      !folderGrant.value ||
      folderScanning.value ||
      folderPicking.value ||
      folderSamplePicking.value ||
      folderRescanning.value ||
      folderImportActive.value
    ) {
      return true
    }
    if (folderScan.value) {
      return (
        !folderSelection.value ||
        folderSelectionLoading.value ||
        folderSelection.value.approved_file_count === 0 ||
        folderImportTerminal.value
      )
    }
    return false
  }
  return selectedFiles.value.length === 0 || uploading.value
})

const primaryLabel = computed(() => {
  if (mode.value === 'folder') {
    if (folderImportRemoving.value || folderImport.value?.state === 'removing') {
      return 'Removing...'
    }
    if (folderRescanning.value) return 'Scanning...'
    if (folderRescanImportStarting.value) return 'Importing changes...'
    if (folderImportCancelling.value || folderImport.value?.state === 'cancelling') {
      return 'Cancelling...'
    }
    if (folderImportActive.value) return 'Importing...'
    if (folderImport.value?.state === 'removed') return 'Removed'
    if (folderImportTerminal.value) return 'Imported'
    if (folderScan.value) return 'Import selected'
    if (folderScanning.value) return 'Scanning...'
    return 'Scan folder'
  }
  if (selectedFiles.value.length > 1) return `Import ${selectedFiles.value.length} files`
  return 'Import'
})

function resetFolderReview() {
  clearFolderImportPoll()
  folderScan.value = null
  folderSelection.value = null
  folderSelectionLoading.value = false
  folderImport.value = null
  folderImportStarting.value = false
  folderImportCancelling.value = false
  folderImportRemoving.value = false
  folderRemoveConfirmOpen.value = false
  folderImportCompletion.value = null
  folderImportCompletionLoading.value = false
  folderImportReview.value = null
  folderImportReviewLoading.value = false
  folderRescan.value = null
  folderRescanning.value = false
  folderRescanImportStarting.value = false
  excludedFileIds.value = []
  excludedExtensions.value = []
  excludedFolders.value = []
}

function setMode(next: Mode) {
  if (mode.value === next) return
  mode.value = next
  // Reset files when switching modes (extension constraints differ).
  selectedFiles.value = []
  fileStatuses.value = []
  error.value = ''
  success.value = ''
  folderGrant.value = null
  includeHiddenInFolderScan.value = false
  resetFolderReview()
  if (next === 'jira' && recentImports.value.length === 0) {
    loadRecentImports()
  }
}

function acceptFiles(list: FileList | null | undefined) {
  if (!list || list.length === 0) return
  // In jira mode keep single-file behaviour; in generic mode accept many.
  const incoming = Array.from(list)
  if (mode.value === 'jira') {
    const f = incoming[0]
    if (!f || !validateFile(f)) return
    selectedFiles.value = [f]
    fileStatuses.value = [{ state: 'pending' }]
  } else {
    const valid: File[] = []
    for (const f of incoming) {
      if (validateFile(f)) valid.push(f)
      else return
    }
    // Append rather than replace so user can drop in batches.
    selectedFiles.value = [...selectedFiles.value, ...valid]
    fileStatuses.value = selectedFiles.value.map(() => ({ state: 'pending' }))
  }
  error.value = ''
  success.value = ''
}

function handleFileSelect(event: Event) {
  const input = event.target as HTMLInputElement
  acceptFiles(input.files)
  // Allow re-selecting the same file after a remove.
  input.value = ''
}

function handleDrop(event: DragEvent) {
  acceptFiles(event.dataTransfer?.files)
}

function removeFile(idx: number) {
  selectedFiles.value = selectedFiles.value.filter((_, i) => i !== idx)
  fileStatuses.value = fileStatuses.value.filter((_, i) => i !== idx)
}

function validateFile(file: File): boolean {
  if (mode.value === 'jira') {
    const name = file.name.toLowerCase()
    if (!name.endsWith('.xml') && !name.endsWith('.csv')) {
      error.value = 'Jira mode accepts only .xml or .csv exports.'
      return false
    }
  }
  return true
}

async function handleImport() {
  if (selectedFiles.value.length === 0) return
  uploading.value = true
  error.value = ''
  success.value = ''

  try {
    if (mode.value === 'jira') {
      // Jira import is single-file by design (one export = one batch).
      await importJira(selectedFiles.value[0]!)
    } else {
      await importGenericBatch()
    }
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : 'Import failed'
  } finally {
    uploading.value = false
  }
}

async function handlePrimaryAction() {
  if (mode.value === 'folder') {
    if (folderScan.value) {
      await startFolderImport()
      return
    }
    await scanFolderSource()
    return
  }
  await handleImport()
}

async function chooseFolderSource() {
  folderPicking.value = true
  error.value = ''
  success.value = ''
  resetFolderReview()
  try {
    folderGrant.value = await sourceImport.pickFolderSource()
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : 'Folder selection failed'
  } finally {
    folderPicking.value = false
  }
}

async function chooseSampleDataset() {
  folderSamplePicking.value = true
  error.value = ''
  success.value = ''
  resetFolderReview()
  try {
    folderGrant.value = await sourceImport.pickSampleDataset()
    success.value = 'Sample folder selected. Scan it when ready.'
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : 'Sample folder selection failed'
  } finally {
    folderSamplePicking.value = false
  }
}

async function scanFolderSource() {
  if (!folderGrant.value) return
  folderScanning.value = true
  error.value = ''
  success.value = ''
  resetFolderReview()
  try {
    folderScan.value = await sourceImport.scanSource(folderGrant.value.source_token, {
      includeHidden: includeHiddenInFolderScan.value,
    })
    await refreshFolderSelection()
    success.value =
      `Scan ready: ${folderScan.value.supported_file_count} supported files, ` +
      `${folderScan.value.skipped_file_count} skipped.`
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : 'Folder scan failed'
  } finally {
    folderScanning.value = false
  }
}

async function refreshFolderSelection() {
  if (!folderScan.value) return
  folderSelectionLoading.value = true
  error.value = ''
  folderImport.value = null
  folderImportCompletion.value = null
  folderImportReview.value = null
  folderRescan.value = null
  clearFolderImportPoll()
  try {
    folderSelection.value = await sourceImport.createSelection(folderScan.value.scan_id, {
      excludedFileIds: excludedFileIds.value,
      excludedExtensions: excludedExtensions.value,
      excludedFolders: excludedFolders.value,
    })
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : 'Failed to update folder review'
  } finally {
    folderSelectionLoading.value = false
  }
}

async function startFolderImport() {
  if (!folderScan.value || !folderSelection.value) return
  folderImportStarting.value = true
  error.value = ''
  success.value = ''
  folderImportCompletion.value = null
  folderImportReview.value = null
  folderRescan.value = null
  try {
    folderImport.value = await sourceImport.startImport(
      folderScan.value.scan_id,
      folderSelection.value.selection_id,
    )
    success.value = `Import started: ${folderImport.value.total_file_count} files approved.`
    scheduleFolderImportPoll()
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : 'Folder import failed to start'
  } finally {
    folderImportStarting.value = false
  }
}

async function cancelFolderImport() {
  if (!folderImport.value || !folderImportCanCancel.value || folderImportCancelling.value) return
  const batch = folderImport.value

  folderImportCancelling.value = true
  error.value = ''
  success.value = ''
  try {
    folderImport.value = await sourceImport.cancelImport(batch.batch_id)
    success.value = 'Cancelling import after the current file finishes.'
    scheduleFolderImportPoll()
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : 'Failed to cancel import'
  } finally {
    folderImportCancelling.value = false
  }
}

function requestRemoveFolderImport() {
  if (!folderImport.value || !folderImportCanRemove.value || folderImportRemoving.value) return
  folderRemoveConfirmOpen.value = true
}

async function confirmRemoveFolderImport() {
  if (!folderImport.value || !folderImportCanRemove.value || folderImportRemoving.value) return
  const batch = folderImport.value

  folderImportRemoving.value = true
  error.value = ''
  success.value = ''
  folderImportCompletion.value = null
  folderRescan.value = null
  clearFolderImportPoll()
  try {
    folderImport.value = await sourceImport.removeImport(batch.batch_id, batch.batch_id)
    success.value =
      `Removed import: ${folderImport.value.created_note_count} created notes moved out of memory.`
    folderImportReview.value = null
    emit('imported', folderImport.value as unknown as Record<string, unknown>)
    folderRemoveConfirmOpen.value = false
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : 'Failed to remove import'
    folderRemoveConfirmOpen.value = false
  } finally {
    folderImportRemoving.value = false
  }
}

async function rescanFolderImport() {
  if (!folderImport.value || !folderImportCanRescan.value || folderRescanning.value) return
  const batch = folderImport.value

  folderRescanning.value = true
  error.value = ''
  success.value = ''
  folderImportCompletion.value = null
  folderImportReview.value = null
  folderRescan.value = null
  try {
    folderRescan.value = await sourceImport.rescanImport(batch.batch_id)
    if (folderRescan.value.importable_file_count > 0) {
      success.value =
        `Scan again ready: ${folderRescan.value.new_file_count} new and ` +
        `${folderRescan.value.changed_file_count} changed files.`
    } else {
      success.value = 'Scan again found no new or changed files.'
      void refreshFolderImportCompletion()
      void refreshFolderImportReview()
    }
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : 'Failed to scan source again'
  } finally {
    folderRescanning.value = false
  }
}

async function startFolderRescanImport() {
  if (!folderRescan.value?.scan_id || folderRescan.value.importable_file_count === 0) return
  if (folderRescanImportStarting.value || folderImportActive.value) return

  folderRescanImportStarting.value = true
  error.value = ''
  success.value = ''
  folderImportCompletion.value = null
  folderImportReview.value = null
  try {
    const selection = await sourceImport.createSelection(folderRescan.value.scan_id, {})
    folderImport.value = await sourceImport.startImport(
      folderRescan.value.scan_id,
      selection.selection_id,
    )
    folderSelection.value = selection
    success.value =
      `Importing ${folderImport.value.total_file_count} new or changed files.`
    folderRescan.value = null
    scheduleFolderImportPoll()
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : 'Failed to import source changes'
  } finally {
    folderRescanImportStarting.value = false
  }
}

async function refreshFolderImportCompletion() {
  const batch = folderImport.value
  if (!batch || batch.imported_file_count === 0 || batch.state === 'removed') {
    folderImportCompletion.value = null
    return
  }
  folderImportCompletionLoading.value = true
  try {
    folderImportCompletion.value = await sourceImport.getImportCompletion(batch.batch_id)
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : 'Failed to load import summary'
  } finally {
    folderImportCompletionLoading.value = false
  }
}

async function refreshFolderImportReview() {
  const batch = folderImport.value
  if (!batch || folderImportProblemCount.value === 0) {
    folderImportReview.value = null
    return
  }
  folderImportReviewLoading.value = true
  try {
    folderImportReview.value = await sourceImport.getImportReview(batch.batch_id, 100)
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : 'Failed to load skipped file review'
  } finally {
    folderImportReviewLoading.value = false
  }
}

function clearFolderImportPoll() {
  if (folderImportPollTimer) {
    clearTimeout(folderImportPollTimer)
    folderImportPollTimer = null
  }
}

function scheduleFolderImportPoll() {
  clearFolderImportPoll()
  const batchId = folderImport.value?.batch_id
  if (!batchId || folderImportTerminal.value) {
    if (folderImport.value?.state === 'completed') {
      success.value =
        `Imported ${folderImport.value.imported_file_count} files ` +
        `and created ${folderImport.value.created_note_count} notes.`
      emit('imported', folderImport.value as unknown as Record<string, unknown>)
      void refreshFolderImportCompletion()
      void refreshFolderImportReview()
    } else if (folderImport.value?.state === 'cancelled') {
      success.value =
        `Import cancelled: ${folderImport.value.imported_file_count} files imported ` +
        `and ${folderImport.value.skipped_file_count} skipped.`
      emit('imported', folderImport.value as unknown as Record<string, unknown>)
      void refreshFolderImportCompletion()
      void refreshFolderImportReview()
    } else if (folderImport.value?.state === 'interrupted') {
      error.value = 'Import was interrupted. Created notes remain until you remove the import.'
      void refreshFolderImportCompletion()
      void refreshFolderImportReview()
    } else if (folderImport.value?.state === 'failed') {
      void refreshFolderImportCompletion()
      void refreshFolderImportReview()
    }
    return
  }
  folderImportPollTimer = setTimeout(async () => {
    try {
      folderImport.value = await sourceImport.getImport(batchId)
      scheduleFolderImportPoll()
    } catch (err: unknown) {
      error.value = err instanceof Error ? err.message : 'Failed to refresh import progress'
    }
  }, 900)
}

function askFolderImportQuestion(question: string) {
  const batch = folderImport.value
  if (!batch || !question.trim()) return
  void navigateTo({
    path: '/main',
    query: {
      import_batch_id: batch.batch_id,
      q: question,
    },
  })
}

function isExcludedFile(id: string): boolean {
  return excludedFileIds.value.includes(id)
}

function isExcludedExtension(extension: string): boolean {
  return excludedExtensions.value.includes(extension)
}

function isExcludedFolder(relpath: string): boolean {
  return excludedFolders.value.includes(relpath)
}

async function toggleFileExclusion(id: string) {
  if (folderReviewLocked.value) return
  excludedFileIds.value = toggleListValue(excludedFileIds.value, id)
  await refreshFolderSelection()
}

async function toggleExtensionExclusion(extension: string) {
  if (folderReviewLocked.value) return
  excludedExtensions.value = toggleListValue(excludedExtensions.value, extension)
  await refreshFolderSelection()
}

async function toggleFolderExclusion(relpath: string) {
  if (folderReviewLocked.value) return
  excludedFolders.value = toggleListValue(excludedFolders.value, relpath)
  await refreshFolderSelection()
}

function toggleListValue(values: string[], value: string): string[] {
  return values.includes(value)
    ? values.filter(item => item !== value)
    : [...values, value]
}

async function importGenericBatch() {
  const files = selectedFiles.value
  uploadProgress.value = { done: 0, total: files.length }
  let okCount = 0
  let failCount = 0
  const lastErrors: string[] = []

  // Bounded parallel upload. The backend is now non-blocking (pdfplumber
  // runs in a threadpool, SQLite has a 30 s busy_timeout) so we can run a
  // few uploads at once and the user gets per-file XHR progress in the
  // StatusBar.
  const CONCURRENCY = 3
  let nextIndex = 0
  let doneSoFar = 0

  const runOne = async (i: number) => {
    fileStatuses.value[i] = { state: 'uploading' }
    const file = files[i]!
    try {
      const result = await importGeneric(file)
      fileStatuses.value[i] = { state: 'ok' }
      okCount += 1
      emit('imported', result)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Import failed'
      fileStatuses.value[i] = { state: 'error', error: msg }
      failCount += 1
      if (lastErrors.length < 3) lastErrors.push(`${file.name}: ${msg}`)
    } finally {
      doneSoFar += 1
      uploadProgress.value = { done: doneSoFar, total: files.length }
    }
  }

  const worker = async () => {
    while (true) {
      const i = nextIndex++
      if (i >= files.length) return
      await runOne(i)
    }
  }

  const workerCount = Math.min(CONCURRENCY, files.length)
  await Promise.all(Array.from({ length: workerCount }, () => worker()))

  if (failCount === 0) {
    success.value = `Imported ${okCount} ${okCount === 1 ? 'file' : 'files'}.`
  } else if (okCount === 0) {
    error.value = `All ${failCount} imports failed. ${lastErrors.join('; ')}`
  } else {
    success.value = `Imported ${okCount} of ${files.length}.`
    error.value = `${failCount} failed: ${lastErrors.join('; ')}`
  }
}

async function importGeneric(file: File): Promise<Record<string, unknown>> {
  // Use the shared uploadFile() so the StatusBar pill shows real
  // bytes-uploaded progress for each file.
  const result = await ingest.uploadFile('/api/memory/ingest', file, {
    folder: targetFolder.value,
  })
  return (result || {}) as Record<string, unknown>
}

async function importJira(file: File) {
  const formData = new FormData()
  formData.append('file', file)
  const filter = projectFilter.value.trim()
  if (filter) formData.append('project_filter', filter)

  const result = await $fetch<{
    status: string
    filename: string
    format: string
    stats: {
      issue_count: number
      inserted: number
      updated: number
      skipped: number
      bytes_processed: number
      project_keys: string[]
    }
  }>(apiUrl('/api/jira/import'), {
    method: 'POST',
    body: formData,
  })

  const s = result.stats
  const projects = s.project_keys.length ? ` [${s.project_keys.join(', ')}]` : ''
  success.value =
    `Jira ${result.format.toUpperCase()}: ${s.issue_count} issues ` +
    `(${s.inserted} new, ${s.updated} updated, ${s.skipped} skipped)${projects}`
  emit('imported', result as unknown as Record<string, unknown>)
  // Refresh history so the user sees their new batch immediately.
  loadRecentImports()
}

async function loadRecentImports() {
  recentLoading.value = true
  recentError.value = ''
  try {
    const rows = await $fetch<JiraImportRow[]>(apiUrl('/api/jira/imports?limit=10'))
    recentImports.value = rows
  } catch (err: unknown) {
    recentError.value =
      err instanceof Error ? err.message : 'Failed to load Jira import history'
  } finally {
    recentLoading.value = false
  }
}

function formatDate(iso: string): string {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString()
  } catch {
    return iso
  }
}

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

function folderIssueCanRetry(file: { status: string; reason?: string | null }): boolean {
  const reason = (file.reason ?? '').toLowerCase()
  if (reason.includes('duplicate_content')) return false
  if (file.status === 'failed') return true
  return [
    'app_closed_during_import',
    'cancelled_by_user',
    'no longer available',
    'outside the selected folder',
    'permission',
    'unreadable',
  ].some(marker => reason.includes(marker))
}

function folderIssueCanFixLocally(file: { reason?: string | null }): boolean {
  const reason = (file.reason ?? '').toLowerCase()
  return [
    'encrypted',
    'file_too_large',
    'limit',
    'no longer available',
    'online_only',
    'outside the selected folder',
    'password',
    'permission',
    'placeholder',
    'source file',
    'unreadable',
    'unsupported',
  ].some(marker => reason.includes(marker))
}

function folderIssueActionHint(file: SourceImportFileReviewItem): string {
  const reason = (file.reason ?? '').toLowerCase()
  if (reason.includes('duplicate_content')) {
    return file.duplicate_of
      ? `Already imported from ${file.duplicate_of}.`
      : 'Already imported from another file in this batch.'
  }
  if (reason.includes('cancelled_by_user')) {
    return 'Scan again when you are ready to import it.'
  }
  if (reason.includes('app_closed_during_import')) {
    return 'Scan again to retry the unfinished file.'
  }
  if (reason.includes('password') || reason.includes('encrypted')) {
    return 'Export an unlocked copy, then import it.'
  }
  if (reason.includes('online_only') || reason.includes('placeholder')) {
    return 'Download it to this computer, then scan again.'
  }
  if (
    reason.includes('no longer available') ||
    reason.includes('outside the selected folder')
  ) {
    return 'Put the file back in the folder, then scan again.'
  }
  if (reason.includes('permission') || reason.includes('unreadable')) {
    return 'Check local file permissions, then scan again.'
  }
  if (reason.includes('unsupported')) {
    return 'Convert it to a supported document type.'
  }
  if (reason.includes('too large') || reason.includes('limit')) {
    return 'Split or reduce the file before importing.'
  }
  if (file.status === 'failed') {
    return 'Check that the file opens locally, then scan again.'
  }
  return 'This file was left out of memory for this import.'
}

function humanizeReason(reason: string): string {
  return reason
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (ch) => ch.toUpperCase())
}

// Lazy-load history when dialog becomes visible while in Jira mode.
watch(
  () => mode.value,
  (m) => {
    if (m === 'jira' && recentImports.value.length === 0) {
      loadRecentImports()
    }
  }
)

onUnmounted(() => {
  clearFolderImportPoll()
})
</script>

<style scoped>
.import-dialog__backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  z-index: 99;
}
.import-dialog__panel {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  background: #1a1a2e;
  border: 1px solid var(--color-border, #333);
  border-radius: 12px;
  padding: 2rem;
  z-index: 100;
  min-width: 440px;
  max-width: 560px;
  max-height: 90vh;
  overflow-y: auto;
}
.import-dialog__title {
  margin: 0 0 1rem;
  font-size: 1.2rem;
}
.import-dialog__modes {
  display: flex;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}
.import-dialog__mode-btn {
  flex: 1;
  padding: 0.45rem 0.75rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 6px;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font-size: 0.9rem;
}
.import-dialog__mode-btn--active {
  background: var(--color-primary, #60a5fa);
  color: #fff;
  border-color: var(--color-primary, #60a5fa);
}
.import-dialog__hint {
  font-size: 0.8rem;
  opacity: 0.7;
  margin: 0 0 1rem;
  line-height: 1.4;
}
.import-dialog__hint code {
  background: rgba(255, 255, 255, 0.08);
  padding: 0 0.25rem;
  border-radius: 3px;
}
.import-dialog__dropzone {
  border: 2px dashed var(--color-border, #333);
  border-radius: 8px;
  padding: 2rem;
  text-align: center;
}
.import-dialog__file-input {
  display: none;
}
.import-dialog__browse-btn,
.import-dialog__sample-btn {
  margin-top: 0.5rem;
  padding: 0.4rem 1.25rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 4px;
  background: transparent;
  color: inherit;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
}
.import-dialog__sample-btn {
  border-color: rgba(34, 197, 94, 0.45);
  color: #86efac;
}
.import-dialog__browse-btn:disabled,
.import-dialog__sample-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.import-dialog__selected {
  margin-top: 0.75rem;
  font-size: 0.9rem;
  opacity: 0.8;
}
.import-dialog__file-list {
  margin: 0.75rem 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  max-height: 180px;
  overflow-y: auto;
}
.import-dialog__file-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
  padding: 0.35rem 0.55rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.02);
  font-size: 0.82rem;
}
.import-dialog__file-row--ok {
  border-left: 3px solid #22c55e;
}
.import-dialog__file-row--error {
  border-left: 3px solid #ef4444;
}
.import-dialog__file-row--uploading {
  border-left: 3px solid #60a5fa;
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
.import-dialog__file-status {
  font-size: 0.78rem;
  opacity: 0.85;
}
.import-dialog__file-status--ok {
  color: #22c55e;
}
.import-dialog__file-status--error {
  color: #ef4444;
}
.import-dialog__file-remove {
  background: transparent;
  border: none;
  color: inherit;
  opacity: 0.5;
  cursor: pointer;
  font-size: 1rem;
  line-height: 1;
  padding: 0 0.2rem;
}
.import-dialog__file-remove:hover {
  opacity: 1;
  color: #ef4444;
}
.import-dialog__options {
  margin-top: 1rem;
}
.import-dialog__label {
  font-size: 0.85rem;
  display: block;
  margin-bottom: 0.25rem;
}
.import-dialog__sublabel {
  font-size: 0.75rem;
  opacity: 0.6;
  margin: 0.35rem 0 0;
}
.import-dialog__source {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.import-dialog__source-actions {
  display: flex;
  gap: 0.5rem;
  justify-content: flex-start;
  flex-wrap: wrap;
}
.import-dialog__source-card {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  align-items: flex-start;
  padding: 0.75rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
}
.import-dialog__source-name {
  font-size: 0.92rem;
  font-weight: 600;
}
.import-dialog__source-path {
  margin-top: 0.2rem;
  font-size: 0.74rem;
  opacity: 0.65;
  overflow-wrap: anywhere;
}
.import-dialog__source-badge {
  flex: 0 0 auto;
  padding: 0.18rem 0.45rem;
  border: 1px solid rgba(96, 165, 250, 0.5);
  border-radius: 999px;
  color: #93c5fd;
  font-size: 0.68rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.import-dialog__toggle {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  font-size: 0.78rem;
  opacity: 0.78;
}
.import-dialog__toggle input {
  margin: 0;
}
.import-dialog__scan {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.import-dialog__scan-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.5rem;
}
.import-dialog__scan-stat {
  padding: 0.65rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.025);
}
.import-dialog__scan-stat span {
  display: block;
  font-size: 0.68rem;
  opacity: 0.62;
}
.import-dialog__scan-stat strong {
  display: block;
  margin-top: 0.2rem;
  font-size: 1rem;
}
.import-dialog__approval {
  padding: 0.7rem;
  border: 1px solid rgba(34, 197, 94, 0.35);
  border-radius: 6px;
  background: rgba(34, 197, 94, 0.045);
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.55rem;
}
.import-dialog__approval .import-dialog__sublabel {
  grid-column: 1 / -1;
}
.import-dialog__approval-stat span {
  display: block;
  font-size: 0.68rem;
  opacity: 0.65;
}
.import-dialog__approval-stat strong {
  display: flex;
  align-items: baseline;
  gap: 0.4rem;
  margin-top: 0.2rem;
  font-size: 1rem;
}
.import-dialog__approval-stat small {
  font-size: 0.68rem;
  font-weight: 400;
  opacity: 0.65;
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
.import-dialog__scan-section {
  padding-top: 0.25rem;
}
.import-dialog__scan-heading {
  margin-bottom: 0.35rem;
  font-size: 0.78rem;
  opacity: 0.7;
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
.import-dialog__chip-btn {
  padding: 0.2rem 0.45rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.025);
  color: inherit;
  cursor: pointer;
  font: inherit;
  font-size: 0.72rem;
}
.import-dialog__chip-btn--excluded {
  border-color: rgba(239, 68, 68, 0.55);
  color: #fca5a5;
  text-decoration: line-through;
}
.import-dialog__chip-btn:disabled {
  opacity: 0.55;
  cursor: wait;
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
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0.55rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.02);
  font-size: 0.8rem;
}
.import-dialog__scan-file--supported {
  border-left: 3px solid #22c55e;
}
.import-dialog__scan-file--unsupported {
  border-left: 3px solid #f59e0b;
}
.import-dialog__scan-file--skipped {
  border-left: 3px solid #64748b;
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
.import-dialog__file-toggle {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
}
.import-dialog__file-toggle input {
  margin: 0;
}
.import-dialog__select,
.import-dialog__input {
  padding: 0.4rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 4px;
  background: transparent;
  color: inherit;
  width: 100%;
  font: inherit;
}
.import-dialog__progress {
  margin-top: 0.75rem;
  opacity: 0.7;
}
.import-dialog__error {
  margin-top: 0.75rem;
  color: #ef4444;
}
.import-dialog__success {
  margin-top: 0.75rem;
  color: #22c55e;
}
.import-dialog__recent {
  margin-top: 1.5rem;
  padding-top: 1rem;
  border-top: 1px solid var(--color-border, #333);
}
.import-dialog__recent-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.5rem;
}
.import-dialog__recent-title {
  margin: 0;
  font-size: 0.95rem;
}
.import-dialog__refresh-btn {
  padding: 0.25rem 0.6rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 4px;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font-size: 0.8rem;
}
.import-dialog__refresh-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.import-dialog__recent-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.import-dialog__recent-row {
  padding: 0.5rem 0.7rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.02);
}
.import-dialog__recent-row--ok {
  border-left: 3px solid #22c55e;
}
.import-dialog__recent-row--error,
.import-dialog__recent-row--failed {
  border-left: 3px solid #ef4444;
}
.import-dialog__recent-line {
  display: flex;
  justify-content: space-between;
  gap: 0.5rem;
  font-size: 0.85rem;
}
.import-dialog__recent-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.import-dialog__recent-status {
  text-transform: uppercase;
  font-size: 0.7rem;
  opacity: 0.7;
}
.import-dialog__recent-meta {
  font-size: 0.75rem;
  opacity: 0.65;
  margin-top: 0.2rem;
}
.import-dialog__recent-error {
  font-size: 0.75rem;
  color: #ef4444;
  margin-top: 0.25rem;
}
.import-dialog__recent-empty {
  font-size: 0.8rem;
  opacity: 0.6;
  margin: 0.5rem 0 0;
}
.import-dialog__actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.75rem;
  margin-top: 1.5rem;
}
.import-dialog__cancel-btn,
.import-dialog__import-btn {
  padding: 0.5rem 1.25rem;
  border: 1px solid var(--color-border, #333);
  border-radius: 4px;
  background: transparent;
  color: inherit;
  cursor: pointer;
}
.import-dialog__import-btn {
  background: var(--color-primary, #60a5fa);
  color: #fff;
}
.import-dialog__import-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
