# Jira Strategist

> Built-in specialist for analysing Jira tasks, blockers, sprint risk
> and cross-source connections — now with dedicated Jira tools and duel presets.

## Summary

**Jira Strategist** is the first built-in specialist shipped with
Jarvis.  It is automatically seeded into the `agents/` directory on
workspace creation and on startup (for existing workspaces).

It is designed to help users reason about their Jira projects by
grounding answers in the indexed truth — never inventing issue keys.

As of step 22g, the specialist has 6 dedicated Jira tools for
querying SQLite-indexed issues, a set of pre-built duel presets for
strategic debates, and duel verdict → graph edge emission.

## How It Works

### Seeding

`seed_builtin_specialists()` in `specialist_service.py` checks the
`agents/` directory and creates `jira-strategist.json` if it does not
already exist.  The function is called:

1. During `create_workspace()` in `workspace_service.py`
2. During app startup in `main.py` (lifespan hook)

It never overwrites a user-edited file — if the file exists, it is
skipped.  The `builtin: true` flag in the JSON marks it as system-seeded.

### Profile

| Field | Value |
|-------|-------|
| **ID** | `jira-strategist` |
| **Name** | Jira Strategist |
| **Icon** | 🎯 |
| **Tone** | direct, operational |
| **Length** | short, bulleted when listing issues |
| **Citation** | always include issue keys in brackets |

### Rules

1. Never invent issue keys — only cite keys that appear in context.
2. When listing blockers, use hard edges first, soft edges flagged as *(likely)*.
3. When a task is unclear, say so explicitly and cite the enrichment ambiguity level.

### Knowledge scope

Sources: `memory/jira/**`, `memory/decisions/**`, `memory/projects/**`, `memory/people/**`

### Tool access

The specialist has access to 10 tools:

| Tool | Purpose |
|------|---------|
| `jira_list_issues` | Faceted search: status, assignee, project, priority, sprint, sprint_state |
| `jira_describe_issue` | Full detail: sprints, hard/soft links, enrichment, related notes |
| `jira_blockers_of` | BFS blocker chain (max depth 3) + likely blockers from graph |
| `jira_depends_on` | Mirror of blockers — who depends on this issue |
| `jira_sprint_risk` | Sprint analysis: risk/ambiguity, blocking chains, bottleneck assignees |
| `jira_cluster_by_topic` | Groups issues by business_area from enrichment |
| `search_notes` | Search memory |
| `read_note` | Read a note |
| `write_note` | Write to memory |
| `query_graph` | Query the knowledge graph |

All 6 Jira tools are deterministic SQLite queries — no LLM calls, no token cost.

## Tools Package

The tools system was refactored from a single `tools.py` into a package:

| File | Role |
|------|------|
| `tools/__init__.py` | Re-exports `TOOLS`, `ToolNotFoundError`, `execute_tool` |
| `tools/definitions.py` | Tool JSON schemas for Claude API (11 core + 6 Jira) |
| `tools/executor.py` | `execute_tool()` dispatch + core handler routing |
| `tools/jira_tools.py` | 6 Jira tool implementations |

### Jira tool details

- **`_jira_db()`** — opens sync `sqlite3` connection to `{workspace}/app/jarvis.db`
- **`_bfs_links()`** — BFS over `issue_links` table, returns `(direct, transitive)` lists, `max_depth=3`
- All tools attach enrichment data (risk, ambiguity, business_area) when available
- `jira_describe_issue` also returns soft links from graph and related notes/decisions

## Duel presets — removed in ADR 015

The four duel presets (delivery-vs-risk, product-vs-tech, pragmatist-vs-refactorer, growth-vs-stability) and the underlying duel/council feature were removed in [ADR 015](../architecture/decisions/015-single-target-local-only-stack.md). Loading two local models simultaneously violates the [G4b4 memory pressure mechanism](../architecture/decisions/005-hardware-tiered-model-stack-and-first-run-policy.md), and a "second opinion" between two Qwen3 variants of the same family is no longer the multi-cloud-provider differentiation duel was originally built for. The feature is recoverable from the pre-deletion git history if a future product reason surfaces.

The four preset specialists themselves can be invoked individually via the regular specialist UI; only the side-by-side duel comparison is gone.

## Key Files

| File | Role |
|------|------|
| [specialist_service.py](../../backend/services/specialist_service.py) | `_BUILTIN_SPECIALISTS` list + `seed_builtin_specialists()` |
| [tools/definitions.py](../../backend/services/tools/definitions.py) | Tool JSON schemas (CORE_TOOLS + JIRA_TOOLS) |
| [tools/executor.py](../../backend/services/tools/executor.py) | Tool dispatch |
| [tools/jira_tools.py](../../backend/services/tools/jira_tools.py) | 6 Jira tool implementations |
| [test_jira_strategist.py](../../backend/tests/test_jira_strategist.py) | 29 tests |
