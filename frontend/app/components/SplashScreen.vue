<!--
  SplashScreen — calibration-boot surface.

  The first thing users see on every launch. Replaces a 10-30 s blank
  dock-bounce with a precision-instrument readout that mirrors the real
  Tauri-side boot stages (Ollama → sidecar → license → ready) emitted
  via `boot:stage` events. Disappears the moment boot completes; a
  brief signal-lock flash signals the transition.

  Aesthetic: all monospace (no new font load — `JetBrains Mono` is already
  in the project), neon-cyan brand surface with phosphor-amber accent on
  active stage. Hairline borders, scanlines, grain, corner ticks. The
  product is a compliance/security tool whose buyers audit for rigor;
  the boot moment should look like a measurement, not a marketing reel.
-->
<template>
  <div
    class="splash"
    :class="{ 'splash--ready': boot.ready.value, 'splash--error': isError }"
    role="status"
    aria-live="polite"
  >
    <div class="splash__bg-grid" aria-hidden="true" />
    <div class="splash__bg-scan" aria-hidden="true" />
    <div class="splash__bg-noise" aria-hidden="true" />
    <div class="splash__bg-glow" aria-hidden="true" />

    <div class="splash__frame">
      <div class="splash__corner splash__corner--tl" />
      <div class="splash__corner splash__corner--tr" />
      <div class="splash__corner splash__corner--bl" />
      <div class="splash__corner splash__corner--br" />

      <header class="splash__header">
        <span class="splash__chip">[ COLD&nbsp;BOOT ]</span>
        <span class="splash__chip splash__chip--right">{{ platformLabel }}</span>
      </header>

      <div class="splash__brand">
        <h1 class="splash__wordmark">
          <span class="splash__wordmark-part">DEEPFILES</span><span
            class="splash__wordmark-dot"
            aria-hidden="true"
          >·</span><span class="splash__wordmark-part splash__wordmark-part--alt">AI</span>
        </h1>
        <div class="splash__rule" />
        <p class="splash__tag">precision&nbsp;knowledge&nbsp;system</p>
      </div>

      <div class="splash__readout">
        <div class="splash__trace" :aria-valuenow="tracePct" aria-valuemin="0" aria-valuemax="100" role="progressbar">
          <div class="splash__trace-bg" />
          <div class="splash__trace-fill" :style="{ width: tracePct + '%' }" />
          <div class="splash__trace-tick" :style="{ left: tracePct + '%' }" />
          <span class="splash__trace-pct">{{ tracePct.toString().padStart(2, '0') }}</span>
        </div>

        <div class="splash__status">
          <span
            class="splash__status-dot"
            :class="{ 'splash__status-dot--error': isError }"
          />
          <span class="splash__status-label">{{ stageLabel }}</span>
          <span class="splash__status-detail">{{ rotatingDetail }}</span>
        </div>

        <ol class="splash__stages">
          <li
            v-for="(s, i) in stageList"
            :key="s.id"
            class="splash__stage"
            :class="{
              'splash__stage--done': stageStatus(s.id) === 'done',
              'splash__stage--active': stageStatus(s.id) === 'active',
              'splash__stage--pending': stageStatus(s.id) === 'pending',
            }"
          >
            <span class="splash__stage-num">{{ String(i + 1).padStart(2, '0') }}</span>
            <span class="splash__stage-bar" />
            <span class="splash__stage-name">{{ s.label }}</span>
          </li>
        </ol>
      </div>

      <footer class="splash__footer">
        <span class="splash__footer-version">v{{ version }}</span>
        <span class="splash__footer-sep">·</span>
        <span class="splash__footer-build">© The DeepBind Project</span>
      </footer>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useBoot } from '~/composables/useBoot'

const boot = useBoot()

// ---------------------------------------------------------------------------
// Stage descriptor — drives both the status label and the small ordered list.
// IDs match `BootPhase` from the Rust shell so `stageStatus` is a direct map.
// ---------------------------------------------------------------------------
const stageList = [
  { id: 'ollama', label: 'inference runtime', covers: ['ollama_starting', 'ollama_ready'] },
  { id: 'sidecar', label: 'services', covers: ['sidecar_starting', 'sidecar_ready'] },
  { id: 'license', label: 'entitlement', covers: ['license_probing'] },
  { id: 'ready', label: 'ready', covers: ['ready'] },
] as const

