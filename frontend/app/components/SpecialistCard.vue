<template>
  <div
    class="spec-card"
    :class="{
      'spec-card--active': active,
      'spec-card--expanded': expanded,
    }"
  >
    <!-- Main row -->
    <div class="spec-card__main" @click="$emit('toggle-expand', specialist.id)">
      <div class="spec-card__icon-wrap">
        <span class="spec-card__icon">{{ specialist.icon }}</span>
        <span v-if="active" class="spec-card__active-dot" />
      </div>

      <div class="spec-card__body">
        <div class="spec-card__name-row">
          <h3 class="spec-card__name">{{ specialist.name }}</h3>
          <span v-if="active" class="spec-card__active-tag">Active</span>
        </div>
        <div class="spec-card__stats">
          <span class="spec-card__stat">
            <Icon name="ph:file-text" class="spec-card__stat-icon" />
            {{ specialist.file_count || 0 }} files
          </span>
          <span class="spec-card__stat-dot">&middot;</span>
          <span class="spec-card__stat">{{ specialist.source_count }} sources</span>
          <span class="spec-card__stat-dot">&middot;</span>
          <span class="spec-card__stat">{{ specialist.rule_count }} rules</span>
          <template v-if="specialist.default_model">
            <span class="spec-card__stat-dot">&middot;</span>
            <span class="spec-card__stat spec-card__stat--model">
              <Icon name="ph:hard-drives" class="spec-card__stat-icon" />
              {{ modelLabel(specialist.default_model) }}
            </span>
          </template>
        </div>
      </div>

      <div class="spec-card__actions" @click.stop>
        <button
          class="spec-card__btn spec-card__btn--activate"
          :class="{ 'spec-card__btn--activated': active }"
          @click="$emit('activate', specialist.id)"
          :title="active ? 'Deactivate' : 'Activate'"
        >
          <Icon
            :name="active ? 'ph:stop-circle-fill' : 'ph:play-fill'"
            class="icon--md"
          />
        </button>
        <button
          class="spec-card__btn spec-card__btn--edit"
          title="Edit specialist"
          @click="$emit('edit', specialist.id)"
        >
          <Icon name="ph:pencil-simple" class="icon--sm" />
        </button>
        <button
          class="spec-card__btn spec-card__btn--delete"
          title="Delete specialist"
          @click="$emit('delete', specialist.id)"
        >
          <Icon name="ph:trash" class="icon--sm" />
        </button>
        <button
          class="spec-card__btn spec-card__btn--expand"
          :class="{ 'spec-card__btn--expand-open': expanded }"
          @click="$emit('toggle-expand', specialist.id)"
          title="Toggle knowledge panel"
        >
          <Icon name="ph:caret-down" class="icon--sm" />
        </button>
      </div>
    </div>

    <!-- Expandable knowledge panel -->
    <Transition name="expand">
      <SpecialistKnowledgePanel
        v-if="expanded"
        :specialist-id="specialist.id"
      />
    </Transition>
  </div>
</template>

<script setup lang="ts">
import type { SpecialistSummary } from '~/types'

