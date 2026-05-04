<template>
  <SettingsSection id="local-models" title="Local Models" :default-open="true">
    <p class="settings-page__hint">
      Jarvis runs entirely on your machine via Ollama. Pick the active chat model
      and see how much headroom your hardware has for bigger ones.
    </p>

    <!-- Runtime status card -->
    <div
      class="local-models__runtime-card"
      :class="localModels.isOllamaReady() ? 'local-models__runtime-card--ok' : 'local-models__runtime-card--missing'"
    >
      <Icon
        :name="localModels.isOllamaReady() ? 'ph:check-circle-fill' : 'ph:warning-fill'"
        :class="['icon--lg', localModels.isOllamaReady() ? 'icon--success' : 'icon--warning']"
        class="local-models__runtime-icon"
      />
      <div class="local-models__runtime-info">
        <span class="local-models__runtime-title">
          {{ localModels.isOllamaReady()
            ? `Ollama running${localModels.runtime.value?.version ? ' · v' + localModels.runtime.value.version : ''}`
            : 'Local runtime not detected' }}
        </span>
        <span v-if="!localModels.isOllamaReady()" class="local-models__runtime-hint">
          Install Ollama to use local models.
          <a :href="ollamaDownloadUrl" target="_blank" rel="noopener" class="settings-page__link">Download Ollama →</a>
        </span>
      </div>
      <button
        v-if="!localModels.isOllamaReady()"
        class="settings-page__btn settings-page__btn--sm"
        @click="localModels.refreshAll()"
      >
        Check Again
      </button>
    </div>

    <!-- Capacity strip — what your hardware can run -->
    <div v-if="setupFlow.hardwareSummary.value" class="local-models__capacity">
      <div class="local-models__capacity-row">
        <Icon name="ph:desktop-tower" class="icon--lg icon--accent local-models__capacity-icon" />
        <div class="local-models__capacity-text">
          <span class="local-models__capacity-machine">{{ setupFlow.hardwareSummary.value.label }}</span>
          <span class="local-models__capacity-headroom">
            Runs <strong>{{ setupFlow.hardwareSummary.value.runsComfortably }}</strong> comfortably
            · effective context <strong>{{ setupFlow.hardwareSummary.value.effectiveContext }}</strong>
          </span>
        </div>
      </div>
    </div>

    <!-- Active model hero card -->
    <div
      v-if="activeModel"
      class="local-models__active"
      :class="{ 'local-models__active--healthy': activeStatus === 'healthy', 'local-models__active--slow': activeStatus === 'slow', 'local-models__active--fast': activeStatus === 'fast' }"
    >
      <div class="local-models__active-header">
        <div class="local-models__active-title-block">
          <span class="local-models__active-eyebrow">Active chat model</span>
          <span class="local-models__active-name">
            {{ activeModel.label }}
            <span class="local-models__active-dot" />
          </span>
          <span class="local-models__active-capability">{{ capabilityLabel(activeModel.preset) }}</span>
        </div>
        <span class="local-models__quality" :title="qualityDots(activeModel.preset).label">
          <span
            v-for="i in qualityDots(activeModel.preset).filled"
            :key="'af'+i"
            class="local-models__dot local-models__dot--filled"
            :style="{ '--dot-index': i }"
          />
          <span
            v-for="i in qualityDots(activeModel.preset).empty"
            :key="'ae'+i"
            class="local-models__dot local-models__dot--empty"
          />
        </span>
      </div>

      <dl class="local-models__active-stats">
        <div class="local-models__active-stat">
          <dt>Speed</dt>
          <dd>
            <template v-if="activeBaseline">
              <span class="local-models__active-stat-value">{{ activeBaseline.toFixed(1) }}</span>
              <span class="local-models__active-stat-unit">tok/s</span>
              <span
                v-if="activeStatus !== 'unknown'"
                class="local-models__health-pill"
                :class="`local-models__health-pill--${activeStatus}`"
              >{{ activeStatus }}</span>
            </template>
            <template v-else>
              <span class="local-models__active-stat-empty">Not measured · run probe below</span>
            </template>
          </dd>
        </div>
        <div class="local-models__active-stat">
          <dt>Context</dt>
          <dd>
            <span class="local-models__active-stat-value">{{ formatContext(activeModel.context_window) }}</span>
            <span class="local-models__active-stat-unit">tokens</span>
          </dd>
        </div>
        <div class="local-models__active-stat">
          <dt>Size on disk</dt>
          <dd>
            <span class="local-models__active-stat-value">{{ activeModel.download_size_gb }}</span>
            <span class="local-models__active-stat-unit">GB</span>
          </dd>
        </div>
      </dl>

      <div class="local-models__active-controls">
        <label class="local-models__switcher">
          <span class="local-models__switcher-label">Change model</span>
          <select
            class="local-models__switcher-select"
            :value="activeModel.ollama_model"
            :disabled="installedLocalModels.length < 2"
            @change="onChangeActive(($event.target as HTMLSelectElement).value)"
          >
            <option
              v-for="m in installedLocalModels"
              :key="m.ollama_model"
              :value="m.ollama_model"
            >{{ m.label }} · {{ qualityDotsText(m.preset) }} · {{ formatContext(m.context_window) }} ctx</option>
          </select>
        </label>
        <div class="local-models__active-actions">
          <button
            class="settings-page__btn settings-page__btn--sm"
            :disabled="!localModels.isOllamaReady()"
            @click="localModels.warmUpModel(activeModel.ollama_model)"
          >Warm up</button>
          <button
            class="settings-page__btn settings-page__btn--sm settings-page__btn--danger"
            @click="handleDeleteModel(activeModel)"
          >Remove</button>
        </div>
      </div>

      <p
        v-if="installedLocalModels.length === 1"
        class="local-models__active-hint"
      >
        Only one model installed. Add another below to switch between them.
      </p>
    </div>

    <!-- No-active fallback -->
    <div v-else-if="installedLocalModels.length === 0 && localModels.isOllamaReady()" class="local-models__empty">
      No local models installed yet. Install one below to start chatting.
    </div>

    <!-- Other installed models — only when 2+ exist (active is in the dropdown above) -->
    <details v-if="otherInstalledModels.length > 0" class="local-models__other-installed">
      <summary class="local-models__all-toggle">
        Other installed ({{ otherInstalledModels.length }})
      </summary>
      <div class="local-models__installed-list">
        <div
          v-for="m in otherInstalledModels"
          :key="m.model_id"
          class="local-models__installed-row"
        >
          <div class="local-models__installed-info">
            <span class="local-models__installed-name">{{ m.label }}</span>
            <span class="local-models__quality" :title="qualityDots(m.preset).label">
              <span
                v-for="i in qualityDots(m.preset).filled"
                :key="'of'+i"
                class="local-models__dot local-models__dot--filled"
                :style="{ '--dot-index': i }"
              />
              <span
                v-for="i in qualityDots(m.preset).empty"
                :key="'oe'+i"
                class="local-models__dot local-models__dot--empty"
              />
            </span>
            <span class="local-models__installed-meta">{{ m.download_size_gb }} GB · {{ formatContext(m.context_window) }} ctx</span>
          </div>
          <div class="local-models__installed-actions">
            <button
              class="settings-page__btn settings-page__btn--sm"
              @click="localModels.selectModel(m.model_id)"
            >Set active</button>
            <button
              class="settings-page__btn settings-page__btn--sm settings-page__btn--danger"
              @click="handleDeleteModel(m)"
            >Remove</button>
          </div>
        </div>
      </div>
    </details>

    <!-- Chat-model self-test (ADR 012) — measures real tok/s on this machine -->
    <div v-if="localModels.isOllamaReady() && installedLocalModels.length > 0" class="local-models__group">
      <ChatModelProbePanel variant="settings" />
    </div>

    <!-- Install more — collapsed install surface -->
    <details v-if="localModels.isOllamaReady() && uninstalledModels.length > 0" class="local-models__install-more">
      <summary class="local-models__install-summary">
        <span>Install another model</span>
        <span class="local-models__install-count">{{ uninstalledModels.length }} available</span>
      </summary>

      <div v-if="recommendedToInstall.length > 0" class="local-models__group">
        <h3 class="local-models__group-title">Recommended for your hardware</h3>
        <div class="local-models__grid">
          <LocalModelCard
            v-for="m in recommendedToInstall"
            :key="m.model_id"
            :model="m"
            :pulling="localModels.pulling.value === m.model_id"
            :progress="localModels.pulling.value === m.model_id ? localModels.pullProgress.value : null"
            :disabled="!localModels.isOllamaReady()"
            @pull="localModels.pullModel($event)"
            @select="localModels.selectModel($event)"
            @cancel="localModels.cancelPull()"
          />
        </div>
      </div>

      <div v-if="otherUninstalled.length > 0" class="local-models__group">
        <h3 class="local-models__group-title">All other models</h3>
        <div class="local-models__grid">
          <LocalModelCard
            v-for="m in otherUninstalled"
            :key="m.model_id"
            :model="m"
            :pulling="localModels.pulling.value === m.model_id"
            :progress="localModels.pulling.value === m.model_id ? localModels.pullProgress.value : null"
            :disabled="!localModels.isOllamaReady()"
            compact
            @pull="localModels.pullModel($event)"
            @select="localModels.selectModel($event)"
            @cancel="localModels.cancelPull()"
          />
        </div>
      </div>
    </details>

    <!-- Advanced connection settings -->
    <details class="local-models__advanced">
      <summary class="local-models__all-toggle">Advanced connection settings</summary>
      <div class="local-models__url">
        <label class="local-models__url-label">Ollama URL</label>
        <div class="local-models__url-row">
          <input
            type="text"
            class="settings-page__input"
            :value="localModels.baseUrl.value"
            @change="localModels.setBaseUrl(($event.target as HTMLInputElement).value)"
            placeholder="http://localhost:11434"
          />
          <button class="settings-page__btn" @click="localModels.refreshAll()">
            Test Connection
          </button>
        </div>
      </div>
    </details>

    <p v-if="localModels.error.value" class="local-models__error">
      {{ localModels.error.value }}
    </p>
  </SettingsSection>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import { qualityDots, qualityDotsText } from '~/composables/settings/useSharpen'

