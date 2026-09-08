// Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import assert from 'node:assert/strict';
import http from 'node:http';
import { mkdtemp, mkdir, writeFile, rm, symlink, realpath } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { startInspectionPreview } from '../scripts/serve-inspection.mjs';

const apiPath = '/v1/operator/agent-generations/091705f9-e1bb-55f4-b07c-de3d58cb173c/inspection';
function request(server, route = '/', { method = 'GET', headers = {} } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: '127.0.0.1', port: server.address().port,
      path: route, method, headers }, response => {
      const chunks = [];
      response.on('data', chunk => chunks.push(chunk));
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers,
        body: Buffer.concat(chunks).toString() }));
      response.on('error', reject);
    });
    req.on('error', reject); req.end();
  });
}
function close(server) {
  server.closeAllConnections();
  return new Promise(resolve => server.close(resolve));
}
async function fixture(t, upstreamHandler, timeoutMs = 1000) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'hcuopt-preview-'));
  const root = path.join(directory, 'client');
  await mkdir(path.join(root, 'assets'), { recursive: true });
  await writeFile(path.join(root, 'index.html'), '<h1>inspection login</h1>');
  await writeFile(path.join(root, 'assets', 'app.js'), '/* public build */');
  await writeFile(path.join(directory, 'private.js'), 'PRIVATE');
  const calls = [];
  const upstream = http.createServer((req, res) => {
    calls.push({ method: req.method, path: req.url, headers: req.headers });
    upstreamHandler(req, res);
  });
  await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve));
  const viewer = await startInspectionPreview({ port: 0, apiPort: upstream.address().port,
    clientRoot: root, timeoutMs });
  t.after(async () => {
    await close(viewer); await close(upstream);
    const resolved = await realpath(directory);
    assert.ok(resolved.startsWith((await realpath(os.tmpdir())) + path.sep));
    assert.ok(path.basename(resolved).startsWith('hcuopt-preview-'));
    await rm(resolved, { recursive: true });
  });
  return { viewer, upstream, calls, root, directory };
}

test('viewer serves only built public files and validates Host/Origin/method before proxying', async t => {
  const { viewer, calls } = await fixture(t, () => assert.fail('must not reach API'));
  assert.equal(viewer.address().address, '127.0.0.1');
  const page = await request(viewer, '/?agentInspection=selected');
  assert.equal(page.status, 200);
  assert.match(page.body, /inspection login/);
  assert.equal(page.headers['cache-control'], 'no-store');
  assert.equal((await request(viewer, '/assets/app.js')).status, 200);
  assert.equal((await request(viewer, '/', { method: 'HEAD' })).body, '');
  for (const route of ['/v1/tasks', '/openapi.json', '/private.js', '/assets/../../private.js',
    '/assets/%2e%2e%2fprivate.js', '/assets/environment.json', '/assets/missing.js',
    '/v1/operator/agent-generations/not-a-uuid/inspection']) {
    assert.equal((await request(viewer, route)).status, 404, route);
  }
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD']) {
    assert.equal((await request(viewer, apiPath, { method })).status, 405);
  }
  for (const headers of [{ Host: 'example.invalid' }, { Origin: 'http://example.invalid' },
    { 'Sec-Fetch-Site': 'cross-site' }]) {
    assert.equal((await request(viewer, apiPath, { headers })).status, 403);
  }
  assert.equal((await request(viewer, apiPath + '?redirect=other')).status, 400);
  assert.equal(calls.length, 0);
});

