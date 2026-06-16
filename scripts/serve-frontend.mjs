import { createServer } from 'node:http';
import { request as proxyRequest } from 'node:http';
import { createReadStream, existsSync, statSync } from 'node:fs';
import { extname, join, normalize, resolve, sep } from 'node:path';

// ─── Static SPA server for the from-source serve/wake path ───────────────────
//
// The frontend builds as a static SPA (nuxt.config.ts: ssr:false +
// nitro.preset:'static'), so `nuxt build` emits .output/public/ with NO Nitro
// server entry — there is no .output/server/index.mjs to run. This server
// serves that static bundle and bridges the SPA's relative /api/* calls to the
// backend, which is exactly what the Nuxt dev server's devProxy does in dev.
//
// URL contract in a plain browser (no Tauri shell injecting window.__JARVIS_CONFIG__):
//   - REST: apiUrl() returns the path unchanged -> relative /api/... -> proxied
//     here to the backend on :8000.
//   - WebSocket: useWebSocket falls back to runtimeConfig.public.backendWsUrl
//     (ws://127.0.0.1:8000/api/chat/ws) and connects to the backend DIRECTLY,
//     so no WS proxy is needed in this server.

const publicDir = resolve('frontend', '.output', 'public');
const spaFallback = join(publicDir, '200.html'); // Nuxt SPA fallback shell
const indexHtml = join(publicDir, 'index.html');

if (!existsSync(publicDir)) {
  console.error(`[serve-frontend] ${publicDir} not found. Run "npm run build" first.`);
  process.exit(1);
}

const PORT = Number(process.env.NITRO_PORT || 3000);
const HOST = process.env.NITRO_HOST || '127.0.0.1';
const BACKEND_HOST = process.env.JARVIS_BACKEND_HOST || '127.0.0.1';
const BACKEND_PORT = Number(process.env.JARVIS_BACKEND_PORT || 8000);

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.ico': 'image/x-icon',
  '.webp': 'image/webp',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.map': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.md': 'text/markdown; charset=utf-8',
  '.wasm': 'application/wasm',
};

// Reverse-proxy /api/** (REST) to the backend. Streaming bodies (SSE, chunked)
// pass straight through via pipe.
function proxyApi(req, res) {
  const upstream = proxyRequest(
    {
      host: BACKEND_HOST,
      port: BACKEND_PORT,
      method: req.method,
      path: req.url,
      headers: { ...req.headers, host: `${BACKEND_HOST}:${BACKEND_PORT}` },
    },
    (upRes) => {
      res.writeHead(upRes.statusCode || 502, upRes.headers);
      upRes.pipe(res);
    },
  );
  upstream.on('error', (err) => {
    if (!res.headersSent) res.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' });
    res.end(`[serve-frontend] backend proxy error: ${err.message}`);
  });
  req.pipe(upstream);
}

function sendFile(res, filePath, status = 200) {
  const type = MIME[extname(filePath).toLowerCase()] || 'application/octet-stream';
  res.writeHead(status, { 'content-type': type });
  createReadStream(filePath).pipe(res);
}

function serveStatic(req, res) {
  const urlPath = decodeURIComponent((req.url || '/').split('?')[0]);
  let filePath = normalize(join(publicDir, urlPath));

  // Path-traversal guard: the resolved path must stay inside publicDir.
  if (filePath !== publicDir && !filePath.startsWith(publicDir + sep)) {
    res.writeHead(403, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('Forbidden');
    return;
  }

  let stat = null;
  try {
    stat = statSync(filePath);
  } catch {
    stat = null;
  }
  if (stat && stat.isDirectory()) {
    filePath = join(filePath, 'index.html');
    try {
      stat = statSync(filePath);
    } catch {
      stat = null;
    }
  }

  if (stat && stat.isFile()) {
    sendFile(res, filePath);
    return;
  }

  // Unknown path -> SPA client-side route: serve the fallback shell with 200 so
  // the client router can take over.
  const fallback = existsSync(spaFallback) ? spaFallback : indexHtml;
  if (existsSync(fallback)) {
    sendFile(res, fallback, 200);
    return;
  }
  res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
  res.end('Not found');
}

const server = createServer((req, res) => {
  if ((req.url || '').startsWith('/api')) {
    proxyApi(req, res);
    return;
  }
  serveStatic(req, res);
});

server.listen(PORT, HOST, () => {
  console.log(`[serve-frontend] serving ${publicDir}`);
  console.log(
    `[serve-frontend] http://${HOST}:${PORT}  (/api -> http://${BACKEND_HOST}:${BACKEND_PORT})`,
  );
});

const shutdown = () => server.close(() => process.exit(0));
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