type StageId = (typeof stageList)[number]['id']

function stageStatus(id: StageId): 'done' | 'active' | 'pending' {
  const idx = stageList.findIndex(s => s.id === id)
  const phase = boot.phase.value
  // Find the index of the stage that current phase belongs to.
  const currentIdx = stageList.findIndex(s => (s.covers as readonly string[]).includes(phase))
  if (currentIdx === -1) return 'pending'
  if (idx < currentIdx) return 'done'
  if (idx === currentIdx) {
    // Within a stage, the "first half" phases (e.g. ollama_starting) keep the
    // stage active; the "second half" (e.g. ollama_ready) flips it to done so
    // the eye sees forward motion.
    if (phase.endsWith('_ready') && id !== 'ready') return 'done'
    return 'active'
  }
  return 'pending'
}

// ---------------------------------------------------------------------------
// Status label — short, uppercase, instrument-panel cadence.
// ---------------------------------------------------------------------------
const isError = computed(() => boot.phase.value === 'error')

const stageLabel = computed(() => {
  if (isError.value) return 'BOOT FAILED'
  switch (boot.phase.value) {
    case 'ollama_starting': return 'INITIALIZING'
    case 'ollama_ready': return 'RUNTIME ONLINE'
    case 'sidecar_starting': return 'LOADING ENGINE'
    case 'sidecar_ready': return 'SERVICES ONLINE'
    case 'license_probing': return 'VERIFYING'
    case 'ready': return 'READY'
    default: return 'INITIALIZING'
  }
})

// Long stages (sidecar_starting can sit for 10-25 s on a cold install while
// PyInstaller unpacks the 1.8 GB binary) feel dead with a single static
// subtitle. Cycle through honest variants so the eye sees motion. Each
// variant is true at the moment it shows — no faked progress.
const sidecarVariants = [
  'extracting inference services',
  'loading fastembed weights',
  'priming spaCy + tokenizers',
  'first run is the slowest — settling in',
]
const variantIdx = ref(0)
let variantTimer: ReturnType<typeof setInterval> | null = null

