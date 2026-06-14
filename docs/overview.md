---
title: Project Overview
last_reviewed: 2026-05-01
---

## What this project is

DeepBind (codename: Jarvis) is a local-first personal memory, planning, and knowledge system shipped as a notarized macOS desktop app. It provides a Tauri-hosted web interface to a local Markdown knowledge base, powered by an Ollama-hosted local LLM that runs in-process on the user's machine. All user data stays on disk as Markdown files — SQLite and graph layers are derived indexes that can be rebuilt from scratch.

Per [ADR 002](architecture/decisions/002-pure-local-product-shape.md) and [ADR 015](architecture/decisions/015-single-target-local-only-stack.md), the v1 product is **pure-local with zero outbound calls by default**: no cloud-provider SDKs in the binary, no LiteLLM, no API-key UI, no cloud-SKU build target. The audit signal is structural — an auditor running `find DeepBind.app -name '*anthropic*' -o -name '*openai*'` gets empty output.

## Architecture

```
┌─────────────────────────── DeepBind.app ───────────────────────────┐
│                                                                       │
│   Tauri shell (Rust)                                                  │
│     │                                                                 │
│     ├─ webview (Nuxt 4 SPA, served from .output/public)               │
│     │     │                                                           │
│     │     └── HTTP/WS ──▶ jarvis-sidecar (PyInstaller frozen)         │
│     │                       │                                         │
│     │                       ├── HTTP ──▶ ollama serve (bundled)       │
│     │                       │                                         │
│     │                       ├── Markdown files (canonical store)      │
│     │                       ├── SQLite (FTS5 index/cache, derived)    │
│     │                       └── graph.json (derived knowledge graph)  │
│     │                                                                 │
│     └─ supervises both sidecars; clean teardown on Cmd+Q              │
└───────────────────────────────────────────────────────────────────────┘
```

**Frontend** — Nuxt 4 SPA with Vue 3 composables. No SSR. Communicates with the backend via REST (CRUD) and a single WebSocket (chat streaming).

**Backend** — FastAPI sidecar (Python 3.12+) frozen with PyInstaller. Orchestrates retrieval, chat dispatch via the [`OllamaDispatcher`](../backend/services/ollama_dispatcher.py), tool execution, and file I/O. The chat endpoint streams the local model's response over WebSocket, executes tool calls server-side, and loops back for multi-turn tool use (up to 5 rounds).

**Inference** — Ollama 0.18.0 runtime bundled inside the `.app` (under `Contents/Resources/ollama-runtime/`). The Tauri shell spawns it on a private port (`:11435`) on app launch, separate from any user-installed Ollama on `:11434`. Per [ADR 005](architecture/decisions/005-hardware-tiered-model-stack-and-first-run-policy.md), the first-run orchestrator picks a hardware-tiered primary chat model and pulls it on first launch.

**Data flow** — User message → hybrid retrieval (FTS5 + graph neighbors) → context assembly → `OllamaDispatcher` → streamed response + optional tool calls (write notes, create plans, query graph) → results saved to Markdown files → SQLite re-indexed.

## Key technologies

| Layer | Technology |
|-------|-----------|
| Desktop shell | Tauri 2.x (Rust) — signed + notarized for macOS arm64 |
| Frontend framework | Nuxt 4 / Vue 3.5 / TypeScript (strict) |
| Frontend build | Vite, static `nuxt generate` for bundling |
| Backend framework | FastAPI (Python 3.12+), packaged via PyInstaller |
| Inference runtime | Ollama 0.18.0 (bundled), official `ollama` Python client (Apache-2.0) |
| Default chat model | Hardware-tiered: `qwen3:8b` (Tier A) / `qwen3:30b-a3b-instruct-2507-q4_K_M` (Tier B) / `gpt-oss:120b` (Tier C). User-pinnable. |
| Embeddings | fastembed ONNX (multilingual MiniLM, bundled — ~240 MB) |
| Database | SQLite via aiosqlite (FTS5 for search) |
| Graph | JSON file, visualized with force-graph + Three.js |
| NER | spaCy with bundled `en_core_web_sm` + `pl_core_news_sm` lang packs |
| Markdown | marked + DOMPurify for rendering |
| Testing | Vitest (frontend), pytest (backend) |

## Feature documentation

All feature docs are in [docs/features/](features/) and concepts are in [docs/concepts/](concepts/). The registry at [docs/.registry.json](.registry.json) maps source files to their documentation.

| Feature | Doc |
|---------|-----|
| App Shell & Navigation | [app-shell.md](features/app-shell.md) |
| Chat & LLM Dispatch | [chat.md](features/chat.md) |
| Local Models (Ollama dispatch + first-run + downgrade ladder) | [local-models.md](features/local-models.md) |
| Desktop Shell (Tauri + bundled sidecars) | [desktop-shell-graduation.md](features/desktop-shell-graduation.md) |
| Knowledge Graph | [knowledge-graph.md](features/knowledge-graph.md) |
| Memory System | [memory.md](features/memory.md) |
| Planning Service | [planning.md](features/planning.md) |
| Preferences & Settings | [preferences-settings.md](features/preferences-settings.md) |
| Hybrid Retrieval Pipeline | [retrieval.md](features/retrieval.md) |
| Session Management | [sessions.md](features/sessions.md) |
| Specialist System | [specialists.md](features/specialists.md) |
| Workspace & Onboarding | [workspace-onboarding.md](features/workspace-onboarding.md) |
| Database Layer (concept) | [database.md](concepts/database.md) |

## Runbooks

| Topic | Doc |
|---|---|
| macOS release build (signed + notarized) | [runbooks/release-build-macos.md](runbooks/release-build-macos.md) |
| G4b6 cold-launch verification on the notarized bundle | [runbooks/g4b6-cold-launch-verification.md](runbooks/g4b6-cold-launch-verification.md) |

## Architecture decisions

The full ADR history is at [docs/architecture/decisions/](architecture/decisions/). Core decisions for v1:

- **[ADR 002](architecture/decisions/002-pure-local-product-shape.md)** — pure-local product shape (zero outbound calls by default)
- **[ADR 003](architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md)** — Tauri + sidecars desktop distribution
- **[ADR 005](architecture/decisions/005-hardware-tiered-model-stack-and-first-run-policy.md)** — hardware-tiered model stack + first-run policy
- **[ADR 015](architecture/decisions/015-single-target-local-only-stack.md)** — single-target local-only stack (supersedes ADR 014)
