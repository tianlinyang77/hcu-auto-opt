// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import http from 'node:http';
import { readFile, realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const defaultRoot = fileURLToPath(new URL('../dist/client/', import.meta.url));
const inspectionPath = /^\/v1\/operator\/agent-generations\/[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\/inspection$/i;
const assetPath = /^\/assets\/[a-zA-Z0-9][a-zA-Z0-9_.-]*\.(?:js|css|woff2|svg|png|ico)$/;
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.woff2': 'font/woff2', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.ico': 'image/x-icon' };
const allowedStatuses = new Set([200, 401, 403, 404, 405, 422, 503]);

function portNumber(value, allowZero = false) {
  if (!Number.isInteger(value) || value < (allowZero ? 0 : 1) || value > 65535) {
    throw new Error('Port must be an integer from 1 to 65535.');
  }
  return value;
}

function reply(res, status, code) {
  if (res.writableEnded || res.destroyed) return;
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ code, message: code }));
}

function proxyInspection(req, res, apiPort, pathname, timeoutMs) {
  const headers = { Host: `127.0.0.1:${apiPort}`, Accept: 'application/json' };
  if (req.headers.authorization) headers.Authorization = req.headers.authorization;
  // No cookies, proxy headers, retries, redirects, secrets or inferred credentials.
  const upstream = http.request({ hostname: '127.0.0.1', port: apiPort,
    path: pathname, method: 'GET', headers }, (incoming) => {
    if (!allowedStatuses.has(incoming.statusCode)
      || !/^application\/json(?:\s*;|$)/i.test(incoming.headers['content-type'] || '')) {
      reply(res, 502, 'inspection_upstream_response_rejected');
      incoming.destroy();
      return;
    }
    const chunks = [];
    let size = 0;
    incoming.on('data', chunk => {
      size += chunk.length;
      if (size > 8 * 1024 * 1024) {
        reply(res, 502, 'inspection_response_too_large');
        incoming.destroy();
      } else chunks.push(chunk);
    });
    incoming.on('error', () => reply(res, 503, 'inspection_upstream_unavailable'));
    incoming.on('end', () => {
      if (res.writableEnded || res.destroyed) return;
      if (incoming.statusCode === 401) {
        res.setHeader('WWW-Authenticate', 'Basic realm="HCU inspection", charset="UTF-8"');
      }
      res.writeHead(incoming.statusCode, { 'Content-Type': 'application/json' });
      res.end(Buffer.concat(chunks));
    });
  });
  const deadline = setTimeout(() => {
    reply(res, 504, 'inspection_upstream_timeout');
    upstream.destroy();
  }, timeoutMs);
  res.on('close', () => { clearTimeout(deadline); upstream.destroy(); });
  upstream.on('error', () => reply(res, 503, 'inspection_upstream_unavailable'));
  upstream.end();
}

/** Trusted, already-built static root only; not an arbitrary-file/evidence server. */
export async function startInspectionPreview({ port = 4191, apiPort = 8091,
  clientRoot = defaultRoot, timeoutMs = 30000 } = {}) {
  portNumber(port, true);
  portNumber(apiPort);
  if (port === apiPort) throw new Error('Viewer and API ports must be different.');
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 30000) {
    throw new Error('Timeout must be between 1 and 30000 milliseconds.');
  }
  let root;
  try {
    root = await realpath(clientRoot);
    const index = await realpath(path.join(root, 'index.html'));
    if (!index.startsWith(root + path.sep) || !(await stat(index)).isFile()) throw new Error();
  } catch { throw new Error('Build output unavailable. Run npm run build first.'); }
  const server = http.createServer(async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'no-referrer');
    const authority = `127.0.0.1:${server.address().port}`;
    if (req.headers.host !== authority
      || (req.headers.origin && req.headers.origin !== `http://${authority}`)
      || req.headers['sec-fetch-site'] === 'cross-site') {
      reply(res, 403, 'inspection_origin_rejected'); return;
    }
    if (!['GET', 'HEAD'].includes(req.method)) { reply(res, 405, 'inspection_read_only'); return; }
    if (!req.url?.startsWith('/') || req.url.startsWith('//')) {
      reply(res, 400, 'inspection_invalid_path'); return;
    }
    let url;
    try { url = new URL(req.url, `http://${authority}`); }
    catch { reply(res, 400, 'inspection_invalid_path'); return; }
    if (inspectionPath.test(url.pathname)) {
      if (req.method !== 'GET') { reply(res, 405, 'inspection_read_only'); return; }
      if (url.search) { reply(res, 400, 'inspection_invalid_path'); return; }
      proxyInspection(req, res, apiPort, url.pathname, timeoutMs); return;
    }
    if (url.pathname !== '/' && !assetPath.test(url.pathname)) {
      reply(res, 404, 'inspection_route_not_found'); return;
    }
    try {
      const target = await realpath(path.join(root, url.pathname === '/' ? 'index.html' : url.pathname.slice(1)));
      if (!target.startsWith(root + path.sep) || !(await stat(target)).isFile()) {
        reply(res, 404, 'inspection_route_not_found'); return;
      }
      res.setHeader('Content-Type', types[path.extname(target)] || 'application/octet-stream');
      res.end(req.method === 'HEAD' ? undefined : await readFile(target));
    } catch { reply(res, 404, 'inspection_route_not_found'); }
  });
  server.headersTimeout = 10000;
  server.requestTimeout = 30000;
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', resolve);
  });
  return server;
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length === 1 && args[0] === '--help') {
    console.log('Usage: npm run inspection:serve -- [--port 4191] [--api-port 8091]');
    console.log('Loopback read-only viewer; requires built assets and an approved local API/SSH tunnel.');
    return;
  }
  const options = {};
  for (let i = 0; i < args.length; i += 2) {
    const key = { '--port': 'port', '--api-port': 'apiPort' }[args[i]];
    if (!key || key in options || !/^\d+$/.test(args[i + 1] || '')) throw new Error('Invalid or duplicate option. Use --help.');
    options[key] = portNumber(Number(args[i + 1]));
  }
  const server = await startInspectionPreview(options);
  console.log(`Read-only inspection viewer: http://127.0.0.1:${server.address().port}`);
  console.log('Open /?agentInspection=<authorized-run-id> and enter the independent read credential.');
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => {
    server.close(); server.closeAllConnections();
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    // Never print request headers, upstream exceptions or private paths.
    console.error(error.code === 'EADDRINUSE' ? 'Viewer port already in use; no existing process was stopped.'
      : ['Build output unavailable. Run npm run build first.', 'Invalid or duplicate option. Use --help.',
        'Port must be an integer from 1 to 65535.', 'Viewer and API ports must be different.'].includes(error.message)
        ? error.message : 'Inspection viewer could not start. Check local build and port configuration.');
    process.exitCode = 2;
  });
}