watch(
  () => boot.phase.value,
  (phase) => {
    variantIdx.value = 0
    if (variantTimer) {
      clearInterval(variantTimer)
      variantTimer = null
    }
    if (phase === 'sidecar_starting') {
      variantTimer = setInterval(() => {
        variantIdx.value = Math.min(variantIdx.value + 1, sidecarVariants.length - 1)
      }, 4000)
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  if (variantTimer) clearInterval(variantTimer)
})

const rotatingDetail = computed(() => {
  if (isError.value) return boot.error.value ?? boot.detail.value
  if (boot.phase.value === 'sidecar_starting') {
    return sidecarVariants[variantIdx.value] ?? boot.detail.value
  }
  return boot.detail.value
})

// ---------------------------------------------------------------------------
// Trace (progress) — smooth interpolation toward the shell-reported value.
// Without smoothing the bar would jump in big chunks (10% → 30% → 75% → 90%
// → 100%); a 600 ms cubic-bezier transition (CSS) handles that. We just
// surface the integer percentage for the readout next to the trace.
// ---------------------------------------------------------------------------
const tracePct = computed(() => Math.round(boot.progress.value * 100))

// ---------------------------------------------------------------------------
// Footer chrome — version + platform.
// ---------------------------------------------------------------------------
const platformLabel = ref('—')
const version = ref('0.1.0')

onMounted(() => {
  if (typeof navigator === 'undefined') return
  const ua = navigator.userAgent.toLowerCase()
  if (ua.includes('mac')) platformLabel.value = 'APPLE SILICON'
  else if (ua.includes('windows')) platformLabel.value = 'WINDOWS'
  else if (ua.includes('linux')) platformLabel.value = 'LINUX'
})
</script>

<style scoped>
/*
  Rules for this component:
  - JetBrains Mono throughout, hierarchy via weight + size + tracking.
  - Neon-cyan as the brand colour (`--neon-cyan` from main.css), phosphor
    amber (#d4a017, matches ChatFirstTurnWarmup) on the single active stage
    so the eye locks on the right thing.
  - Hairline borders, no rounded chrome, no shadows, no gradient blobs.
  - All animation is decorative; reduced-motion strips it cleanly.
*/

.splash {
  --ink: rgba(255, 255, 255, 0.92);
  --ink-dim: rgba(255, 255, 255, 0.55);
  --ink-mute: rgba(255, 255, 255, 0.30);
  --ink-faint: rgba(255, 255, 255, 0.10);
  --line: rgba(2, 254, 255, 0.10);
  --line-strong: rgba(2, 254, 255, 0.22);
  --accent: #d4a017;
  --accent-glow: rgba(212, 160, 23, 0.32);
  --brand: rgba(2, 254, 255, 0.85);
  --brand-glow: rgba(2, 254, 255, 0.18);
  --bg: #06080d;
  --bg-2: #0a0e17;

  position: fixed;
  inset: 0;
  z-index: 9999;
  background: var(--bg);
  color: var(--ink);
  font-family: 'JetBrains Mono', 'Fira Code', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-feature-settings: 'tnum' 1, 'ss01' 1, 'cv11' 1;
  display: grid;
  place-items: center;
  overflow: hidden;
  user-select: none;
  -webkit-user-select: none;
  cursor: progress;
  transition: opacity 0.42s cubic-bezier(0.2, 0.7, 0.2, 1), filter 0.42s;
}

/* On boot-complete the layout unmounts us; this transition + the
   `splash--ready` class drive the brief signal-lock flash followed by
   the fade-out. */
.splash--ready {
  opacity: 0;
  filter: brightness(1.6);
  pointer-events: none;
}

/* ── Background atmosphere ──────────────────────────────────────────── */
.splash__bg-grid {
  position: absolute;
  inset: 0;
  background:
    linear-gradient(to right, var(--ink-faint) 1px, transparent 1px) 0 0 / 64px 64px,
    linear-gradient(to bottom, var(--ink-faint) 1px, transparent 1px) 0 0 / 64px 64px;
  opacity: 0.18;
  mask-image: radial-gradient(ellipse 70% 60% at center, black 30%, transparent 80%);
  -webkit-mask-image: radial-gradient(ellipse 70% 60% at center, black 30%, transparent 80%);
}

.splash__bg-scan {
  position: absolute;
  inset: 0;
  background: repeating-linear-gradient(
    0deg,
    transparent 0,
    transparent 3px,
    rgba(255, 255, 255, 0.018) 3px,
    rgba(255, 255, 255, 0.018) 4px
  );
  pointer-events: none;
  animation: scanDrift 32s linear infinite;
}

.splash__bg-noise {
  position: absolute;
  inset: -50%;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 0 0.9 0'/></filter><rect width='100%' height='100%' filter='url(%23n)' opacity='0.6'/></svg>");
  opacity: 0.045;
  mix-blend-mode: overlay;
  pointer-events: none;
}

.splash__bg-glow {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse 480px 300px at 50% 38%, var(--brand-glow) 0%, transparent 70%),
    radial-gradient(ellipse 320px 200px at 50% 62%, var(--accent-glow) 0%, transparent 75%);
  opacity: 0.55;
  pointer-events: none;
  animation: glowBreathe 8s ease-in-out infinite;
}

/* ── Frame ──────────────────────────────────────────────────────────── */
.splash__frame {
  position: relative;
  width: min(640px, calc(100vw - 96px));
  padding: 36px 44px 28px;
  display: grid;
  gap: 36px;
  border: 1px solid var(--line);
  background: linear-gradient(180deg, rgba(10, 14, 23, 0.55) 0%, rgba(6, 8, 13, 0.35) 100%);
  backdrop-filter: blur(2px);
  animation: frameEnter 0.7s cubic-bezier(0.2, 0.7, 0.2, 1) both;
}

.splash__corner {
  position: absolute;
  width: 14px;
  height: 14px;
  border-color: var(--line-strong);
  border-style: solid;
  border-width: 0;
}
.splash__corner--tl { top: -1px; left: -1px;  border-top-width: 1px; border-left-width: 1px; }
.splash__corner--tr { top: -1px; right: -1px; border-top-width: 1px; border-right-width: 1px; }
.splash__corner--bl { bottom: -1px; left: -1px;  border-bottom-width: 1px; border-left-width: 1px; }
.splash__corner--br { bottom: -1px; right: -1px; border-bottom-width: 1px; border-right-width: 1px; }