const localModels = useLocalModels()
const setupFlow = useLocalSetupFlow()
const chatHealth = useChatHealth()
const chatModel = useChatModel()

const ollamaDownloadUrl = computed(() => {
  const urls: Record<string, string> = {
    macos: 'https://ollama.com/download/mac',
    windows: 'https://ollama.com/download/windows',
    linux: 'https://ollama.com/download/linux',
  }
  return urls[setupFlow.detectedOS.value] ?? 'https://ollama.com/download'
})

const installedLocalModels = computed(() =>
  localModels.catalog.value.filter(m => m.installed),
)

const activeModel = computed(() =>
  installedLocalModels.value.find(m => m.active) ?? null,
)

const otherInstalledModels = computed(() =>
  installedLocalModels.value.filter(m => !m.active),
)

const uninstalledModels = computed(() =>
  localModels.catalog.value.filter(m => !m.installed),
)

const recommendedToInstall = computed(() =>
  localModels.recommendedModels.value
    .filter(m => !m.installed)
    .slice(0, 3),
)

const otherUninstalled = computed(() => {
  const recIds = new Set(recommendedToInstall.value.map(m => m.model_id))
  return uninstalledModels.value.filter(m => !recIds.has(m.model_id))
})

const activeBaseline = computed(() => {
  const m = activeModel.value
  if (!m) return null
  return chatHealth.getBaseline(m.ollama_model)
})

