<!--
  ChatFirstTurnWarmup — first-turn-only bootstrap surface.

  Why this exists: the first chat turn of a session pays a one-time
  cost — Ollama loads the model into memory, the KV cache is empty so
  the system prompt + history must be fully prefilled, and the
  fastembed/tokenizer/HF caches may still be warming. On 24 GB Apple M5
  with the bundled Ollama 0.18.0 (pinned for Metal-4 compatibility) this
  cost is ~16 s for a typical chat turn. Subsequent turns reuse the
  KV-cache prefix and land in 1-2 s — see ADR 009 amendment 2026-05-01.

  A generic spinner/typing-dots indicator over a 16 s wait reads as
  "broken." This component replaces it for turn 1 with an honest
  instrument-panel surface: shows what's actually happening, names the
  one-time nature of the cost, and gives the user a stable mental model
  for the rest of the session.

  Strict scope: shown ONLY for the first turn of a fresh session
  (no assistant message has ever rendered yet). Subsequent turns —
  including slow ones caught by the chat-health watcher — render the
  existing typing-dots indicator and the per-turn telemetry pill. The
  warmup surface is a one-shot.

  Aesthetic intent: monospace-only, hairline borders, instrument-panel
  cadence. The product is a compliance/security tool whose audience
  expects engineering rigor; the bootstrap surface should look like a
  measurement, not a marketing animation.
-->
<template>
  <div class="warmup" role="status" aria-live="polite">
    <div class="warmup__frame">
      <div class="warmup__corner warmup__corner--tl" />
      <div class="warmup__corner warmup__corner--tr" />
      <div class="warmup__corner warmup__corner--bl" />
      <div class="warmup__corner warmup__corner--br" />

      <header class="warmup__header">
        <span class="warmup__label">
          <span class="warmup__label-dot" />
          BOOTSTRAPPING LOCAL MODEL
        </span>
        <span class="warmup__timer">
          <span class="warmup__timer-elapsed">{{ elapsedDisplay }}</span>
          <span class="warmup__timer-sep">·</span>
          <span class="warmup__timer-est">~{{ estimatedTotalSec }}s</span>
        </span>
      </header>

      <div class="warmup__rule" />

      <ul class="warmup__stages">
        <li
          v-for="(stage, i) in stages"
          :key="stage.id"
          class="warmup__stage"
          :class="{
            'warmup__stage--done': stageStatus(i) === 'done',
            'warmup__stage--active': stageStatus(i) === 'active',
            'warmup__stage--pending': stageStatus(i) === 'pending',
          }"
        >
          <span class="warmup__stage-marker" aria-hidden="true">
            <Icon v-if="stageStatus(i) === 'done'" name="ph:check-bold" class="icon--sm" />
            <template v-else-if="stageStatus(i) === 'active'">●</template>
            <template v-else>·</template>
          </span>
          <span class="warmup__stage-name">{{ stage.label }}</span>
          <span class="warmup__stage-detail">{{ stage.detail }}</span>
        </li>
      </ul>

      <div class="warmup__rule" />

      <p class="warmup__note">
        First message bootstraps the local model.
        <br>
        Follow-up replies typically land in <span class="warmup__note-em">1–2&nbsp;s</span>.
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

interface StageDef { id: string; label: string; detail: string; until: number }

const props = withDefaults(
  defineProps<{
    /** Wall-clock when the user's first turn was dispatched (ms epoch). */
    startedAt: number
    /** Honest estimate of total bootstrap time on this hardware (seconds). */
    estimatedTotalSec?: number
  }>(),
  { estimatedTotalSec: 15 },
)

// ── Live elapsed counter ─────────────────────────────────────────────
// rAF tick rather than setInterval — the timer is a visual element, not
// a control surface; we only need ~screen-refresh cadence and the rAF
// auto-pauses when the tab is backgrounded (don't drive a counter no
// one's looking at).
const nowMs = ref(Date.now())
let _raf: number | null = null
const _tick = () => {
  nowMs.value = Date.now()
  _raf = requestAnimationFrame(_tick)
}
onMounted(() => { _raf = requestAnimationFrame(_tick) })
onBeforeUnmount(() => { if (_raf !== null) cancelAnimationFrame(_raf) })