test('viewer forwards exact read plus explicit auth only and retains denial/no-store semantics', async t => {
  let status = 200;
  const { viewer, calls } = await fixture(t, (req, res) => {
    res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'public',
      'Set-Cookie': 'not-allowed=1', 'Access-Control-Allow-Origin': '*' });
    res.end('{"fixture":true}');
  });
  for (status of [200, 401, 403, 404, 405, 422, 503]) {
    const response = await request(viewer, apiPath, { headers: {
      Authorization: 'Basic fixture-only', Cookie: 'private=1', 'X-Forwarded-Proto': 'https',
      'X-Forwarded-For': 'example.invalid', 'X-Model-Key': 'never-forward' } });
    assert.equal(response.status, status);
    assert.equal(response.headers['cache-control'], 'no-store');
    assert.equal(response.headers['set-cookie'], undefined);
    assert.equal(response.headers['access-control-allow-origin'], undefined);
    assert.equal(response.headers['x-content-type-options'], 'nosniff');
    assert.equal(response.body, '{"fixture":true}');
  }
  assert.equal(calls.length, 7);
  for (const call of calls) {
    assert.equal(call.method, 'GET'); assert.equal(call.path, apiPath);
    assert.equal(call.headers.authorization, 'Basic fixture-only');
    for (const key of ['cookie', 'x-forwarded-proto', 'x-forwarded-for', 'x-model-key']) {
      assert.equal(call.headers[key], undefined);
    }
  }
  await request(viewer, apiPath);
  assert.equal(calls.at(-1).headers.authorization, undefined);
});

test('viewer rejects redirects and HTML without following a credential-bearing hop', async t => {
  let redirect = true;
  const { viewer, calls } = await fixture(t, (req, res) => {
    res.writeHead(redirect ? 302 : 200, { 'Content-Type': redirect ? 'application/json' : 'text/html',
      Location: 'http://example.invalid/private' }); res.end('not evidence');
  });
  for (const mode of [true, false]) {
    redirect = mode;
    const response = await request(viewer, apiPath, { headers: { Authorization: 'Basic fixture-only' } });
    assert.equal(response.status, 502); assert.equal(response.headers.location, undefined);
    assert.doesNotMatch(response.body, /private|not evidence/);
  }
  assert.equal(calls.length, 2);
});

test('viewer times out stalled upstream with a bounded safe failure and no retry', async t => {
  const { viewer, calls } = await fixture(t, () => {}, 50);
  const response = await request(viewer, apiPath);
  assert.equal(response.status, 504);
  assert.match(response.body, /inspection_upstream_timeout/);
  assert.equal(calls.length, 1);
});

test('viewer handles unavailable and oversized upstream without returning fake evidence', async t => {
  const { viewer, upstream } = await fixture(t, (req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end('x'.repeat(8 * 1024 * 1024 + 1));
  });
  assert.equal((await request(viewer, apiPath)).status, 502);
  await close(upstream);
  const response = await request(viewer, apiPath);
  assert.equal(response.status, 503);
  assert.doesNotMatch(response.body, /ECONNREFUSED|127\.0\.0\.1|fixture-only/);
});

test('viewer prevents a build asset symlink from exposing files outside the static root', async t => {
  const { directory } = await fixture(t, () => assert.fail('must not reach API'));
  const alternate = path.join(directory, 'alternate'); await mkdir(alternate);
  await writeFile(path.join(alternate, 'index.html'), '<h1>trusted index</h1>');
  try { await symlink(directory, path.join(alternate, 'assets'), 'junction'); }
  catch (error) { if (error.code === 'EPERM') { t.skip('OS denies fixture symlink creation'); return; } throw error; }
  const viewer = await startInspectionPreview({ port: 0, clientRoot: alternate });
  try { assert.equal((await request(viewer, '/assets/private.js')).status, 404); }
  finally { await close(viewer); }
});

test('viewer CLI validates options and fails closed instead of stopping a busy service', async t => {
  const script = fileURLToPath(new URL('../scripts/serve-inspection.mjs', import.meta.url));
  const help = spawnSync(process.execPath, [script, '--help'], { encoding: 'utf8' });
  assert.equal(help.status, 0); assert.match(help.stdout, /Loopback read-only/);
  for (const args of [['--host', '0.0.0.0'], ['--api-port', 'https://example.invalid'], ['--port', '0'],
    ['--port', '4191', '--port', '4192']]) {
    assert.equal(spawnSync(process.execPath, [script, ...args], { encoding: 'utf8' }).status, 2);
  }
  const { viewer, root } = await fixture(t, () => {});
  await assert.rejects(startInspectionPreview({ port: viewer.address().port, clientRoot: root }), { code: 'EADDRINUSE' });
  assert.equal((await request(viewer)).status, 200);
  await assert.rejects(startInspectionPreview({ clientRoot: path.join(root, 'missing') }), /Build output unavailable/);
  await assert.rejects(startInspectionPreview({ port: 4191, apiPort: 4191, clientRoot: root }), /must be different/);
});