const activeStatus = computed(() => {
  const m = activeModel.value
  if (!m) return 'unknown'
  return chatHealth.statusFor(m.ollama_model)
})

function capabilityLabel(preset: string): string {
  const map: Record<string, string> = {
    'fast': 'Quick chat — fast replies, lighter depth',
    'everyday': 'Everyday chat — balanced for most tasks',
    'balanced': 'Solid chat — good speed and depth',
    'long-docs': 'Long-context — chew through large docs',
    'reasoning': 'Reasoning — slower, deeper thinking',
    'code': 'Coding — strong on technical tasks',
    'best-local': 'Top quality on this machine',
  }
  return map[preset] ?? 'Local chat model'
}

function formatContext(raw: string | number | undefined): string {
  if (raw == null || raw === '') return '—'
  const n = typeof raw === 'number' ? raw : Number(raw)
  if (!Number.isFinite(n) || n <= 0) return String(raw)
  if (n >= 1000) return `${(n / 1000).toFixed(0)}K`
  return String(n)
}

async function onChangeActive(ollamaModel: string) {
  const target = installedLocalModels.value.find(m => m.ollama_model === ollamaModel)
  if (!target || target.active) return
  await localModels.selectModel(target.model_id)
  chatModel.selectModel(target.ollama_model)
}

async function handleDeleteModel(model: { ollama_model: string }) {
  await localModels.deleteModel(model.ollama_model)
  await localModels.refreshAll()
}

onMounted(async () => {
  await localModels.refreshAll()
  await chatHealth.ensureBaselinesLoaded()
})
</script>
