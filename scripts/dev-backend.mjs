import { spawn, spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';

const isWin = process.platform === 'win32';
const venvPython = isWin
  ? join('.venv', 'Scripts', 'python.exe')
  : join('.venv', 'bin', 'python');
const fullPath = join('backend', venvPython);

// Persistent fastembed cache for the embedder + reranker. When not frozen
// (dev/serve from source) the backend passes no cache_dir, so fastembed would
// otherwise default to $TMPDIR/fastembed_cache — which macOS purges, dropping
// the ONNX weights and breaking semantic search/rerank on the next run. Pin it
// to the gitignored bundled-models dir (same place desktop builds ship it via
// desktop/scripts/fetch-bundled-models.sh). Respects an existing override.
const fastembedCache =
  process.env.FASTEMBED_CACHE_PATH || resolve('backend', '_bundled_models', 'fastembed');

const YELLOW = '\x1b[33m';
const DIM = '\x1b[2m';
const RESET = '\x1b[0m';

if (!existsSync(fullPath)) {
  console.error(`[dev-backend] ${fullPath} not found.`);
  console.error(`${DIM}  → Run "npm run install:backend" or "npm run wake-up-jarvis" first.${RESET}`);
  process.exit(1);
}

// Sanity check: make sure the venv has uvicorn. If a previous install was
// interrupted the venv python exists but backend deps are missing.
const probe = spawnSync(venvPython, ['-c', 'import uvicorn, main'], {
  cwd: 'backend',
  stdio: 'pipe',
  encoding: 'utf8',
  timeout: 20000,
});
if (probe.error || probe.status !== 0) {
  console.error(`${YELLOW}[dev-backend] backend venv is missing dependencies.${RESET}`);
  const err = ((probe.stderr || '') + (probe.stdout || '')).trim();
  if (err) console.error(`${DIM}  ${err.split('\n').slice(-3).join('\n  ')}${RESET}`);
  console.error(`${DIM}  → Run "npm run install:backend" (or delete backend/.venv and "npm run wake-up-jarvis")${RESET}`);
  process.exit(1);
}

const child = spawn(
  venvPython,
  ['-m', 'uvicorn', 'main:app', '--reload', '--host', '127.0.0.1', '--port', '8000'],
  { cwd: 'backend', stdio: 'inherit', env: { ...process.env, FASTEMBED_CACHE_PATH: fastembedCache } },
);

const forward = (sig) => () => child.kill(sig);
process.on('SIGINT', forward('SIGINT'));
process.on('SIGTERM', forward('SIGTERM'));
child.on('exit', (code) => process.exit(code ?? 0));