function modelLabel(dm?: { provider: string; model: string } | null): string {
  // ADR 015 — single dispatch target; specialist `default_model` is a
  // legacy carry-over for compatibility with old saved profiles. Strip
  // the `ollama_chat/` prefix when present, otherwise show the raw tag.
  if (!dm) return ''
  return dm.model.replace(/^ollama(?:_chat)?\//, '')
}

defineProps<{
  specialist: SpecialistSummary
  active?: boolean
  expanded?: boolean
}>()

defineEmits<{
  activate: [id: string]
  edit: [id: string]
  delete: [id: string]
  'toggle-expand': [id: string]
}>()
</script>

<style scoped>
.spec-card {
  border: 1px solid var(--border-default);
  border-radius: 12px;
  background: var(--bg-surface);
  transition: all 0.25s ease;
  overflow: hidden;
  position: relative;
  width: 100%;
}

/* Subtle scanline texture */
.spec-card::after {
  content: '';
  position: absolute;
  inset: 0;
  background: repeating-linear-gradient(
    0deg,
    transparent,
    transparent 3px,
    rgba(2, 254, 255, 0.008) 3px,
    rgba(2, 254, 255, 0.008) 4px
  );
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.3s;
}

.spec-card:hover::after,
.spec-card--expanded::after {
  opacity: 1;
}

.spec-card:hover {
  border-color: var(--border-strong);
  box-shadow: 0 2px 16px rgba(0, 0, 0, 0.2);
}

.spec-card--active {
  border-color: var(--neon-cyan-30);
  background:
    linear-gradient(135deg, var(--neon-cyan-08) 0%, transparent 60%),
    var(--bg-surface);
  box-shadow:
    0 0 20px var(--neon-cyan-08),
    inset 0 1px 0 var(--neon-cyan-15);
}

.spec-card--expanded {
  border-color: var(--neon-cyan-15);
}

/* --- Main row --- */
.spec-card__main {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: 1rem 1.25rem;
  cursor: pointer;
  position: relative;
  z-index: 1;
}

/* --- Icon --- */
.spec-card__icon-wrap {
  position: relative;
  width: 44px;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 12px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.spec-card--active .spec-card__icon-wrap {
  background: var(--neon-cyan-08);
  border-color: var(--neon-cyan-15);
}

.spec-card__icon {
  font-size: 1.4rem;
  line-height: 1;
}

.spec-card__active-dot {
  position: absolute;
  bottom: -1px;
  right: -1px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--neon-cyan);
  border: 2px solid var(--bg-surface);
  box-shadow: 0 0 6px var(--neon-cyan-60);
  animation: pulse-dot 2s ease-in-out infinite;
}

@keyframes pulse-dot {
  0%, 100% { box-shadow: 0 0 4px var(--neon-cyan-30); }
  50% { box-shadow: 0 0 10px var(--neon-cyan-60); }
}

/* --- Body --- */
.spec-card__body {
  flex: 1;
  min-width: 0;
}

.spec-card__name-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.spec-card__name {
  margin: 0;
  font-size: 0.92rem;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.spec-card__active-tag {
  font-size: 0.6rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  padding: 0.1rem 0.4rem;
  border-radius: 3px;
  background: var(--neon-cyan-15);
  color: var(--neon-cyan);
  border: 1px solid var(--neon-cyan-30);
  flex-shrink: 0;
}

.spec-card__stats {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  margin-top: 0.2rem;
}

.spec-card__stat {
  display: flex;
  align-items: center;
  gap: 0.2rem;
  font-size: 0.72rem;
  color: var(--text-muted);
}

.spec-card__stat-icon {
  font-size: 11px;
  color: var(--text-muted);
}

.spec-card__stat-dot {
  color: var(--text-muted);
  font-size: 0.65rem;
}

.spec-card__stat--model {
  color: var(--text-secondary);
}

/* --- Actions --- */
.spec-card__actions {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  flex-shrink: 0;
}

.spec-card__btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.2s;
}

.spec-card__btn:hover {
  background: var(--bg-hover);
  border-color: var(--border-default);
  color: var(--text-primary);
}

.spec-card__btn--activate:hover {
  border-color: var(--neon-cyan-30);
  color: var(--neon-cyan);
  background: var(--neon-cyan-08);
  box-shadow: 0 0 8px var(--neon-cyan-08);
}

.spec-card__btn--activated {
  border-color: var(--neon-cyan-30);
  color: var(--neon-cyan);
  background: var(--neon-cyan-08);
}

.spec-card__btn--edit:hover {
  border-color: rgba(234, 179, 8, 0.3);
  color: var(--neon-yellow);
  background: rgba(234, 179, 8, 0.08);
}

.spec-card__btn--delete:hover {
  border-color: rgba(239, 68, 68, 0.3);
  color: var(--neon-red);
  background: rgba(239, 68, 68, 0.08);
}

.spec-card__btn--expand {
  transition: all 0.2s, transform 0.3s ease;
}

.spec-card__btn--expand-open {
  transform: rotate(180deg);
}

/* --- Expand transition --- */
.expand-enter-active {
  transition: all 0.3s ease;
  overflow: hidden;
}
.expand-leave-active {
  transition: all 0.25s ease;
  overflow: hidden;
}
.expand-enter-from,
.expand-leave-to {
  opacity: 0;
  max-height: 0;
  padding-top: 0;
  padding-bottom: 0;
}
.expand-enter-to,
.expand-leave-from {
  opacity: 1;
  max-height: 500px;
}
</style>
