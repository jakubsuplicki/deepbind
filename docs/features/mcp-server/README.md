# MCP Server

Local Model Context Protocol server that lets any MCP-compatible AI client
(Claude Desktop, Cursor, VS Code Copilot, Continue, Zed, …) tap directly
into your Jarvis workspace — notes, conversations, Jira, knowledge graph.

- **Transport:** stdio only (zero-config, no port, no token)
- **Binary:** `jarvis-mcp` — standalone CLI on your `PATH` after backend install
- **Surface:** 22 read-only tools + 3 opt-in write tools (25 total)
- **Privacy:** runs locally, launched on demand by the client
- **Logs:** JSONL append-only at `app/logs/mcp/YYYY-MM-DD.jsonl`
- **Writes:** disabled unless `mcp.allow_writes: true` in `app/config.json`

## Quick start

1. Open **Settings → MCP** in the Jarvis UI.
2. Pick your client in the snippet switcher.
3. Copy the generated JSON (paths are filled in for your machine).
4. Paste into your client's config file (see [`clients/`](./clients/)).
5. Restart the client and ask: *"Use `jarvis_workspace_stats`."*

## Minimal config (all clients)

```json
{
  "mcpServers": {
    "jarvis": {
      "command": "jarvis-mcp",
      "args": ["--transport", "stdio"]
    }
  }
}
```

No `cwd`, no `PYTHONPATH`, no env required — the workspace is auto-discovered.
If `jarvis-mcp` is not on your `PATH`, the Settings snippet generator emits
an absolute-path fallback pointing at `backend/.venv/bin/jarvis-mcp`.

## Ready-to-paste per-client configs

See [`clients/`](./clients/) for:
- [`claude-desktop.json`](./clients/claude-desktop.json)
- [`cursor.json`](./clients/cursor.json)
- [`vscode-copilot.json`](./clients/vscode-copilot.json)
- [`continue.json`](./clients/continue.json)

## Tools

23 read-only + 3 opt-in write. Categories: `search`, `memory`, `graph`,
`sessions`, `jira`, `workspace`, `preferences`. Each tool is tagged with a
cost class (`free` / `cheap` / `standard` / `premium`) for per-session
budget caps — configurable in Settings → MCP.

**Full reference:** [`tools.md`](./tools.md) — every tool, what it does,
and what the user gains when Jarvis uses it.

## Module layout

The `jarvis-mcp` binary is the entry point for `backend/mcp_server/`:

- `__main__.py` — CLI bootstrap (`jarvis-mcp [--workspace PATH] [--allow-writes] [--verbose]`). Resolves the workspace, configures stdio logging to stderr (stdout is reserved for the MCP framing), then hands off to `app.build_app()`.
- `app.py` — FastMCP application factory. Registers every tool via `tools.register_all(mcp, workspace=…, allow_writes=…)`.
- `config.py` — Workspace path discovery: `--workspace` CLI flag → `JARVIS_WORKSPACE` env var → `~/.jarvis/config.toml` → default `~/Jarvis`.
- `middleware/audit.py` — JSONL audit logger. Every tool invocation appends one line to `app/logs/mcp/YYYY-MM-DD.jsonl` with timestamp, tool name, args summary, latency, and outcome.
- `middleware/budget.py` — Per-session cost-class accounting. Tracks `free` / `cheap` / `standard` / `premium` calls against the user's configured caps; refuses additional calls in a class once the cap is hit.
- `tools/` — One module per tool family. `register_all()` invokes each module's `register()`:
  - `search.py` — semantic + BM25 search across notes.
  - `notes.py` — read tools (and the opt-in write tools when `--allow-writes`).
  - `graph.py` — graph traversal and node lookup.
  - `sessions.py` — read past chat sessions and pin/unpin them.
  - `jira.py` — Jira-aware queries (delegates to `services.tools.jira_tools`).
  - `meta.py` — `jarvis_workspace_stats`, `jarvis_health`.
  - `continuation.py` — pagination cursors for tools that may exceed a single response window.
  - `writes.py` — registered only when `allow_writes=True`; appends notes, dismisses suggestions.

Tools call directly into the same backend services that power the FastAPI app (`services/memory_service`, `services/retrieval`, `services/graph_service`, `services/session_service`, `services/specialist_service`, `services/tools/jira_tools`, …) — there is no HTTP layer between the MCP server and the workspace. Both processes share the SQLite database file in the workspace; pragmas applied via `services/_db.py` (WAL mode, busy timeout, synchronous=NORMAL) keep concurrent writers from tripping each other when the FastAPI app and the MCP server run side by side.

## Specs

- [`refactor.spec.md`](./refactor.spec.md) — historical refactor notes
  (CLI-only, stdio-only architecture)
- [`../../steps/step-24*-mcp-*.spec.md`](../../steps/) — original design specs

> **Historical note:** earlier versions shipped an optional SSE transport
> on `127.0.0.1:8765` with a bearer token. That was removed in favour of
> the simpler stdio-only model: clients spawn `jarvis-mcp` themselves, so
> there's no background server, no port, and no token to manage.