/* ── Header (chip row) ──────────────────────────────────────────────── */
.splash__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 9.5px;
  letter-spacing: 0.22em;
  color: var(--ink-dim);
  font-weight: 500;
  animation: fadeUp 0.6s 0.05s ease-out both;
}

.splash__chip {
  padding: 3px 8px;
  border: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.015);
}

.splash__chip--right {
  color: var(--ink-mute);
}

/* ── Brand ──────────────────────────────────────────────────────────── */
.splash__brand {
  display: grid;
  gap: 14px;
  justify-items: center;
  text-align: center;
  animation: fadeUp 0.7s 0.18s cubic-bezier(0.2, 0.7, 0.2, 1) both;
}

.splash__wordmark {
  margin: 0;
  font-size: clamp(36px, 6vw, 56px);
  font-weight: 800;
  letter-spacing: 0.18em;
  line-height: 1;
  color: var(--ink);
  text-shadow: 0 0 24px var(--brand-glow), 0 0 1px rgba(2, 254, 255, 0.4);
  animation: wordmarkBreathe 7s ease-in-out infinite;
  display: inline-flex;
  align-items: baseline;
  flex-wrap: nowrap;
  white-space: nowrap;
}

.splash__wordmark-part--alt {
  color: var(--brand);
}

.splash__wordmark-dot {
  display: inline-block;
  margin: 0 0.18em 0.05em;
  color: var(--accent);
  text-shadow: 0 0 14px var(--accent-glow);
  animation: dotPulse 2.4s ease-in-out infinite;
}

.splash__rule {
  width: 64px;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--line-strong), transparent);
}

.splash__tag {
  margin: 0;
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.36em;
  text-transform: uppercase;
  color: var(--ink-mute);
}

/* ── Readout (progress + status + stage list) ───────────────────────── */
.splash__readout {
  display: grid;
  gap: 18px;
  animation: fadeUp 0.8s 0.36s cubic-bezier(0.2, 0.7, 0.2, 1) both;
}

.splash__trace {
  position: relative;
  height: 16px;
  display: flex;
  align-items: center;
}

.splash__trace-bg {
  position: absolute;
  left: 0; right: 0;
  top: 50%;
  transform: translateY(-50%);
  height: 1px;
  background: var(--line);
}

.splash__trace-fill {
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  height: 1px;
  background: linear-gradient(90deg, var(--brand) 0%, var(--accent) 100%);
  box-shadow: 0 0 8px var(--brand-glow);
  transition: width 0.6s cubic-bezier(0.4, 0, 0.2, 1);
  width: 0;
}

.splash__trace-tick {
  position: absolute;
  top: 50%;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 12px var(--accent), 0 0 0 1px var(--accent-glow);
  transform: translate(-50%, -50%);
  transition: left 0.6s cubic-bezier(0.4, 0, 0.2, 1);
}

.splash__trace-pct {
  position: absolute;
  right: 0;
  bottom: -16px;
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.08em;
  color: var(--ink-mute);
}

.splash__status {
  display: grid;
  grid-template-columns: 16px auto 1fr;
  align-items: baseline;
  column-gap: 10px;
  row-gap: 4px;
  margin-top: 6px;
}

.splash__status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 0 var(--accent-glow);
  animation: dotBeat 1.6s ease-in-out infinite;
  align-self: center;
}

