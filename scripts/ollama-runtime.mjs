import { spawn, spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createServer } from 'node:net';
import { resolve } from 'node:path';

// ─── Bundled Ollama runtime launcher (dev/serve) ─────────────────────────────
//
// In production the Tauri shell spawns the bundled Ollama runtime on a private
// localhost port and tells the sidecar about it via JARVIS_OLLAMA_BASE_URL
// (see desktop/scripts/fetch-ollama.sh + the Rust shell). The from-source dev
// and serve paths historically relied on whatever Ollama happened to be on
// :11434 — which breaks on Apple M5: upstream Ollama >=0.21.2 ships GGML metal
// kernels whose MPPTensorOpsMatMul2d instantiations fail Metal 4's strict
// bfloat/half matching, so the runner SIGABRTs and every model load returns
// 500. The repo therefore pins the runtime at 0.18.0 (fetch-ollama.sh §pin).
//
// This helper makes `npm run dev` / `npm run wake` mirror production: start the
// pinned bundled runtime on a free port and export JARVIS_OLLAMA_BASE_URL so
// the backend (services/ollama_service.py: DEFAULT_OLLAMA_BASE_URL) targets it.
//
// Escape hatches:
//   - JARVIS_OLLAMA_BASE_URL already set  -> respect it, spawn nothing.
//   - bundled runtime missing             -> on macOS/Linux, fetch it via
//                                            fetch-ollama.sh; otherwise warn
//                                            and fall back to system Ollama.

const isWin = process.platform === 'win32';

const RUNTIME_BIN = resolve(
  'desktop',
  'src-tauri',
  'binaries',
  'ollama-runtime',
  isWin ? 'ollama.exe' : 'ollama',
);
const FETCH_SCRIPT = resolve('desktop', 'scripts', 'fetch-ollama.sh');

const DIM = '\x1b[2m';
const YELLOW = '\x1b[33m';
const GREEN = '\x1b[32m';
const CYAN = '\x1b[36m';
const RESET = '\x1b[0m';

function findFreePort() {
  return new Promise((res, rej) => {
    const srv = createServer();
    srv.unref();
    srv.on('error', rej);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => res(port));
    });
  });
}

async function waitForOllama(baseUrl, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`${baseUrl}/api/tags`, { signal: AbortSignal.timeout(2000) });
      if (r.ok) return true;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

// Forward a child stream to `out`, one line at a time, dim-prefixed so the
// runtime's chatter doesn't drown the dev/backend/frontend logs.
function pipePrefixed(stream, out, tag) {
  let buf = '';
  stream.on('data', (chunk) => {
    buf += chunk.toString();
    const lines = buf.split('\n');
    buf = lines.pop() ?? '';
    for (const line of lines) out.write(`${tag}${line}\n`);
  });
}

/**
 * Start the bundled Ollama runtime and point the backend at it.
 *
 * @returns {Promise<{child: import('node:child_process').ChildProcess, baseUrl: string, port: number} | null>}
 *   A handle (caller must kill `child` on shutdown), or null when nothing was
 *   spawned (override present, or fell back to system Ollama).
 */
export async function startBundledOllama() {
  if (process.env.JARVIS_OLLAMA_BASE_URL) {
    console.log(
      `${DIM}[ollama] using JARVIS_OLLAMA_BASE_URL=${process.env.JARVIS_OLLAMA_BASE_URL} (override)${RESET}`,
    );
    return null;
  }

  if (!existsSync(RUNTIME_BIN)) {
    if (!isWin && existsSync(FETCH_SCRIPT)) {
      console.log(
        `${YELLOW}[ollama] bundled runtime not found — fetching pinned 0.18.0 via fetch-ollama.sh${RESET}`,
      );
      const r = spawnSync('bash', [FETCH_SCRIPT], { stdio: 'inherit' });
      if (r.status !== 0 || !existsSync(RUNTIME_BIN)) {
        console.warn(
          `${YELLOW}[ollama] fetch failed — falling back to system Ollama on :11434${RESET}`,
        );
        return null;
      }
    } else {
      console.warn(`${YELLOW}[ollama] bundled runtime not found at ${RUNTIME_BIN}.${RESET}`);
      console.warn(
        `${DIM}        Run "bash desktop/scripts/fetch-ollama.sh" to fetch it, or set${RESET}`,
      );
      console.warn(
        `${DIM}        JARVIS_OLLAMA_BASE_URL to your own Ollama. Falling back to :11434.${RESET}`,
      );
      return null;
    }
  }

  const port = await findFreePort();
  const baseUrl = `http://127.0.0.1:${port}`;
  console.log(`${CYAN}[ollama] starting bundled runtime (0.18.0) on ${baseUrl}${RESET}`);

  // The binary dlopens sibling dylibs via @loader_path, so launching it by its
  // own path (regardless of cwd) resolves the runtime payload correctly.
  const child = spawn(RUNTIME_BIN, ['serve'], {
    env: { ...process.env, OLLAMA_HOST: `127.0.0.1:${port}` },
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  const tag = `${DIM}[ollama]${RESET} `;
  pipePrefixed(child.stdout, process.stdout, tag);
  pipePrefixed(child.stderr, process.stderr, tag);
  child.on('error', (err) => {
    console.error(`${YELLOW}[ollama] failed to start: ${err.message}${RESET}`);
  });

  const ready = await waitForOllama(baseUrl);
  if (ready) {
    console.log(`${GREEN}[ollama] runtime ready${RESET}`);
  } else {
    console.warn(
      `${YELLOW}[ollama] runtime not ready within 30s — continuing (backend will retry)${RESET}`,
    );
  }

  // Backend children inherit process.env at spawn time; set it before they start.
  process.env.JARVIS_OLLAMA_BASE_URL = baseUrl;
  return { child, baseUrl, port };
}
