import type { ChatModelProbeStatus, ModelRecommendation } from '~/types'
import { useChatModelProbe } from '~/composables/useChatModelProbe'
import { useLocalModels } from '~/composables/useLocalModels'
import { useSnackbar } from '~/composables/useSnackbar'

/**
 * Chat-health watcher (ADR 005 §C trigger 2).
 *
 * Replaces the disabled pre-flight RAM check with an empirical
 * observed-vs-baseline comparison. The chat-model-probe (ADR 012)
 * captures `realistic_tps` per candidate model on this exact machine
 * during install / re-run; that's the per-model baseline. We compare
 * each turn's observed decode_tps against the same model's baseline,
 * roll a small window, and emit a single soft-hint toast on sustained
 * drift. Never gates dispatch. Never auto-swaps.
 *
 * Two triggers, both advisory:
 *
 * - **Sustained-slow** (ratio < 0.5 across the full window) →
 *   "Your model is running at <X>% of expected speed. Re-test models
 *   or close other apps." Cooldown 10 min per (model, slow).
 *
 * - **Sustained-fast with a heavier rung available** (ratio > 1.05
 *   AND a heavier installed catalog model passed its probe) →
 *   "You may have headroom for {model}. Re-test to confirm."
 *   Cooldown 24h per (model, fast).
 *
 * Policy choices:
 *
 * - Window size 5 turns. Smaller (3) is too jumpy on a model that
 *   warms up; larger (10) takes forever to fire on a real regression.
 * - Ratio threshold 0.5 for slow — the empirical "model is paging
 *   noticeably" line on Apple Silicon unified memory. Lower would
 *   only catch catastrophic states; higher would nag.
 * - Status (`healthy` / `slow` / `fast` / `unknown`) is derived from
 *   the latest sample alone, not the whole window. The chat bubble
 *   shows the per-turn telemetry tinted by *that turn's* status —
 *   honest reading of "this turn was slow" rather than "the model
 *   is currently slow."
 */

interface ChatHealthState {
  // Baseline decode_tps per ollama_model (e.g. "qwen3:8b" → 18.4).
  // Sourced from the probe's persisted `candidates_evaluated[i].realistic_tps`.
  baselines: Record<string, number>
  // Rolling window of recent decode_tps observations per ollama_model.
  // Newest at the end; capped at WINDOW_SIZE.
  windows: Record<string, number[]>
  // Last firing timestamp (ms epoch) per (ollama_model, kind) so we
  // can apply cooldowns without re-nagging.
  lastHintAt: Record<string, number>
  // Have we tried to load baselines from /api/local/chat-model-probe?
  baselinesLoaded: boolean
}

const WINDOW_SIZE = 5
const SLOW_RATIO = 0.5
const FAST_RATIO = 1.05
const SLOW_COOLDOWN_MS = 10 * 60 * 1000
const FAST_COOLDOWN_MS = 24 * 60 * 60 * 1000