const elapsedSec = computed(() =>
  Math.max(0, Math.floor((nowMs.value - props.startedAt) / 1000)),
)
const elapsedDisplay = computed(() => {
  const s = elapsedSec.value
  const mm = Math.floor(s / 60).toString().padStart(2, '0')
  const ss = (s % 60).toString().padStart(2, '0')
  return `${mm}:${ss}`
})

// ── Stage model ──────────────────────────────────────────────────────
// "until" is the elapsed-second mark by which we expect this stage to
// be DONE. The numbers are estimates (we don't get per-stage signals
// from Ollama mid-turn), tuned to typical M5 + qwen3:8b + ~2.7K-token
// prompt timing. They're calibrated so the user's mental model tracks
// reality even though we're not driving real telemetry.
const stages = computed<StageDef[]>(() => [
  { id: 'load',    label: 'load model',     detail: 'Ollama mmap into unified memory',     until: 2 },
  { id: 'read',    label: 'read notes',     detail: 'BM25 + cosine over your local vault',  until: 3 },
  { id: 'compose', label: 'compose first',  detail: `prefilling on ${hardwareLabel.value}`, until: Math.max(5, props.estimatedTotalSec - 4) },
  { id: 'stream',  label: 'stream',         detail: 'first tokens incoming',                until: props.estimatedTotalSec },
])

const stageStatus = (i: number): 'done' | 'active' | 'pending' => {
  const s = elapsedSec.value
  const stage = stages.value[i]
  const prevUntil = i === 0 ? 0 : stages.value[i - 1].until
  if (s >= stage.until) return 'done'
  if (s >= prevUntil) return 'active'
  return 'pending'
}

// Honest hardware label without claiming we know exactly. Surfaces in
// the "compose first" stage detail. Defaults to a generic phrase if
// the userAgent doesn't pin Apple Silicon — better to stay vague than
// to claim a chip we can't detect.
const hardwareLabel = computed(() => {
  if (typeof navigator === 'undefined') return 'this machine'
  const ua = navigator.userAgent.toLowerCase()
  if (ua.includes('mac')) return 'Apple Silicon'
  return 'this machine'
})
</script>

<style scoped>
/*
  All values monospace, all rules hairlines. The instrument-panel feel
  comes from restraint: no rounded chrome, no shadows, no gradients.
  A single accent (phosphor amber #d4a017) marks active state. Dark
  surface inherits from the parent chat panel.
*/

.warmup {
  --line: rgba(255, 255, 255, 0.08);
  --line-strong: rgba(255, 255, 255, 0.16);
  --ink: rgba(255, 255, 255, 0.92);
  --ink-dim: rgba(255, 255, 255, 0.52);
  --ink-mute: rgba(255, 255, 255, 0.34);
  --accent: #d4a017;          /* phosphor amber — single accent */
  --accent-glow: rgba(212, 160, 23, 0.16);

  font-family: 'JetBrains Mono', 'Fira Code', ui-monospace, monospace;
  font-feature-settings: 'tnum' 1, 'zero' 1, 'cv11' 1;
  color: var(--ink);
  margin: 8px 0;
  animation: warmup-enter 0.42s cubic-bezier(0.2, 0.7, 0.2, 1) both;
}

.warmup__frame {
  position: relative;
  padding: 18px 22px 16px;
  border: 1px solid var(--line);
  background:
    /* faint horizontal scanlines — subtle, only visible on dark surface */
    repeating-linear-gradient(
      0deg,
      transparent 0,
      transparent 3px,
      rgba(255, 255, 255, 0.012) 3px,
      rgba(255, 255, 255, 0.012) 4px
    );
}

/* Decorative corner ticks — no ornamentation purpose, pure aesthetic
   signal that this is an instrument readout, not a chat message. */
.warmup__corner {
  position: absolute;
  width: 8px;
  height: 8px;
  border-color: var(--line-strong);
  border-style: solid;
  border-width: 0;
}
.warmup__corner--tl { top: -1px; left: -1px;    border-top-width: 1px;    border-left-width: 1px; }
.warmup__corner--tr { top: -1px; right: -1px;   border-top-width: 1px;    border-right-width: 1px; }
.warmup__corner--bl { bottom: -1px; left: -1px; border-bottom-width: 1px; border-left-width: 1px; }
.warmup__corner--br { bottom: -1px; right: -1px;border-bottom-width: 1px; border-right-width: 1px; }

