import type { HardwareProfile, RuntimeStatus, ModelRecommendation, PullProgress } from '~/types'

const OLLAMA_BASE_URL_KEY = 'jarvis-ollama-base-url'

// Empty string means "no client-side override — let the backend's
// JARVIS_OLLAMA_BASE_URL env var win". In the bundled product the Tauri
// shell points the backend at `http://127.0.0.1:11435` (the private
// bundled-Ollama port), so the frontend has no business hardcoding
// `:11434` (the upstream Ollama default) on top. The Settings →
// Advanced UI is the only path that should set a non-empty value.
const LEGACY_DEFAULT_BASE_URL = 'http://localhost:11434'

function _readBaseUrl(): string {
  try {
    const v = localStorage.getItem(OLLAMA_BASE_URL_KEY) ?? ''
    // Old builds shipped with `:11434` written to localStorage on first
    // load. That value used to be a no-op default; under the bundled
    // architecture it now actively overrides the correct backend URL.
    // Treat it as "not set" so we don't fight the backend.
    return v === LEGACY_DEFAULT_BASE_URL ? '' : v
  } catch {
    return ''
  }
}

export function useLocalModels() {
  const hardware = useState<HardwareProfile | null>('local-hardware', () => null)
  const runtime = useState<RuntimeStatus | null>('local-runtime', () => null)
  const catalog = useState<ModelRecommendation[]>('local-catalog', () => [])
  const pulling = useState<string | null>('local-pulling', () => null)
  const pullProgress = useState<PullProgress | null>('local-pull-progress', () => null)
  const loading = useState<boolean>('local-loading', () => false)
  const error = useState<string | null>('local-error', () => null)
  const baseUrl = useState<string>('local-base-url', () => _readBaseUrl())
  const ollamaDown = useState<boolean>('local-ollama-down', () => false)
  let _healthInterval: ReturnType<typeof setInterval> | null = null
  let _pullAbortController: AbortController | null = null

  // Helpers — only forward base_url when the user has explicitly set one.
  // Empty string means "use backend default" (bundled :11435 in production).
  function _baseUrlParams(): Record<string, string> {
    return baseUrl.value ? { base_url: baseUrl.value } : {}
  }
  function _baseUrlBody<T extends Record<string, unknown>>(extra: T): T {
    return baseUrl.value ? ({ ...extra, base_url: baseUrl.value } as T) : extra
  }

  async function fetchHardware(): Promise<void> {
    try {
      hardware.value = await $fetch<HardwareProfile>(apiUrl('/api/local/hardware'))
    } catch (e: unknown) {
      error.value = 'Failed to fetch hardware info'
    }
  }

  async function fetchRuntime(): Promise<void> {
    try {
      runtime.value = await $fetch<RuntimeStatus>(apiUrl('/api/local/runtime'), {
        params: _baseUrlParams(),
      })
    } catch (e: unknown) {
      runtime.value = {
        runtime: 'ollama',
        installed: false,
        running: false,
        base_url: baseUrl.value,
        reachable: false,
      }
    }
  }

  async function fetchCatalog(): Promise<void> {
    try {
      catalog.value = await $fetch<ModelRecommendation[]>(apiUrl('/api/local/models/catalog'), {
        params: _baseUrlParams(),
      })
      // If the selected model is no longer installed, fall back to the
      // first active/installed model so the UI doesn't look broken.
      try {
        const chatModel = useChatModel()
        const isStillAvailable = catalog.value.some(
          m => m.installed && m.ollama_model === chatModel.activeModel.value,
        )
        if (isStillAvailable) return
        const fallback = catalog.value.find(m => m.active) ?? catalog.value.find(m => m.installed)
        if (fallback) chatModel.selectModel(fallback.ollama_model)
      } catch { /* ignore — composable unavailable in non-Nuxt contexts */ }
    } catch (e: unknown) {
      error.value = 'Failed to fetch model catalog'
    }
  }

  async function refreshAll(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      await fetchHardware()
      await fetchRuntime()
      // Always fetch catalog — models are scored by hardware even without Ollama
      await fetchCatalog()
    } finally {
      loading.value = false
    }
  }

  async function pullModel(modelId: string): Promise<void> {
    const model = catalog.value.find(m => m.model_id === modelId)
    if (!model) return

    pulling.value = modelId
    pullProgress.value = { status: 'starting' }
    error.value = null

    _pullAbortController = new AbortController()

    try {
      const response = await fetch(apiUrl('/api/local/models/pull'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: _pullAbortController.signal,
        body: JSON.stringify(_baseUrlBody({ model: model.ollama_model })),
      })

      if (!response.ok || !response.body) {
        throw new Error(`Pull failed: ${response.status}`)
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        const text = decoder.decode(value)
        for (const line of text.split('\n')) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6))
              if (data.error) {
                const raw: string = data.error
                const snackbar = useSnackbar()
                if (raw.includes('requires a newer version of Ollama')) {
                  snackbar.error(
                    'Ten model wymaga nowszej wersji Ollama.',
                    { label: 'Pobierz aktualizację →', href: 'https://ollama.com/download' },
                  )
                } else {
                  snackbar.error(raw, undefined, 8000)
                }
                pulling.value = null
                pullProgress.value = null
                return
              }
              pullProgress.value = data
              if (data.status === 'done' || data.status === 'success') {
                pulling.value = null
                pullProgress.value = null
                await fetchCatalog()
              }
            } catch {
              // ignore malformed SSE lines
            }
          }
        }
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : 'Pull failed'
    } finally {
      if (pulling.value === modelId) {
        pulling.value = null
        pullProgress.value = null
      }
    }
  }

  function cancelPull(): void {
    if (_pullAbortController) {
      _pullAbortController.abort()
      _pullAbortController = null
    }
    pulling.value = null
    pullProgress.value = null
  }

  async function selectModel(modelId: string): Promise<void> {
    const model = catalog.value.find(m => m.model_id === modelId)
    if (!model) return

    try {
      await $fetch(apiUrl('/api/local/models/select'), {
        method: 'POST',
        body: _baseUrlBody({
          model_id: model.model_id,
          ollama_model: model.ollama_model,
        }),
      })
      await fetchCatalog()
      // Sync chat-side active model so the ModelSelector in the chat
      // header reflects the newly activated local model. Without this, the
      // model is marked "Active" in Settings but the chat still points at
      // a stale local one.
      try {
        const chatModel = useChatModel()
        chatModel.selectModel(model.ollama_model)
      } catch { /* ignore — composable unavailable in non-Nuxt contexts */ }
    } catch (e: unknown) {
      error.value = 'Failed to select model'
    }
  }

  async function deleteModel(ollamaModel: string): Promise<void> {
    try {
      await $fetch(apiUrl(`/api/local/models/${encodeURIComponent(ollamaModel)}`), {
        method: 'DELETE',
        params: _baseUrlParams(),
      })
      await fetchCatalog()
    } catch (e: unknown) {
      error.value = 'Failed to delete model'
    }
  }

  function isOllamaReady(): boolean {
    return !!(runtime.value?.installed && runtime.value?.running)
  }

  function startHealthPolling(intervalMs = 30_000): void {
    stopHealthPolling()
    _healthInterval = setInterval(async () => {
      await fetchRuntime()
      ollamaDown.value = !(runtime.value?.reachable)
    }, intervalMs)
  }

  function stopHealthPolling(): void {
    if (_healthInterval) {
      clearInterval(_healthInterval)
      _healthInterval = null
    }
  }

  async function warmUpModel(ollamaModel: string): Promise<void> {
    try {
      await $fetch(apiUrl('/api/local/models/warm-up'), {
        method: 'POST',
        body: _baseUrlBody({ model: ollamaModel }),
      })
    } catch {
      // Fire-and-forget; ignore errors
    }
  }

  function setBaseUrl(url: string): void {
    baseUrl.value = url
    try {
      localStorage.setItem(OLLAMA_BASE_URL_KEY, url)
    } catch { /* ignore */ }
  }

  const recommendedModels = computed(() =>
    catalog.value.filter(m => m.recommended),
  )

  const installedModels = computed(() =>
    catalog.value.filter(m => m.installed),
  )

  const activeModel = computed(() =>
    catalog.value.find(m => m.active),
  )

  return {
    hardware,
    runtime,
    catalog,
    pulling,
    pullProgress,
    loading,
    error,
    baseUrl,
    ollamaDown,
    fetchHardware,
    fetchRuntime,
    fetchCatalog,
    refreshAll,
    pullModel,
    cancelPull,
    selectModel,
    deleteModel,
    isOllamaReady,
    startHealthPolling,
    stopHealthPolling,
    warmUpModel,
    setBaseUrl,
    recommendedModels,
    installedModels,
    activeModel,
  }
}
