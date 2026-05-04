/**
 * Active chat-model state — replaces the multi-provider `useApiKeys` per ADR 015.
 *
 * The v1 stack has a single dispatch target (local Ollama). This composable
 * is the shared reactive state for "which local model is selected right now"
 * plus a localStorage-backed persistence layer so the selection survives a
 * reload. There are no API keys, no cloud providers, no provider abstraction.
 *
 * The `activeProvider` ref is kept on the surface (always `"ollama"`) so chat
 * WS payload builders and existing template conditionals stay mechanical
 * pending a future cleanup.
 */

const _DEFAULT_MODEL = 'qwen3:8b'
const _STORAGE_KEY = 'jarvis_active_model'

/**
 * One-time prefix migration. Strips a stale `ollama_chat/` prefix from any
 * value persisted by an older build. The wire protocol now passes the raw
 * Ollama tag end-to-end (the LiteLLM-style prefix was vestigial after ADR
 * 015 dropped LiteLLM). Without this, a dev whose localStorage still holds
 * `ollama_chat/qwen3:8b` would see the catalog active-flag fail to match
 * any installed model and the chat header fall back to the default.
 */
function _normalizeStored(raw: string | null): string {
  if (!raw) return _DEFAULT_MODEL
  return raw.startsWith('ollama_chat/') ? raw.slice('ollama_chat/'.length) : raw
}

export function useChatModel() {
  const activeProvider = useState<'ollama'>('activeProvider', () => 'ollama')

  const activeModel = useState<string>('activeModel', () => {
    try {
      const saved = _normalizeStored(localStorage.getItem(_STORAGE_KEY))
      if (saved !== localStorage.getItem(_STORAGE_KEY)) {
        localStorage.setItem(_STORAGE_KEY, saved)
      }
      return saved
    } catch { /* ignore — SSR or storage blocked */ }
    return _DEFAULT_MODEL
  })

  function selectModel(modelId: string): void {
    activeModel.value = modelId
    try {
      localStorage.setItem(_STORAGE_KEY, modelId)
    } catch { /* ignore */ }
  }

  return {
    activeProvider,
    activeModel,
    selectModel,
  }
}