/** Strip the LiteLLM-era prefix so the key matches probe / catalog records. */
function _normalize(model: string): string {
  return model.replace(/^ollama(?:_chat)?\//, '')
}

export function useChatHealth() {
  const state = useState<ChatHealthState>('chat-health', () => ({
    baselines: {},
    windows: {},
    lastHintAt: {},
    baselinesLoaded: false,
  }))

  const probe = useChatModelProbe()
  const localModels = useLocalModels()
  const snackbar = useSnackbar()

  /**
   * Load baselines from the persisted probe record. Idempotent — only
   * fetches the first time it's called per page session. Re-fetched
   * after the user runs a fresh probe via `refreshBaselines()`.
   */
  async function ensureBaselinesLoaded(): Promise<void> {
    if (state.value.baselinesLoaded) return
    state.value.baselinesLoaded = true
    await refreshBaselines()
  }

  async function refreshBaselines(): Promise<void> {
    let status: ChatModelProbeStatus | null = probe.status.value
    if (!status) {
      status = await probe.fetchStatus()
    }
    const persisted = status?.persisted
    if (!persisted?.candidates_evaluated?.length) return
    const next: Record<string, number> = {}
    for (const ev of persisted.candidates_evaluated) {
      if (ev.realistic_tps != null && ev.realistic_tps > 0) {
        next[_normalize(ev.model)] = ev.realistic_tps
      }
    }
    state.value.baselines = next
  }

  function getBaseline(model: string): number | null {
    return state.value.baselines[_normalize(model)] ?? null
  }

  /** Pure status classifier — no side effects, safe to call from render. */
  function statusFor(model: string): 'healthy' | 'slow' | 'fast' | 'unknown' {
    const key = _normalize(model)
    const baseline = state.value.baselines[key]
    const window = state.value.windows[key]
    if (!baseline || !window?.length) return 'unknown'
    const latest = window[window.length - 1]
    const ratio = latest / baseline
    if (ratio < SLOW_RATIO) return 'slow'
    if (ratio > FAST_RATIO) return 'fast'
    return 'healthy'
  }

  /**
   * Fold one turn's observed decode_tps into the rolling window for
   * `model` and evaluate the soft-hint triggers. The watcher only
   * fires on a *full* window — a single fast/slow turn is noise, not
   * signal.
   */
  function recordTurn(model: string, decodeTps: number): void {
    if (!Number.isFinite(decodeTps) || decodeTps <= 0) return
    const key = _normalize(model)
    const existing = state.value.windows[key] ?? []
    const next = [...existing, decodeTps].slice(-WINDOW_SIZE)
    state.value.windows = { ...state.value.windows, [key]: next }
    _evaluateTriggers(key)
  }

  function _evaluateTriggers(modelKey: string): void {
    const baseline = state.value.baselines[modelKey]
    const window = state.value.windows[modelKey] ?? []
    if (!baseline || window.length < WINDOW_SIZE) return
    const mean = window.reduce((a, b) => a + b, 0) / window.length
    const ratio = mean / baseline
    if (ratio < SLOW_RATIO && _allBelow(window, baseline, SLOW_RATIO)) {
      _maybeHintSlow(modelKey, ratio)
    } else if (ratio > FAST_RATIO && _allAbove(window, baseline, FAST_RATIO)) {
      _maybeHintFast(modelKey, ratio)
    }
  }

  function _allBelow(window: number[], baseline: number, ratio: number): boolean {
    return window.every(v => v / baseline < ratio)
  }

  function _allAbove(window: number[], baseline: number, ratio: number): boolean {
    return window.every(v => v / baseline > ratio)
  }

  function _withinCooldown(key: string, cooldownMs: number): boolean {
    const last = state.value.lastHintAt[key] ?? 0
    return Date.now() - last < cooldownMs
  }

  function _markHinted(key: string): void {
    state.value.lastHintAt = { ...state.value.lastHintAt, [key]: Date.now() }
  }

  function _maybeHintSlow(modelKey: string, ratio: number): void {
    const cooldownKey = `${modelKey}:slow`
    if (_withinCooldown(cooldownKey, SLOW_COOLDOWN_MS)) return
    _markHinted(cooldownKey)
    const pct = Math.round(ratio * 100)
    snackbar.warning(
      `${modelKey} is running at ~${pct}% of expected speed. Try a smaller model or close other apps.`,
      { label: 'Re-test models', href: '/settings#local-models' },
      12_000,
    )
  }

  function _maybeHintFast(modelKey: string, ratio: number): void {
    const cooldownKey = `${modelKey}:fast`
    if (_withinCooldown(cooldownKey, FAST_COOLDOWN_MS)) return
    const heavier = _findHeavierInstalledRung(modelKey)
    if (!heavier) return
    _markHinted(cooldownKey)
    const pct = Math.round(ratio * 100)
    snackbar.show(
      `${modelKey} is running at ~${pct}% of baseline — you may have headroom for ${heavier.label || heavier.ollama_model}.`,
      {
        type: 'info',
        action: { label: 'Re-test models', href: '/settings#local-models' },
        duration: 12_000,
      },
    )
  }

  /**
   * Find the next-heavier installed catalog rung above `current` that
   * also has a passing probe baseline (i.e. we have evidence it works
   * on this hardware). Returns null when no qualifying upgrade exists.
   *
   * Ordering caveat: we sort by `download_size_gb`, which is *quant-
   * adjusted disk size*, not parameter count. On the current Tier A
   * chat ladder (Qwen3-4B Q4 ~3 GB → Qwen3-8B Q4 ~6 GB → Qwen3-30B-A3B
   * Q4 ~20 GB) all models are at Q4_K_M and the ordering is monotonic
   * with parameter count, so this proxy is correct in v1. If the
   * catalog ever mixes quants — e.g. a Q8 4B (~5 GB) vs a Q4 8B
   * (~6 GB) — `download_size_gb` would no longer monotonically follow
   * "heavier model"; revisit with an explicit ladder ordering field
   * (ADR 005's `ladder_positions` is the right source of truth)
   * before adding mixed-quant entries.
   */
  function _findHeavierInstalledRung(current: string): ModelRecommendation | null {
    const catalog = localModels.catalog.value
    const currentEntry = catalog.find(m => _normalize(m.ollama_model) === current)
    if (!currentEntry) return null
    const installed = catalog.filter(m => m.installed && m.ollama_model !== currentEntry.ollama_model)
    const heavier = installed.filter(m => m.download_size_gb > currentEntry.download_size_gb)
    if (!heavier.length) return null
    heavier.sort((a, b) => a.download_size_gb - b.download_size_gb)
    // Only suggest a rung that has a passing probe baseline — without
    // one we have no evidence the heavier model actually runs OK on
    // this machine.
    for (const candidate of heavier) {
      if (state.value.baselines[_normalize(candidate.ollama_model)] != null) {
        return candidate
      }
    }
    return null
  }

  /** Reset for tests; not used in production. */
  function _reset(): void {
    state.value = {
      baselines: {},
      windows: {},
      lastHintAt: {},
      baselinesLoaded: false,
    }
  }

  return {
    ensureBaselinesLoaded,
    refreshBaselines,
    getBaseline,
    statusFor,
    recordTurn,
    _reset,
  }
}