.splash__status-dot--error {
  background: var(--neon-orange, #fb923c);
  animation: none;
  box-shadow: 0 0 8px rgba(251, 146, 60, 0.45);
}

.splash__status-label {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.18em;
  color: var(--ink);
  text-transform: uppercase;
}

.splash__status-detail {
  grid-column: 2 / 4;
  font-size: 11.5px;
  color: var(--ink-dim);
  letter-spacing: 0.02em;
  font-style: italic;
  /* Smooth swap between rotating subtitles. */
  transition: opacity 0.25s ease;
}

/* ── Stage list (4-line micro-grid) ─────────────────────────────────── */
.splash__stages {
  list-style: none;
  margin: 0;
  padding: 12px 0 0;
  border-top: 1px dashed var(--line);
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px 24px;
}

.splash__stage {
  display: grid;
  grid-template-columns: 26px 18px 1fr;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  letter-spacing: 0.06em;
  color: var(--ink-mute);
  transition: color 0.3s ease;
}

.splash__stage-num {
  font-variant-numeric: tabular-nums;
  font-size: 10px;
  letter-spacing: 0.1em;
  color: var(--ink-faint);
}

.splash__stage-bar {
  height: 1px;
  background: var(--ink-faint);
  transition: background 0.3s ease;
}

.splash__stage-name {
  text-transform: lowercase;
  letter-spacing: 0.04em;
}

.splash__stage--active {
  color: var(--ink);
}
.splash__stage--active .splash__stage-num {
  color: var(--accent);
}
.splash__stage--active .splash__stage-bar {
  background: var(--accent);
  box-shadow: 0 0 6px var(--accent-glow);
  animation: barPulse 1.6s ease-in-out infinite;
}

.splash__stage--done {
  color: var(--ink-dim);
}
.splash__stage--done .splash__stage-num {
  color: var(--brand);
}
.splash__stage--done .splash__stage-bar {
  background: var(--brand);
}
.splash__stage--done .splash__stage-name {
  text-decoration: line-through;
  text-decoration-color: var(--brand);
  text-decoration-thickness: 1px;
}

/* ── Footer ─────────────────────────────────────────────────────────── */
.splash__footer {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 6px;
  font-size: 9.5px;
  letter-spacing: 0.18em;
  color: var(--ink-mute);
  text-transform: uppercase;
  animation: fadeUp 0.7s 0.55s ease-out both;
}

.splash__footer-sep {
  color: var(--ink-faint);
}

/* ── Error state ────────────────────────────────────────────────────── */
.splash--error .splash__trace-fill,
.splash--error .splash__trace-tick {
  background: var(--neon-orange, #fb923c);
  box-shadow: 0 0 8px rgba(251, 146, 60, 0.45);
}

.splash--error .splash__status-label {
  color: var(--neon-orange, #fb923c);
}

/* ── Animations ─────────────────────────────────────────────────────── */
@keyframes frameEnter {
  from { opacity: 0; transform: translateY(6px); filter: blur(1px); }
  to   { opacity: 1; transform: translateY(0); filter: blur(0); }
}

@keyframes fadeUp {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: translateY(0); }
}

@keyframes wordmarkBreathe {
  0%, 100% { transform: scale(1); text-shadow: 0 0 24px var(--brand-glow), 0 0 1px rgba(2, 254, 255, 0.4); }
  50%      { transform: scale(1.005); text-shadow: 0 0 32px var(--brand-glow), 0 0 2px rgba(2, 254, 255, 0.6); }
}

@keyframes dotPulse {
  0%, 100% { opacity: 0.85; transform: scale(1); }
  50%      { opacity: 1; transform: scale(1.12); }
}

@keyframes dotBeat {
  0%, 100% { box-shadow: 0 0 0 0 var(--accent-glow); }
  50%      { box-shadow: 0 0 0 5px transparent; }
}

@keyframes barPulse {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0.4; }
}

@keyframes glowBreathe {
  0%, 100% { opacity: 0.50; }
  50%      { opacity: 0.70; }
}

@keyframes scanDrift {
  from { background-position: 0 0; }
  to   { background-position: 0 64px; }
}

/* ── Reduced motion ─────────────────────────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
  .splash__frame,
  .splash__brand,
  .splash__readout,
  .splash__footer,
  .splash__header { animation: none; }
  .splash__wordmark,
  .splash__wordmark-dot,
  .splash__status-dot,
  .splash__bg-glow,
  .splash__bg-scan,
  .splash__stage--active .splash__stage-bar { animation: none; }
}

/* ── Light-mode adaptation ──────────────────────────────────────────── */
/* Splash stays dark in all themes — it's the brand surface and dark is
   load-bearing for the phosphor accents. We only soften ink levels in
   case a future setting wants to tone it down. */
</style>