.warmup__header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 10px;
}

.warmup__label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 10.5px;
  font-weight: 500;
  letter-spacing: 0.16em;
  color: var(--ink-dim);
  text-transform: uppercase;
}

.warmup__label-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 0 var(--accent-glow);
  animation: pulse 1.6s ease-in-out infinite;
}

.warmup__timer {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.04em;
}

.warmup__timer-elapsed {
  color: var(--ink);
  font-weight: 500;
}

.warmup__timer-sep {
  color: var(--ink-mute);
  margin: 0 1px;
}

.warmup__timer-est {
  color: var(--ink-mute);
}

.warmup__rule {
  height: 1px;
  background: var(--line);
  margin: 4px 0 12px;
}

.warmup__stages {
  list-style: none;
  margin: 0 0 12px;
  padding: 0;
  display: grid;
  gap: 7px;
}

.warmup__stage {
  display: grid;
  grid-template-columns: 18px 124px 1fr;
  align-items: baseline;
  gap: 12px;
  font-size: 12px;
  line-height: 1.55;
  transition: color 0.3s ease;
}

.warmup__stage-marker {
  font-size: 11.5px;
  width: 18px;
  text-align: center;
  font-feature-settings: 'tnum' 1;
}

.warmup__stage-name {
  font-weight: 500;
  letter-spacing: 0.01em;
}

.warmup__stage-detail {
  color: var(--ink-mute);
  font-size: 11.5px;
  letter-spacing: 0.005em;
}

/* Pending: dim, no animation. */
.warmup__stage--pending {
  color: var(--ink-mute);
}
.warmup__stage--pending .warmup__stage-marker {
  color: var(--ink-mute);
}

/* Done: full ink, restful check. The check character is monospace-rendered. */
.warmup__stage--done {
  color: var(--ink);
}
.warmup__stage--done .warmup__stage-marker {
  color: var(--accent);
  opacity: 0.7;
}

/* Active: full ink + a subtle pulsing cursor on the marker. The pulse
   is rate-limited so it never feels frantic — it's a heartbeat, not a
   spinner. */
.warmup__stage--active {
  color: var(--ink);
}
.warmup__stage--active .warmup__stage-marker {
  color: var(--accent);
  animation: pulse-marker 1.4s ease-in-out infinite;
}
.warmup__stage--active .warmup__stage-detail {
  color: var(--ink-dim);
}

.warmup__note {
  margin: 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--ink-dim);
  letter-spacing: 0.02em;
}

.warmup__note-em {
  color: var(--ink);
  font-weight: 500;
  white-space: nowrap;
}

/* Light-mode adaptation — the existing chat panel supports light
   backgrounds; the warmup surface inverts cleanly. */
@media (prefers-color-scheme: light) {
  .warmup {
    --line: rgba(0, 0, 0, 0.10);
    --line-strong: rgba(0, 0, 0, 0.18);
    --ink: rgba(0, 0, 0, 0.88);
    --ink-dim: rgba(0, 0, 0, 0.55);
    --ink-mute: rgba(0, 0, 0, 0.36);
    --accent: #b3801a;
    --accent-glow: rgba(179, 128, 26, 0.15);
  }
  .warmup__frame {
    background:
      repeating-linear-gradient(
        0deg,
        transparent 0,
        transparent 3px,
        rgba(0, 0, 0, 0.02) 3px,
        rgba(0, 0, 0, 0.02) 4px
      );
  }
}

/* Reduced-motion users get a static surface. The pulse and entry
   animation are decorative; the timer continues to update. */
@media (prefers-reduced-motion: reduce) {
  .warmup {
    animation: none;
  }
  .warmup__label-dot,
  .warmup__stage--active .warmup__stage-marker {
    animation: none;
  }
}

@keyframes warmup-enter {
  from {
    opacity: 0;
    transform: translateY(4px);
    filter: blur(0.5px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
    filter: blur(0);
  }
}

@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 0 var(--accent-glow); }
  50%      { box-shadow: 0 0 0 4px transparent; }
}

@keyframes pulse-marker {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0.45; }
}
</style>
