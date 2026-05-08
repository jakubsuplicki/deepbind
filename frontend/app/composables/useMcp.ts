/**
 * useMcp — composable for the Settings → MCP Server panel.
 *
 * The MCP server is a stdio CLI that MCP clients (Cursor, Claude Desktop,
 * VS Code, Continue, Zed) launch themselves. The Jarvis backend does NOT
 * manage its lifecycle — this composable just reports status and renders
 * client config snippets.
 *
 * `cli_command` + `cli_args` come from the backend, which picks them based
 * on whether `jarvis-mcp` is on PATH, whether we're running under
 * PyInstaller (then it's the bundle binary + `--mcp`), or a dev venv.
 */

import { ref, computed, readonly } from 'vue'

export interface McpInfo {
  cli_on_path: boolean
  cli_path: string | null
  cli_command: string
  cli_args: string[]
  workspace_path: string
  backend_dir: string
  tool_count: number
  write_tool_count: number
  audit_log_path: string
  calls_today: number
  last_call: string | null
  top_tool: string | null
}

const info = ref<McpInfo>({
  cli_on_path: false,
  cli_path: null,
  cli_command: 'jarvis-mcp',
  cli_args: ['--transport', 'stdio'],
  workspace_path: '',
  backend_dir: '',
  tool_count: 0,
  write_tool_count: 0,
  audit_log_path: '',
  calls_today: 0,
  last_call: null,
  top_tool: null,
})

const loading = ref(false)
const error = ref<string | null>(null)

async function refreshInfo(): Promise<void> {
  loading.value = true
  try {
    info.value = await $fetch<McpInfo>(apiUrl('/api/mcp/info'))
    error.value = null
  } catch (e) {
    error.value = (e as { statusMessage?: string })?.statusMessage ?? 'Failed to fetch MCP info'
  } finally {
    loading.value = false
  }
}

// ---------------------------------------------------------------------------
// Snippet generators — produce ready-to-paste client configs.
// ---------------------------------------------------------------------------

export interface SnippetContext {
  /** Either "jarvis-mcp" (CLI on PATH), the bundle binary path, or the dev venv fallback. */
  command: string
  /** Argv tail. Includes "--mcp" when the bundle binary is dispatching as MCP. */
  args: string[]
  /** True when `jarvis-mcp` is on PATH; affects which snippet hint is shown. */
  portable: boolean
}

function jsonStringify(obj: unknown): string {
  return JSON.stringify(obj, null, 2)
}

/** Claude Desktop / Cursor / Continue / Zed — stdio MCP config. */
export function buildStdioConfig(ctx: SnippetContext): string {
  return jsonStringify({
    mcpServers: {
      jarvis: {
        command: ctx.command,
        args: ctx.args,
      },
    },
  })
}

/** VS Code / GitHub Copilot — `.vscode/mcp.json` style. */
export function buildVscodeConfig(ctx: SnippetContext): string {
  return jsonStringify({
    servers: {
      jarvis: {
        type: 'stdio',
        command: ctx.command,
        args: ctx.args,
      },
    },
  })
}

export function useMcp() {
  const snippetCtx = computed<SnippetContext>(() => ({
    command: info.value.cli_command,
    args: info.value.cli_args,
    portable: info.value.cli_on_path,
  }))

  return {
    info: readonly(info),
    loading: readonly(loading),
    error: readonly(error),
    snippetCtx,
    refreshInfo,
    buildStdioConfig,
    buildVscodeConfig,
  }
}
