# Jarvis — Implementation Spec Index

> **Master tracking file.** Every step links here. Check off when DoD is met.

---

## References

- [overview.md](overview.md) — Project overview, architecture, and feature map
- [CODING-GUIDELINES.md](CODING-GUIDELINES.md) — Coding rules (Python + Nuxt)

---

## Definition of Done (Global)

Every step is considered **done** only when ALL of the following are true:

1. All files listed in the step spec are created/modified
2. All tests pass (`pytest` for backend, `vitest` for frontend)
3. No lint errors
4. All acceptance criteria from the step spec are checked off
5. Code committed with descriptive message
6. This index updated with ✅

---

> Each phase below landed as a feature documented under [docs/features/](features/); see [overview.md](overview.md) for the full feature map.

## Phase 1 — System Skeleton

- [x] Step 01 — Backend Init (FastAPI)
- [x] Step 02 — Frontend Init (Nuxt 3)
- [x] Step 03 — Onboarding + Workspace Creation

## Phase 2 — Local Memory

- [x] Step 04 — Memory Service + SQLite Index

## Phase 3 — Claude API

- [x] Step 05 — Claude API + Streaming + Tools

## Phase 4 — Voice

- [x] Step 06 — Voice Input/Output + States

## Phase 5 — Planning & Operational Memory

- [x] Step 07 — Planning Tools + Session Persistence

## Phase 6 — Knowledge Graph

- [x] Step 08 — Knowledge Graph + Retrieval

## Phase 7 — Specialists

- [x] Step 09 — Specialist System + UI Wizard

## Phase 8 — Polish

- [x] Step 10 — Polish, Obsidian, Caching, Ingest

---

## Progress Log

| Date | Step | Status | Commit |
|------|------|--------|--------|
| 2026-04-12 | Step 01 | ✅ Done | `feat: step-01 backend init` |
| 2025-07-15 | Step 02 | ✅ Done | `feat: step-02 frontend init (nuxt)` |
| 2025-07-15 | Step 03 | ✅ Done | `feat: step-03 onboarding + workspace` |
| 2025-07-15 | Step 04 | ✅ Done | `feat: step-04 memory service + sqlite index` |
| 2026-04-12 | Step 05 | ✅ Done | `feat: step-05 claude api + streaming + tools` |
| 2026-04-12 | Step 06 | ✅ Done | `feat: step-06 voice input/output` |
| 2026-04-12 | Step 07 | ✅ Done | `feat: step-07 planning tools + session persistence` |
| 2026-04-12 | Step 08 | ✅ Done | `feat: step-08 knowledge graph` |
| 2026-04-12 | Step 09 | ✅ Done | `feat: step-09 specialist system` |
| 2026-04-12 | Step 10 | ✅ Done | `feat: step-10 polish + ingest + settings` |
